"""Run product calculations in a disposable worker process.

Some DirectML and GPU runtimes keep native allocations for the lifetime of
their process. A child process gives those allocations a deterministic release
boundary when a calculation finishes, while the parent retains the UI/service
and result assets.
"""
from __future__ import annotations

import multiprocessing
import traceback
from typing import Any


# Allow native GPU/PyTorch teardown after closing IPC; this is not a
# calculation timeout. Only an unusable channel starts this grace period.
_WORKER_EXIT_GRACE_SECONDS = 30.0


def _exit_description(code: int | None) -> str:
    """Preserve raw exit codes and identify known Windows native exceptions.

    Access violation identifies the failure class, not its cause: the code alone
    cannot distinguish a backend/driver defect from allocation or other faults.
    """
    label = f"退出码 {code}"
    if isinstance(code, int) and (code & 0xffffffff) == 0xc0000005:
        label += " / 0xC0000005（原生访问冲突；不能仅据此判定内存不足）"
    return label


class _PipeCancelEvent:
    """Cancellation view polled by a child while the parent owns the request."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.cancelled = False

    def is_set(self) -> bool:
        if self.cancelled:
            return True
        try:
            while self.connection.poll():
                message = self.connection.recv()
                if isinstance(message, tuple) and message and message[0] == "cancel":
                    self.cancelled = True
        except (EOFError, OSError):
            # A lost parent cannot consume results. Stop work cooperatively.
            self.cancelled = True
        return self.cancelled


_in_scheduler_worker = False


def _execute(kind, arguments, cancel, report):
    if kind in ("recipe", "single"):
        from .compute_resources import estimate_memory
        runtime = arguments.get("runtime") or {}
        profile = runtime.get("profile", runtime)
        memory_estimate = estimate_memory(arguments["settings"],
            layer_count=len(profile.get("steps", [])) or 1)
    if kind == "recipe":
        from .algorithm_paths import run_recipe
        result = run_recipe(
            arguments["settings"], arguments["runtime"],
            algorithm=arguments["algorithm"], options=arguments["options"],
            backend=arguments["backend"], cancel_event=cancel,
            fast_cycle=arguments["fast_cycle"],
            fast_cycle_residual_percent=arguments["fast_cycle_residual_percent"],
            capture_layer_fields=arguments.get("capture_layer_fields", False),
            layer_field_spool_path=arguments.get("layer_field_spool_path"),
            progress=report)
    elif kind == "single":
        from .simulator import simulate
        result = simulate(
            arguments["settings"], mask=arguments["mask"],
            backend=arguments["backend"], cancel_event=cancel,
            source_profile=arguments["source_profile"])
    elif kind == "mask_optimization":
        from .api import CoatService
        from .compute_resources import estimate_memory
        service = CoatService(arguments["root"], restrict_to_root=arguments["restrict_to_root"])
        document = service.load("process", arguments["process_path"])
        memory_estimate = estimate_memory(arguments["settings"],
            layer_count=len(document.data.get("steps", [])) or 1)
        result = service._optimize_masks_worker(arguments["settings"], arguments["process_path"],
            options=arguments["options"], pending_only=arguments["pending_only"],
            cancel_event=cancel, progress_callback=report,
            checkpoint_path=arguments.get("checkpoint_path"))
    else:
        raise ValueError("未知的隔离计算类型")
    result["memory_estimate"] = memory_estimate
    return result


def _compute_child(connection: Any, kind: str, arguments: dict[str, Any]) -> None:
    cancel = _PipeCancelEvent(connection)

    def report(payload: object) -> None:
        connection.send(("progress", payload))

    try:
        result = _execute(kind, arguments, cancel, report)
        connection.send(("result", result))
    except BaseException as exc:
        try:
            connection.send(("error", type(exc).__name__, str(exc), traceback.format_exc()))
        except (OSError, EOFError, MemoryError):
            # A second allocation/serialization failure can prevent even the
            # small error envelope. Parent exit/channel checks report failure.
            pass
    finally:
        connection.close()


def _run(kind: str, arguments: dict[str, Any], *, cancel_event: Any = None,
         progress: Any = None) -> dict[str, Any]:
    if _in_scheduler_worker:
        return _execute(kind, arguments, cancel_event, progress or (lambda value: None))
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=True)
    process = None
    try:
        process = context.Process(target=_compute_child, args=(child, kind, arguments),
                                  name="coat-isolated-compute")
        process.start()
    except BaseException:
        parent.close()
        child.close()
        if process is not None:
            if process.is_alive():
                process.terminate()
                process.join()
            process.close()
        raise
    child.close()

    result = None
    remote_error = None
    sent_cancel = False
    transport_error = None
    forced_stop = False

    def receive(message: object) -> None:
        nonlocal result, remote_error
        if not isinstance(message, tuple) or not message:
            return
        if message[0] == "progress":
            if progress is not None:
                progress(message[1])
        elif message[0] == "result":
            result = message[1]
        elif message[0] == "error":
            remote_error = (str(message[1]), str(message[2]),
                            str(message[3]) if len(message) > 3 else "")

    def read_one(timeout: float = 0.0) -> bool:
        nonlocal transport_error
        try:
            if not parent.poll(timeout):
                return False
            message = parent.recv()
        except (EOFError, OSError) as exc:
            # Windows can report pipe closure from poll (PeekNamedPipe), not
            # just recv. Do not let a final drain hide a result or child error.
            transport_error = exc
            return False
        # Callback errors belong to the caller, not to the IPC transport.
        receive(message)
        return True

    try:
        while process.is_alive():
            if cancel_event is not None and cancel_event.is_set() and not sent_cancel:
                try:
                    parent.send(("cancel",))
                except (OSError, EOFError):
                    pass
                sent_cancel = True
            read_one(0.1)
            if transport_error is not None:
                break
        # Drain before joining: a large result can block send until consumed.
        if transport_error is None:
            while read_one():
                pass
        # EOF normally precedes a clean exit by a few instructions. A broken
        # channel must not leave the queue waiting forever for a live worker.
        process.join(_WORKER_EXIT_GRACE_SECONDS)
        if process.is_alive():
            forced_stop = True
            process.terminate()
            process.join()
        diagnostic = (
            f"隔离计算（{kind} / {arguments.get('backend', '未知设备')}，"
            f"{_exit_description(process.exitcode)}，子进程 PID {process.pid}）")
        if transport_error is not None:
            diagnostic += (f"；通信管道已关闭：{type(transport_error).__name__}"
                           f" {transport_error}")
        if forced_stop:
            diagnostic += "；管道关闭后子进程未退出，已终止"
        if remote_error is not None:
            name, message, remote_traceback = remote_error
            from .algorithm_paths import BudgetExceeded
            exception_type = {
                "InterruptedError": InterruptedError,
                "TimeoutError": TimeoutError,
                "BudgetExceeded": BudgetExceeded,
                "ValueError": ValueError,
                "TypeError": TypeError,
                "OSError": OSError,
                "MemoryError": MemoryError,
                "RuntimeError": RuntimeError,
            }.get(name, RuntimeError)
            if name == "MemoryError":
                message = "内存不足：隔离计算无法完成分配或结果传输。" + message
            detail = f"{message}\n{diagnostic}"
            if remote_traceback:
                detail += f"\n子进程 traceback:\n{remote_traceback}"
            raise exception_type(detail)
        expected_closure = (
            transport_error is None or isinstance(transport_error, EOFError)
            or getattr(transport_error, "winerror", None) in (109, 232))
        if result is None or process.exitcode != 0 or forced_stop or not expected_closure:
            reason = "未返回结果" if result is None else "结果未通过进程退出校验"
            if process.exitcode not in (None, 0):
                reason += "；计算进程异常退出，可能是内存耗尽或原生设备错误（未自动重算）"
            raise RuntimeError(f"{diagnostic}；{reason}") from transport_error
        return result
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join()
        process.close()


def simulate_single_isolated(settings, *, mask, backend, source_profile,
                             cancel_event=None) -> dict[str, Any]:
    return _run("single", {"settings": settings, "mask": mask,
                            "backend": backend, "source_profile": source_profile},
                cancel_event=cancel_event)


def simulate_recipe_isolated(settings, runtime, *, algorithm, options, backend,
                             fast_cycle, fast_cycle_residual_percent,
                             cancel_event=None, progress=None,
                             capture_layer_fields=False,
                             layer_field_spool_path=None) -> dict[str, Any]:
    return _run("recipe", {"settings": settings, "runtime": runtime,
                            "algorithm": algorithm, "options": dict(options or {}),
                            "backend": backend, "fast_cycle": fast_cycle,
                            "fast_cycle_residual_percent": fast_cycle_residual_percent,
                            "capture_layer_fields": bool(capture_layer_fields),
                            "layer_field_spool_path": str(layer_field_spool_path)
                                if layer_field_spool_path is not None else None},
                cancel_event=cancel_event, progress=progress)



def optimize_masks_isolated(settings, process_path, *, root, restrict_to_root,
                            options, pending_only, cancel_event=None, progress=None,
                            checkpoint_path=None):
    return _run("mask_optimization", {"settings": settings, "process_path": process_path,
                "root": root, "restrict_to_root": restrict_to_root,
                "options": dict(options or {}), "pending_only": pending_only,
                "checkpoint_path": str(checkpoint_path) if checkpoint_path is not None else None,
                "backend": (options or {}).get("backend", "自动")},
                cancel_event=cancel_event, progress=progress)
