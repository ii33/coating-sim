"""Bounded, ordered, Qt-free physical evaluation workers for synthesis.

Only trusted parent-created objects cross the private framed-pickle pipes. No
worker owns proposal IDs, budgets, archive decisions, audit or application.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from copy import deepcopy
import hashlib
from dataclasses import dataclass
import errno
import math
from itertools import chain
import os
from pathlib import Path
import pickle
from queue import Empty, Full, Queue
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
from threading import Event, Thread
import time
from typing import Any, Callable, Iterable, Mapping

from . import film_optimization_structure as physical
from .film_performance import available_cpu_workers

MIN_PARALLEL_WORK = 200_000  # Project cold-start/discovery heuristic, not a work budget.
STARTUP_TIMEOUT_SECONDS = 20.0
CLOSE_GRACE_SECONDS = 0.5
MAX_FRAME_BYTES = 256 * 1024**2
THREAD_ENVIRONMENT = {name: "1" for name in (
    "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS")}


@dataclass
class EvaluationTask:
    design: Mapping[str, Any]
    jacobian: bool = True
    zero_ids: tuple[str, ...] = ()
    context: Any = None
    retain_responses: bool = False


@dataclass
class EvaluationOutcome:
    token: int
    value: Any = None
    error: BaseException | None = None
    infrastructure: bool = False
    consumed: bool = False
    discarded: bool = False
    worker_pid: int | None = None


class InfrastructureFailure(RuntimeError):
    """The same logical evaluation must be recomputed serially."""


def _write_frame(stream, value):
    payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    if len(payload) > MAX_FRAME_BYTES:
        raise InfrastructureFailure("headless evaluation frame exceeds IPC resource limit")
    stream.write(struct.pack("!Q", len(payload)))
    stream.write(payload)
    stream.flush()
    return len(payload) + 8


def _read_frame(stream):
    header = stream.read(8)
    if not header:
        raise EOFError("headless worker pipe closed")
    if len(header) != 8:
        raise InfrastructureFailure("truncated headless frame header")
    length = struct.unpack("!Q", header)[0]
    if length > MAX_FRAME_BYTES:
        raise InfrastructureFailure("headless frame exceeds IPC resource limit")
    chunks, remaining = [], length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("truncated headless frame body")
        chunks.append(chunk)
        remaining -= len(chunk)
    started = time.perf_counter()
    value = pickle.loads(b"".join(chunks))  # Private, trusted self-created pipe.
    return value, time.perf_counter() - started, length + 8


def _windows_creation_time(handle=None):
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes as w
    windows_library = getattr(ctypes, "WinDLL", None)
    if windows_library is None:
        raise OSError("Windows DLL loader unavailable")
    kernel = windows_library("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.GetProcessTimes.argtypes = [w.HANDLE, ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME),
                                      ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME)]
    times = [w.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle or kernel.GetCurrentProcess(), *(ctypes.byref(t) for t in times)):
        raise OSError("cannot read owned process creation identity")
    return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime


class _WindowsWorkerJob:
    """Assign our suspended launcher before it can create its Python child."""
    def __init__(self):
        if os.name != "nt":
            raise OSError("owned Windows worker jobs require Windows")
        import ctypes
        from ctypes import wintypes as w
        self.ctypes, self.w = ctypes, w
        windows_library = getattr(ctypes, "WinDLL", None)
        if windows_library is None:
            raise OSError("Windows DLL loader unavailable")
        self.kernel = windows_library("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            ("SetInformationJobObject", [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
            ("CloseHandle", [w.HANDLE], w.BOOL),
            ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            ("IsProcessInJob", [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
            ("WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD)):
            method = getattr(self.kernel, name)
            method.argtypes, method.restype = args, result
        self.resume = windows_library("ntdll").NtResumeProcess
        self.resume.argtypes, self.resume.restype = [w.HANDLE], w.LONG

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]
        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("owned synthesis Job Object unavailable")
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise OSError("owned synthesis Job Object configuration failed")

    def attach_and_resume(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError("cannot own suspended synthesis launcher")
        if self.resume(int(process._handle)) < 0:
            raise OSError("cannot resume owned synthesis launcher")

    def verify_worker(self, pid, creation_time):
        handle = self.kernel.OpenProcess(0x100000 | 0x1000, False, pid)
        if not handle:
            raise OSError("cannot retain owned actual worker identity")
        belongs = self.w.BOOL()
        if (not self.kernel.IsProcessInJob(handle, self.handle, self.ctypes.byref(belongs))
                or not belongs.value or _windows_creation_time(handle) != creation_time):
            self.kernel.CloseHandle(handle)
            raise OSError("actual worker identity is not in this owned job")
        return handle

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class _CancelFile:
    def __init__(self, path):
        self.path = Path(path)

    def is_set(self):
        return self.path.exists()


def _worker_main():
    started = time.perf_counter()
    initial, _, _ = _read_frame(sys.stdin.buffer)
    request, cancel = initial["request"], _CancelFile(initial["cancel_path"])
    prepared = physical._PreparedStructureEvaluator(request)
    _write_frame(sys.stdout.buffer, {"kind": "ready", "pid": os.getpid(),
        "prepared_material_contexts": 1,
        "frozen_request_identity": initial.get("frozen_request_identity"),
        "initialization_seconds": time.perf_counter() - started,
        "creation_time": _windows_creation_time(),
        "qt_imported": any(name.startswith(("PySide", "PyQt")) for name in sys.modules),
        "thread_environment": {key: os.environ.get(key) for key in THREAD_ENVIRONMENT}})
    while True:
        command, _, _ = _read_frame(sys.stdin.buffer)
        if command["kind"] == "close":
            return
        token = command["token"]
        _write_frame(sys.stdout.buffer, {"kind": "started", "token": token})
        work = {"physical_evaluations": 0, "wavelength_points": 0, "incomplete_calls": 0}
        original = physical.simulate_with_thickness_jacobian

        def observed(*args, _work=work, _original=original, **kwargs):
            _work["physical_evaluations"] += 1
            _work["incomplete_calls"] += 1
            value = _original(*args, **kwargs)
            _work["wavelength_points"] += len(value["wavelength_nm"])
            _work["incomplete_calls"] -= 1
            return value

        physical.simulate_with_thickness_jacobian = observed
        before = time.perf_counter()
        cpu_elapsed: float | None = None
        try:
            design = command["design"]
            cpu_before = time.process_time()
            try:
                value = prepared.evaluate(design,
                    variable_ids=tuple(row["id"] for row in design["front_layers"]) if command["jacobian"] else (),
                    with_jacobian=command["jacobian"], zero_thickness_layer_ids=command["zero_ids"],
                    derived_validation_grid=False, cancel_event=cancel)
            finally:
                cpu_elapsed = time.process_time() - cpu_before
            value = dict(value)
            if not command.get("retain_responses", False):
                value.pop("responses", None)  # Legacy decisions retain the compact contract.
            reply: dict[str, Any] = {"status": "ok", "value": value}
        except physical._StructureCancelled:
            reply = {"status": "cancelled", "message": "headless evaluation cancelled"}
        except (ValueError, ArithmeticError) as exc:
            reply = {"status": "physical_error", "error_type": type(exc).__name__,
                     "arithmetic": isinstance(exc, ArithmeticError), "message": str(exc)}
        except BaseException as exc:
            reply = {"status": "infrastructure_error", "message": f"{type(exc).__name__}: {exc}"}
        finally:
            physical.simulate_with_thickness_jacobian = original
        reply.update(kind="result", token=token, work=work,
                     elapsed_seconds=time.perf_counter() - before, cpu_seconds=cpu_elapsed)
        _write_frame(sys.stdout.buffer, reply)


class _Child:
    def __init__(self, process, stderr, reader, job=None):
        self.process, self.stderr, self.reader, self.job = process, stderr, reader, job
        self.writer_queue: Queue = Queue(maxsize=2)
        self.writer_stop = Event()
        self.writer: Thread | None = None
        self.actual_pid: int | None = None
        self.actual_handle = None
        self.actual_dead = False
        self.launcher_creation_time = _windows_creation_time(int(process._handle)) if os.name == "nt" else None
        self.pending: int | None = None
        self.ready = False


@dataclass
class _SerialCost:
    work: int = 0
    seconds: float = 0.0
    samples: int = 0


@dataclass
class _WorkerCPUCost:
    completed_tasks: int = 0
    known_cpu_seconds: float = 0.0
    cpu_samples: int = 0
    unrecorded_completed_tasks: int = 0


class OrderedEvaluationExecutor:
    """Run-scoped pool; callers consume outcomes in the original proposal order."""

    def __init__(self, requested_workers: int, cancel_event=None):
        self.requested_workers = requested_workers
        available = available_cpu_workers()
        self.available_workers = available
        self.workers = min(8 if requested_workers == 0 else requested_workers, available)
        self.cancel_event = cancel_event
        self.request = None
        self.frozen_request_identity: str | None = None
        self.children: list[_Child] = []
        self.events: Queue = Queue()
        self.outcomes: dict[int, EvaluationOutcome] = {}
        self.submitted: set[int] = set()
        self.finished: set[int] = set()
        self.started: set[int] = set()
        self.serial_invocations = self.fallback_tasks = 0
        self.serial_calls = self.serial_points = self.serial_unknown = 0
        self.worker_calls = self.worker_points = self.worker_incomplete = 0
        self.worker_elapsed = self.startup_seconds = self.initialization_seconds = self.ipc_seconds = 0.0
        self.ipc_bytes = self.parallel_batches = self.error_tasks = self.cancelled_tasks = 0
        self.fallback_events: list[str] = []
        self.selection_reasons: dict[str, int] = {}
        self.next_token = 0
        self.disabled = False
        self.closed = False
        self.temporary: TemporaryDirectory | None = None
        self.cancel_path: Path | None = None
        self.worker_records: list[dict] = []
        self.batch_decisions: list[dict] = []
        self.serial_observed_work = 0
        self.known_worker_cpu_seconds = 0.0
        self.unrecorded_worker_cpu_tasks = 0
        self.worker_cpu_by_pid: dict[int, _WorkerCPUCost] = {}
        self.serial_costs: dict[tuple[bool, bool], _SerialCost] = {}
        self._last_selection: dict[str, object] = {}
        self._selected_workers = 0

    def configure(self, request):
        if self.children or self.submitted:
            raise RuntimeError("cannot reconfigure a started frozen evaluation pool")
        self.request = deepcopy(request)
        self.frozen_request_identity = hashlib.sha256(
            pickle.dumps(self.request, protocol=pickle.HIGHEST_PROTOCOL)).hexdigest()

    def check_cancel(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise physical._StructureCancelled()

    def eligible(self, count: int, work: int, budget_allows: bool, *, phase: str = "",
                 worker_limit: int | None = None) -> bool:
        """Choose only from completed observations; never prepare or score work.

        Cold discovery uses the existing work threshold cumulatively across
        completed serial calls. It predicts neither future work nor speedup.
        Warm small batches compare measured serial cost with observed IPC cost;
        forward/Jacobian/zero-probe samples are kept distinct. This coarse proxy
        is local to one run and is not a hardware calibration certificate.
        """
        warm_workers = sum(child.ready for child in self.children)
        cap = min(self.workers, max(0, count),
                  self.workers if worker_limit is None else max(1, worker_limit))
        kind = {"needle": (True, True), "growth": (True, False),
                "structure": (False, False), "structure_global": (False, False),
                "global": (False, False), "hybrid_prior": (False, False),
                "hybrid_global": (False, False), "hybrid_needle": (True, True)}.get(phase)
        sample = self.serial_costs.get(kind) if kind is not None else None
        serial_estimate = (work * sample.seconds / sample.work
                           if sample is not None and sample.work > 0 else None)
        ipc_estimate = (count * self.ipc_seconds / len(self.finished) if self.finished else None)
        saved_estimate = (serial_estimate * (1 - 1 / min(cap, warm_workers))
                          if serial_estimate is not None and min(cap, warm_workers) >= 2 else None)
        target = cap
        if self.workers <= 1:
            reason = "requested_serial" if self.requested_workers == 1 else "available_cpu_serial"
        elif cap <= 1:
            reason = ("insufficient_candidates" if count < 2 else
                      "resource_guard_serial" if worker_limit == 1 else "insufficient_candidates")
        elif not budget_allows:
            reason = "finite_budget_serial"
        elif self.disabled or self.closed:
            reason = "pool_unavailable"
        elif count < 2:
            reason = "insufficient_candidates"
        elif work >= MIN_PARALLEL_WORK:
            reason = "warm_work_threshold" if warm_workers else "cold_work_threshold"
        elif warm_workers >= 2:
            target = min(cap, warm_workers)
            if saved_estimate is None or ipc_estimate is None:
                reason = "warm_insufficient_cost_samples"
            else:
                reason = "warm_measured_reuse" if saved_estimate > ipc_estimate else "warm_ipc_cost_serial"
        elif self.serial_observed_work + work >= MIN_PARALLEL_WORK:
            # A bounded discovery pool avoids auto-opening a large pool merely
            # because many prior small serial calls accumulated work.
            target = min(2, cap)
            reason = "cold_accumulated_work_discovery"
        else:
            reason = "cold_work_below_threshold"
        parallel = reason in {"cold_work_threshold", "warm_work_threshold",
                              "cold_accumulated_work_discovery", "warm_measured_reuse"}
        self._selected_workers = target if parallel else 0
        self.selection_reasons[reason] = self.selection_reasons.get(reason, 0) + 1
        self._last_selection = {
            "selection_reason": reason, "pool_temperature": "warm" if warm_workers else "cold",
            "requested_worker_limit": self.requested_workers, "available_cpu_limit": self.available_workers,
            "effective_worker_limit": self.workers, "candidate_worker_cap": cap,
            "resource_worker_limit": worker_limit,
            "ready_pool_workers": warm_workers, "selected_worker_cap": self._selected_workers,
            "cold_work_threshold": MIN_PARALLEL_WORK,
            "observed_serial_work": self.serial_observed_work,
            "cost_sample_kind": None if kind is None else {"jacobian": kind[0], "zero_probe": kind[1]},
            "serial_cost_samples": sample.samples if sample is not None else 0,
            "estimated_serial_seconds": serial_estimate, "estimated_ipc_seconds": ipc_estimate,
            "estimated_parallel_savings_before_ipc_seconds": saved_estimate,
            "estimate_scope": "run-local completed physical callbacks; point-times-layer proxy; no speedup guarantee",
        }
        return parallel

    def serial(self, callback: Callable, task: EvaluationTask, *, fallback=False):
        self.serial_invocations += 1
        self.fallback_tasks += int(fallback)
        started = time.perf_counter()
        try:
            value = callback()
        except BaseException:
            self.serial_unknown += 1
            raise
        elapsed = time.perf_counter() - started
        limit = min(10001, 1_000_000 // (len(task.design.get("front_layers", ())) +
                                       len(task.design.get("back_layers", ())) + 3))
        responses = value.get("responses", ())
        points = sum(len(row["wavelength_nm"]) for row in responses)
        self.serial_points += points
        work = points * max(1, len(task.design.get("front_layers", ())) + len(task.design.get("back_layers", ())))
        if work > 0 and math.isfinite(elapsed) and elapsed >= 0:
            sample = self.serial_costs.setdefault((task.jacobian, bool(task.zero_ids)), _SerialCost())
            sample.work += work
            sample.seconds += elapsed
            sample.samples += 1
            self.serial_observed_work += work
        self.serial_calls += sum(math.ceil(len(row["wavelength_nm"]) / limit) for row in responses)
        return value

    def _reader(self, index, stream):
        try:
            while True:
                value, decode_seconds, byte_count = _read_frame(stream)
                self.events.put((index, value, decode_seconds, byte_count))
        except BaseException as exc:
            self.events.put((index, {"kind": "reader_error", "message": f"{type(exc).__name__}: {exc}"}, 0., 0))

    def _writer(self, index, stream, commands, stop):
        try:
            while not stop.is_set():
                try:
                    command = commands.get(timeout=0.05)
                except Empty:
                    continue
                started = time.perf_counter()
                byte_count = _write_frame(stream, command)
                self.events.put((index, {"kind": "sent"}, time.perf_counter() - started, byte_count))
                if command.get("kind") == "close":
                    return
        except BaseException as exc:
            self.events.put((index, {"kind": "reader_error", "message":
                f"writer {type(exc).__name__}: {exc}"}, 0., 0))

    def _send(self, child, command):
        # A child that stops reading cannot block the parent cancellation loop.
        try:
            child.writer_queue.put_nowait(command)
        except Full as exc:
            raise InfrastructureFailure("bounded headless writer queue is full") from exc

    def _receive(self, timeout=0.05):
        try:
            index, reply, seconds, byte_count = self.events.get(timeout=timeout)
        except Empty:
            return
        self.ipc_seconds += seconds
        self.ipc_bytes += byte_count
        child = self.children[index]
        kind = reply.get("kind")
        if kind == "ready":
            if self.closed:
                return
            if reply.get("qt_imported"):
                raise InfrastructureFailure("headless synthesis child imported Qt")
            child.actual_pid = int(reply["pid"])
            if child.job is not None:
                child.actual_handle = child.job.verify_worker(child.actual_pid, reply["creation_time"])
            reply.update(wrapper_pid=child.process.pid, actual_worker_pid=child.actual_pid,
                         wrapper_creation_time=child.launcher_creation_time)
            child.ready = True
            self.initialization_seconds += float(reply.get("initialization_seconds", 0.))
            self.worker_records.append(reply)
        elif kind == "started":
            self.started.add(reply["token"])
        elif kind == "result":
            token = reply["token"]
            if token in self.finished:
                return
            self.finished.add(token)
            if child.pending == token:
                child.pending = None
            work = reply.get("work", {})
            self.worker_calls += int(work.get("physical_evaluations", 0))
            self.worker_points += int(work.get("wavelength_points", 0))
            self.worker_incomplete += int(work.get("incomplete_calls", 0))
            self.worker_elapsed += float(reply.get("elapsed_seconds", 0.))
            cpu_seconds = reply.get("cpu_seconds")
            measured_cpu: float | None = None
            if (isinstance(cpu_seconds, (int, float)) and not isinstance(cpu_seconds, bool)
                    and math.isfinite(cpu_seconds) and cpu_seconds >= 0):
                measured_cpu = float(cpu_seconds)
            if measured_cpu is not None:
                self.known_worker_cpu_seconds += measured_cpu
            else:
                self.unrecorded_worker_cpu_tasks += 1
            if child.actual_pid is not None:
                cpu_sample = self.worker_cpu_by_pid.setdefault(child.actual_pid, _WorkerCPUCost())
                cpu_sample.completed_tasks += 1
                if measured_cpu is not None:
                    cpu_sample.known_cpu_seconds += measured_cpu
                    cpu_sample.cpu_samples += 1
                else:
                    cpu_sample.unrecorded_completed_tasks += 1
            status = reply["status"]
            outcome = self.outcomes[token]
            outcome.worker_pid = child.actual_pid
            if status == "ok":
                if not outcome.discarded:
                    outcome.value = reply["value"]
            elif status == "cancelled":
                self.cancelled_tasks += 1
                outcome.error = physical._StructureCancelled()
            elif status == "physical_error":
                self.error_tasks += 1
                outcome.error = (ArithmeticError if reply.get("arithmetic") else ValueError)(reply["message"])
            else:
                self._infrastructure(reply.get("message", "worker infrastructure error"))
                outcome.infrastructure = True
                outcome.error = InfrastructureFailure(reply.get("message", "worker infrastructure error"))
        elif kind == "reader_error":
            if self.closed:
                return
            self._infrastructure(reply["message"])
            if child.pending is not None:
                outcome = self.outcomes[child.pending]
                outcome.infrastructure = True
                outcome.error = InfrastructureFailure(reply["message"])
                child.pending = None

    def _infrastructure(self, message):
        self.disabled = True
        if message not in self.fallback_events:
            self.fallback_events.append(str(message))

    def _ensure_pool(self, count=None):
        target = min(self.workers, self.workers if count is None else count)
        if len(self.children) >= target:
            return
        self.check_cancel()
        started = time.perf_counter()
        if self.temporary is None:
            self.temporary = TemporaryDirectory(prefix="coat-synthesis-workers-")
            self.cancel_path = Path(self.temporary.name) / "cancel"
        try:
            for index in range(len(self.children), target):
                self.check_cancel()
                stderr = TemporaryFile(mode="w+b")
                env = dict(os.environ, **THREAD_ENVIRONMENT)
                # Bind child imports even when PYTHONSAFEPATH omits cwd.
                # Preserve the rest of the user's path and safe-path choice.
                source_root = str(Path(__file__).resolve().parents[2])
                inherited_path = env.get("PYTHONPATH", "")
                env["PYTHONPATH"] = source_root + (os.pathsep + inherited_path if inherited_path else "")
                job = _WindowsWorkerJob() if os.name == "nt" else None
                try:
                    process = subprocess.Popen([sys.executable, "-m", "coat_sim.core.film_synthesis_parallel", "--worker"],
                        cwd=str(Path(__file__).resolve().parents[2]), env=env,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x4) if job is not None else 0,
                        start_new_session=job is None)
                except BaseException:
                    stderr.close()
                    if job is not None:
                        job.close()
                    raise
                reader = Thread(target=self._reader, args=(index, process.stdout),
                                name=f"synthesis-reader-{process.pid}", daemon=True)
                child = _Child(process, stderr, reader, job)
                self.children.append(child)
                if job is not None:
                    job.attach_and_resume(process)
                child.writer = Thread(target=self._writer, args=(index, process.stdin, child.writer_queue, child.writer_stop),
                    name=f"synthesis-writer-{process.pid}", daemon=True)
                reader.start()
                child.writer.start()
                self._send(child, {"request": self.request, "cancel_path": str(self.cancel_path),
                                   "frozen_request_identity": self.frozen_request_identity})
            deadline = started + STARTUP_TIMEOUT_SECONDS
            while not all(child.ready for child in self.children):
                self.check_cancel()
                self._receive()
                if self.disabled or time.monotonic() >= deadline:
                    raise InfrastructureFailure("headless worker ready handshake failed or timed out")
        except physical._StructureCancelled:
            self.close()
            raise
        except (OSError, EOFError, RuntimeError, pickle.PickleError) as exc:
            self._infrastructure(f"startup: {type(exc).__name__}: {exc}")
            self.close()
        finally:
            self.startup_seconds += time.perf_counter() - started

    def _submit(self, child, task):
        self.check_cancel()
        token = self.next_token
        self.next_token += 1
        outcome = EvaluationOutcome(token, worker_pid=child.actual_pid)
        self.outcomes[token] = outcome
        child.pending = token
        try:
            self._send(child, {"kind": "evaluate", "token": token, "design": task.design,
                               "jacobian": task.jacobian, "zero_ids": task.zero_ids,
                               "retain_responses": task.retain_responses})
            self.submitted.add(token)
        except (OSError, EOFError, RuntimeError, pickle.PickleError) as exc:
            self._infrastructure(f"send: {type(exc).__name__}: {exc}")
            outcome.infrastructure, outcome.error = True, InfrastructureFailure(str(exc))
            child.pending = None
        return outcome

    def _wait(self, outcome):
        while outcome.token not in self.finished and outcome.error is None:
            self.check_cancel()
            self._receive()
        return outcome

    def take(self, outcome):
        outcome.consumed = True
        if outcome.infrastructure:
            raise InfrastructureFailure(str(outcome.error))
        if outcome.error is not None:
            raise outcome.error
        value, outcome.value = outcome.value, None
        return value

    @contextmanager
    def batch(self, tasks: Iterable[EvaluationTask], *, count: int, work: int, budget_allows: bool, phase: str = "",
              worker_limit: int | None = None):
        decision = {"phase": phase, "candidate_count": count, "estimated_work": work, "budget_allows": budget_allows}
        self.batch_decisions.append(decision)
        parallel = self.eligible(count, work, budget_allows, phase=phase, worker_limit=worker_limit)
        decision.update(self._last_selection)
        decision["eligibility_reason"] = self._last_selection["selection_reason"]
        decision["pool_eligible"] = parallel
        startup_before = self.startup_seconds
        selected_workers = self._selected_workers
        if parallel:
            try:
                self._ensure_pool(selected_workers)
            except BaseException as exc:
                self.close()
                decision.update(startup_seconds_in_batch=self.startup_seconds - startup_before,
                    parallel_selected=False, backend="not_dispatched", effective_workers=0,
                    submitted_tasks=0, completed_tasks=0, discarded_tasks=0, worker_pids=[],
                    owned_worker_pids=[child.actual_pid for child in self.children if child.actual_pid is not None],
                    launcher_pids=[child.process.pid for child in self.children],
                    startup_state="cancelled" if isinstance(exc, physical._StructureCancelled) else "failed",
                    startup_error=f"{type(exc).__name__}: {exc}")
                raise
            parallel = not (self.disabled or self.closed)
            if not parallel:
                decision["selection_reason"] = "startup_unavailable_serial"
        decision["startup_seconds_in_batch"] = self.startup_seconds - startup_before
        decision["parallel_selected"] = parallel
        tokens: list[int] = []

        def ordered():
            iterator = iter(tasks)
            if not parallel:
                for task in iterator:
                    yield task, None
                return
            self.parallel_batches += 1
            pending: deque = deque()
            tail: Iterable[EvaluationTask] = ()
            for child in self.children[:selected_workers]:
                try:
                    task = next(iterator)
                except StopIteration:
                    break
                outcome = self._submit(child, task)
                tokens.append(outcome.token)
                pending.append((child, task, outcome))
            while pending:
                child, task, outcome = pending.popleft()
                yield task, None if outcome is None else self._wait(outcome)
                try:
                    next_task = next(iterator)
                except StopIteration:
                    continue
                if self.disabled:
                    # Keep the unsubmitted tail lazy, after all prior ordinals.
                    tail = chain((next_task,), iterator)
                    iterator = iter(())
                else:
                    next_outcome = self._submit(child, next_task)
                    tokens.append(next_outcome.token)
                    pending.append((child, next_task, next_outcome))
            for remaining in tail:
                yield remaining, None
        generator = ordered()
        try:
            yield generator
        finally:
            generator.close()
            discarded = False
            for token in tokens:
                outcome = self.outcomes[token]
                if not outcome.consumed:
                    outcome.discarded = True
                    outcome.value = None
                    discarded = True
            if discarded:
                self.close()
            decision.update(submitted_tasks=len(tokens),
                completed_tasks=sum(token in self.finished for token in tokens),
                discarded_tasks=sum(self.outcomes[token].discarded for token in tokens),
                worker_pids=sorted({pid for token in tokens
                                    if (pid := self.outcomes[token].worker_pid) is not None}),
                backend="headless_process" if tokens else "serial",
                effective_workers=len({self.outcomes[token].worker_pid for token in tokens
                                       if self.outcomes[token].worker_pid is not None}) if tokens else 1)

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.cancel_path is not None:
            self.cancel_path.touch(exist_ok=True)
        for child in self.children:
            if child.process.poll() is None:
                try:
                    self._send(child, {"kind": "close"})
                except (OSError, EOFError, RuntimeError, pickle.PickleError):
                    pass
        deadline = time.monotonic() + CLOSE_GRACE_SECONDS
        while any(child.process.poll() is None for child in self.children) and time.monotonic() < deadline:
            self._receive(timeout=0.02)
        for child in self.children:
            # An already-dead child cannot receive a close frame, but its idle
            # writer must still wake independently of pipe/process liveness.
            child.writer_stop.set()
            if child.job is not None:
                child.job.close()
            elif child.process.poll() is None:
                child.process.terminate()
        close_error: Exception | None = None
        for child in self.children:
            try:
                child.process.wait(timeout=1.)
            except subprocess.TimeoutExpired:
                child.process.kill()
                child.process.wait(timeout=1.)
            if child.reader.ident is not None:
                child.reader.join(timeout=1.)
            if child.writer is not None and child.writer.ident is not None:
                child.writer.join(timeout=1.)
            if child.actual_handle is not None:
                child.actual_dead = child.job.kernel.WaitForSingleObject(child.actual_handle, 1000) == 0
                child.job.kernel.CloseHandle(child.actual_handle)
                child.actual_handle = None
            else:
                child.actual_dead = child.process.poll() is not None
            for stream in (child.process.stdin, child.process.stdout, child.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception as exc:
                        # A buffered stdin can flush after its owned reader has
                        # exited. FileIO reports EPIPE on POSIX, EINVAL on Windows.
                        # Ignore only this released, dead-child stdin; defer other
                        # errors until the remaining owned resources are closed.
                        dead_stdin = (stream is child.process.stdin and child.process.poll() is not None
                                      and stream.closed)
                        pipe_flush = (isinstance(exc, OSError) and
                                      (exc.errno == errno.EPIPE or os.name == "nt" and exc.errno == errno.EINVAL))
                        if not (dead_stdin and pipe_flush) and close_error is None:
                            close_error = exc
        while not self.events.empty():
            self._receive(timeout=0.)
        if self.temporary is not None:
            self.temporary.cleanup()
        if close_error is not None:
            raise close_error

    def diagnostics(self):
        unknown = len(self.submitted - self.finished)
        point_unknown = unknown + self.worker_incomplete + self.serial_unknown
        call_unknown = unknown + self.serial_unknown
        return {"version": 1, "recorded": True, "backend": "headless_process" if self.submitted else "serial",
            "requested_workers": self.requested_workers, "effective_workers": len(self.children) if self.submitted else 1,
            "worker_pids": [child.actual_pid for child in self.children if child.actual_pid is not None],
            "launcher_pids": [child.process.pid for child in self.children],
            "owned_worker_count": len(self.children),
            "ready_worker_count": sum(child.actual_pid is not None for child in self.children),
            "workers_with_submitted_tasks": len({outcome.worker_pid for token, outcome in self.outcomes.items()
                if token in self.submitted and outcome.worker_pid is not None}),
            "worker_pid_scope": "owned ready-handshake PIDs may include startup-only idle workers; per-stage worker_pids identify dispatched physical tasks",
            "active_worker_pids": [child.actual_pid for child in self.children if child.actual_pid is not None and not child.actual_dead],
            "cleanup_complete": self.closed and all(child.actual_dead and child.process.poll() is not None
                and not child.reader.is_alive() and (child.writer is None or not child.writer.is_alive())
                for child in self.children),
            "execution_state": "closed" if self.closed else "live", "actual_work_complete": point_unknown == 0,
            "parallel_batches": self.parallel_batches, "submitted_tasks": len(self.submitted),
            "started_tasks": len(self.started), "completed_tasks": len(self.finished),
            "consumed_tasks": sum(outcome.consumed for outcome in self.outcomes.values()),
            "discarded_tasks": sum(outcome.discarded for outcome in self.outcomes.values()),
            "error_tasks": self.error_tasks, "cancelled_tasks": self.cancelled_tasks, "unknown_tasks": unknown,
            "serial_fallback_tasks": self.fallback_tasks, "fallback_events": list(self.fallback_events),
            "serial_evaluator_invocations": self.serial_invocations,
            "worker_evaluator_invocations": None if unknown else len(self.started),
            "training_evaluator_invocations": None if unknown else self.serial_invocations + len(self.started),
            "known_worker_evaluator_invocations": len(self.started),
            "known_training_evaluator_invocations": self.serial_invocations + len(self.started),
            "training_physical_evaluations": None if call_unknown else self.serial_calls + self.worker_calls,
            "training_wavelength_points": None if point_unknown else self.serial_points + self.worker_points,
            "known_training_physical_evaluations": self.serial_calls + self.worker_calls,
            "known_training_wavelength_points": self.serial_points + self.worker_points,
            "worker_training_physical_evaluations": None if unknown else self.worker_calls,
            "worker_training_wavelength_points": None if unknown + self.worker_incomplete else self.worker_points,
            "incomplete_physical_calls": self.worker_incomplete, "serial_unknown_work": self.serial_unknown,
            "startup_seconds": self.startup_seconds, "initialization_seconds": self.initialization_seconds,
            "ipc_seconds": self.ipc_seconds, "ipc_bytes": self.ipc_bytes, "worker_compute_seconds": self.worker_elapsed,
            "worker_cpu_seconds": (None if unknown or self.unrecorded_worker_cpu_tasks
                                   else self.known_worker_cpu_seconds),
            "known_worker_cpu_seconds": self.known_worker_cpu_seconds,
            "unrecorded_worker_cpu_tasks": self.unrecorded_worker_cpu_tasks,
            "worker_cpu_by_pid": [dict(pid=pid, completed_tasks=sample.completed_tasks,
                known_cpu_seconds=sample.known_cpu_seconds, cpu_samples=sample.cpu_samples,
                unrecorded_completed_tasks=sample.unrecorded_completed_tasks)
                for pid, sample in sorted(self.worker_cpu_by_pid.items())],
            "worker_cpu_scope": "process_time deltas inside physical evaluator; received replies only; unreceived work unknown; excludes startup/IPC",
            "selection_reasons": dict(self.selection_reasons), "batch_decisions": list(self.batch_decisions),
            "adaptive_parallel": {
                "cold_work_threshold": MIN_PARALLEL_WORK, "discovery_worker_cap": 2,
                "observed_serial_work": self.serial_observed_work,
                "serial_cost_samples": [dict(jacobian=jacobian, zero_probe=zero_probe,
                    samples=sample.samples, work=sample.work, seconds=sample.seconds)
                    for (jacobian, zero_probe), sample in self.serial_costs.items()],
                "policy": "cold threshold or accumulated-work discovery; warm measured savings exceed IPC",
                "limitations": "point-times-layer proxy; finite samples; no future-work prediction or speedup guarantee",
            },
            "workers": list(self.worker_records),
            "frozen_request_identity": self.frozen_request_identity,
            "prepared_optics_policy": "one independently validated immutable material context per worker; no candidate cache",
            "counter_scope": "training_evaluations_only_audit_reported_separately"}


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("This private headless module only accepts --worker")
    _worker_main()
