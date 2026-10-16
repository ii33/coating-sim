"""One-shot desktop child for authenticated remote CPU appearance jobs.

The job file contains a host *ID*, never a URL or token.  This child resolves
the configured host and OS keyring entry in its own application interpreter.
Only a small path-free request and a SHA-bound .coatasset cross the wire.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time

from coat_sim.appearance.limits import MAX_APPEARANCE_CONTROL_BYTES
from coat_sim.scheduler.jobs import JobSpec, canonical


_JOB = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _emit(kind: str, stage: str, message: str, **fields) -> None:
    value = {"type": kind, "stage": stage, "message": message[:1000], **fields}
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False), flush=True)


def _safe_error(error: BaseException) -> str:
    code = getattr(error, "code", "")
    if not isinstance(code, str) or not _SAFE_CODE.fullmatch(code):
        code = "appearance_remote_failed"
    return code


def _duration(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return "估算中"
    minutes, seconds = divmod(int(round(value)), 60)
    return f"{minutes} 分 {seconds:02d} 秒" if minutes else f"{seconds} 秒"


def _progress(row: dict) -> None:
    progress = row.get("progress") if isinstance(row, dict) else None
    progress = progress if isinstance(progress, dict) else {}
    stage = str(progress.get("stage") or row.get("state") or "remote")[:96]
    state = row.get("state")
    text = {
        "queued": "远端队列已接收；等待执行。",
        "running": "远端任务运行中。",
        "cancelled": "远端已确认取消。",
        "failed": "远端渲染失败。",
        "succeeded": "远端渲染完成，正在校验并下载结果。",
    }.get(state, "正在核对远端任务状态。")
    if state == "running":
        completed = progress.get("completed_wavelengths")
        total = progress.get("total_wavelengths")
        if type(completed) is int and type(total) is int and 0 <= completed <= total and total > 0:
            text = f"远端波长 {completed}/{total} · 已用 {_duration(progress.get('elapsed_s'))}"
        if progress.get("estimated_remaining_s") is not None:
            text += f" · 预计剩余约 {_duration(progress['estimated_remaining_s'])}"
    fields = {}
    for key in ("fraction", "elapsed_s", "estimated_remaining_s",
                "completed_wavelengths", "total_wavelengths", "estimate_kind"):
        value = progress.get(key)
        if value is not None and (isinstance(value, str) or
                                  (isinstance(value, (int, float)) and not isinstance(value, bool)
                                   and math.isfinite(value))):
            fields[key] = value
    _emit("status", stage, text, **fields)


def _settings_client(root: Path, host_id: str, device_id: str):
    from coat_sim.ui.settings import load_ui_settings
    from coat_sim.ui.assistant_credentials import read_compute_token
    from coat_sim.scheduler.remote_client import make_remote_client

    settings = load_ui_settings()
    url = str(settings.get("compute_server_url") or "").strip()
    if not url or "remote:" + hashlib.sha256(url.encode()).hexdigest()[:24] != host_id:
        raise ValueError("configured_remote_host_changed")
    token = read_compute_token(url, strict=True)
    if not token:
        raise ValueError("remote_token_unavailable")
    tls = settings.get("compute_server_tls") is not False
    client = make_remote_client(
        url, root, token=token,
        ca_file=str(settings.get("compute_server_ca_file") or "") or None if tls else None,
        allow_insecure_http=not tls, device_id=device_id)
    return client


def _read_input(input_path: Path, job_dir: Path) -> dict:
    if (not input_path.is_file() or input_path.stat().st_size > MAX_APPEARANCE_CONTROL_BYTES
            or input_path.parent.resolve(strict=True) != job_dir.resolve(strict=True)):
        raise ValueError("invalid_appearance_remote_input")
    value = json.loads(input_path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict)
            or set(value) - {"origin"} != {"schema", "version", "job_id", "input_sha256",
                              "context_sha256", "context", "render_inputs",
                              "backend", "precision", "geometry_thickness_mode",
                              "geometry_thickness_mm", "host_id", "device_id"}
            or value.get("schema") != "coat_sim.appearance_remote_worker_input"
            or value.get("version") != 1
            or not isinstance(value.get("job_id"), str) or not _JOB.fullmatch(value["job_id"])
            or not isinstance(value.get("input_sha256"), str) or not _SHA.fullmatch(value["input_sha256"])
            or not isinstance(value.get("context_sha256"), str) or not _SHA.fullmatch(value["context_sha256"])
            or not isinstance(value.get("context"), dict)
            or hashlib.sha256(canonical(value["context"]).encode()).hexdigest() != value["context_sha256"]
            or not isinstance(value.get("render_inputs"), dict)
            or (value.get("backend"), value.get("precision"), value.get("device_id")) not in (
                ("cpu", "native", "cpu:default"),
                ("gpu_cuda", "mixed", "cuda:default"),
                ("gpu_cuda", "float32", "cuda:default"),
            )
            or not isinstance(value.get("host_id"), str) or not value["host_id"].startswith("remote:")):
        raise ValueError("invalid_appearance_remote_input")
    origin = value.get("origin")
    if "origin" in value:
        if (not isinstance(origin, dict)
                or not {"project_id", "project_generation", "project_root",
                        "configuration_id", "operation_id"}.issubset(origin)
                or set(origin) - {"surface", "project_id", "project_generation", "project_root",
                                  "configuration_id", "operation_id"}
                or not isinstance(origin.get("project_id"), str)
                or _JOB.fullmatch(origin["project_id"]) is None
                or not isinstance(origin.get("project_generation"), str)
                or _JOB.fullmatch(origin["project_generation"]) is None
                or origin.get("operation_id") != value["job_id"]
                or (origin.get("configuration_id") is not None
                    and (not isinstance(origin["configuration_id"], str)
                         or _JOB.fullmatch(origin["configuration_id"]) is None))
                or ("project_root" in origin
                    and (not isinstance(origin["project_root"], str)
                         or not origin["project_root"] or "\x00" in origin["project_root"]))
                or ("surface" in origin
                    and (not isinstance(origin["surface"], str) or len(origin["surface"]) > 128))):
            raise ValueError("invalid_appearance_project_origin")
    return value


def _public_request(value: dict) -> dict:
    request = {"render_inputs": value["render_inputs"],
               "backend": value["backend"], "precision": value["precision"],
               "geometry_thickness_mode": value["geometry_thickness_mode"],
               "display_exposure": 0.01}
    if request["geometry_thickness_mode"] == "explicit_override":
        request["geometry_thickness_mm"] = value["geometry_thickness_mm"]
    elif request["geometry_thickness_mode"] != "film_design_snapshot":
        raise ValueError("invalid_appearance_geometry_mode")
    return request


def _write_completion(job_dir: Path, value: dict, index: dict, rendered: dict) -> None:
    render_dir = Path(rendered["output_dir"])
    image, manifest = render_dir / "display.png", render_dir / "manifest.json"
    completion = {
        "schema": "coat_sim.appearance_result", "job_id": value["job_id"],
        "input_sha256": value["input_sha256"], "backend": value["backend"],
        "precision": value["precision"],
        "render_input_sha256": index["input_sha256"],
        "render_manifest": "render/manifest.json", "render_manifest_sha256": _sha_file(manifest),
        "image": "render/display.png", "image_sha256": _sha_file(image),
        "width": index["resolution"]["width"], "height": index["resolution"]["height"],
        "wavelength_count": len(index["wavelength_nm"]),
    }
    from coat_sim.scheduler.io import write_json_new
    write_json_new(job_dir / "appearance_result.json", completion,
                   max_bytes=1_000_000, indent=2, final_newline=True)


def run(input_path: Path, job_dir: Path, cancel_marker: Path) -> int:
    job_id = None
    try:
        value = _read_input(input_path, job_dir)
        job_id = value["job_id"]
        if cancel_marker.exists():
            _emit("status", "cancelled", "远端提交前已取消。")
            return 3
        context = value["context"]
        root = Path(context["asset_root"]).resolve(strict=True)
        source = Path(context["asset_path"]).resolve(strict=True)
        if (not source.is_relative_to(root / "data") or source.suffix.lower() != ".coatasset"
                or not isinstance(context.get("asset_sha256"), str)
                or not _SHA.fullmatch(context["asset_sha256"])
                or _sha_file(source) != context["asset_sha256"]):
            raise ValueError("appearance_source_asset_mismatch")
        client = _settings_client(root, value["host_id"], value["device_id"])
        from coat_sim.ui.compute_target import validate_capabilities
        capability = client.capabilities()
        rows = validate_capabilities(capability, "appearance_render", remote=True)
        expected_backend = "CPU" if value["backend"] == "cpu" else "gpu_cuda"
        if not any(row["id"] == value["device_id"] and row["available"]
                   and row["backend"] == expected_backend for row in rows):
            raise ValueError("appearance_remote_device_unavailable")
        _emit("status", "uploading", "正在上传已校验的工艺资产。")
        descriptor = client.transport.register_appearance_asset(source)
        if (not isinstance(descriptor, dict)
                or descriptor.get("sha256") != context["asset_sha256"]
                or descriptor.get("role") != "appearance_bundle"
                or descriptor.get("name") != "input.coatasset"
                or type(descriptor.get("bytes")) is not int
                or descriptor["bytes"] != source.stat().st_size
                or not isinstance(descriptor.get("artifact_id"), str)
                or _SHA.fullmatch(descriptor["artifact_id"]) is None):
            raise ValueError("appearance_uploaded_asset_mismatch")
        if cancel_marker.exists():
            _emit("status", "cancelled", "远端提交前已取消。")
            return 3
        spec = JobSpec("appearance_render", {"request": _public_request(value)}, {
            "appearance_artifact": {"artifact_id": descriptor["artifact_id"],
                                    "sha256": descriptor["sha256"]},
            "device_binding": {"schema_version": 1, "device_id": value["device_id"]},
        })
        portable_spec_sha256 = spec.fingerprint
        _emit("status", "submitting", "正在提交远端外观任务；资产冻结可能需要一些时间。")
        try:
            if "origin" in value:
                receipt = client.submit(spec.to_dict(), job_id=job_id,
                                        metadata={"origin": value["origin"]})
            else:
                receipt = client.submit(spec.to_dict(), job_id=job_id)
        except Exception:
            # A timeout after POST is an unknown admission state.  Keep the
            # exact ID for reconciliation; never mint a replacement request.
            try:
                receipt = client.status(job_id)
                if (receipt.get("kind") != "appearance_render"
                        or receipt.get("requested_device_id") != value["device_id"]
                        or receipt.get("input_summary", {}).get("external_spec_sha256")
                           != portable_spec_sha256):
                    raise ValueError("remote_submission_identity_unknown")
            except Exception as exc:
                raise ValueError("remote_submission_state_unknown") from exc
        if (not isinstance(receipt, dict) or receipt.get("job_id") != job_id
                or receipt.get("kind") != "appearance_render"
                or receipt.get("requested_device_id") != value["device_id"]
                or receipt.get("input_summary", {}).get("external_spec_sha256")
                   != portable_spec_sha256):
            raise ValueError("appearance_remote_receipt_mismatch")
        _emit("status", "queued", "远端任务已接收，正在等待队列执行。")
        cancel_sent = False
        last = None
        failures = 0
        while True:
            try:
                if cancel_marker.exists() and not cancel_sent:
                    cancelled = client.cancel(job_id)
                    cancel_sent = bool(cancelled.get("cancel_requested"))
                    _emit("status", "cancelling", "已请求远端取消，等待服务端确认终态。")
                row = client.status(job_id)
                failures = 0
            except Exception:
                failures += 1
                if failures >= 3:
                    raise ValueError("remote_job_state_unknown")
                _emit("status", "reconnecting", "远端连接中断；保留任务 ID，正在重连核对状态。")
                time.sleep(2)
                continue
            if (not isinstance(row, dict) or row.get("job_id") != job_id
                    or row.get("kind") != "appearance_render"
                    or row.get("requested_device_id") != value["device_id"]
                    or row.get("input_summary", {}).get("external_spec_sha256")
                       != portable_spec_sha256):
                raise ValueError("appearance_remote_status_mismatch")
            if row != last:
                _progress(row)
                last = row
            if row.get("state") in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.5)
        if row["state"] == "cancelled":
            return 3
        if cancel_marker.exists():
            # A cancel arriving after terminal success does not authorize a
            # completed wrapper in this local UI attempt.
            return 3
        if row["state"] != "succeeded":
            raise ValueError("appearance_remote_job_failed")
        if row.get("executed_device_id") != value["device_id"]:
            raise ValueError("appearance_remote_actual_device_unverified")
        _emit("status", "downloading", "远端完成，正在逐项校验并下载结果。")
        remote_result = client.result(job_id)
        from coat_sim.scheduler.remote_appearance import materialize_appearance_result
        rendered = materialize_appearance_result(client, job_id, remote_result,
                                                  job_dir / "render")
        if cancel_marker.exists():
            return 3
        index = json.loads(Path(rendered["index_path"]).read_text(encoding="utf-8"))
        _write_completion(job_dir, value, index, rendered)
        _emit("status", "complete", "远端图像及全部结果摘要已校验完成。", fraction=1.0)
        return 0
    except Exception as exc:
        code = _safe_error(exc)
        if code == "appearance_remote_failed" and isinstance(exc, ValueError):
            candidate = str(exc).split(":", 1)[0]
            if _SAFE_CODE.fullmatch(candidate):
                code = candidate
        _emit("error", "failed", code,
              **({"job_id": job_id} if job_id is not None else {}))
        return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--job-dir", required=True, type=Path)
    parser.add_argument("--cancel-marker", required=True, type=Path)
    args = parser.parse_args(argv)
    return run(args.input, args.job_dir, args.cancel_marker)


if __name__ == "__main__":
    raise SystemExit(main())
