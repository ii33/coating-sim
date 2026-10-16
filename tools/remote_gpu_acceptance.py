#!/usr/bin/env python3
"""Run one bounded, loopback-only remote CUDA appearance acceptance check.

Run from a Python environment containing the application remote-service
requirements and point --runtime-python at the project's validated Mitsuba /
Dr.Jit runtime. All service state, input assets, downloads, and caches live in
an owned temporary directory that is removed on exit. The script never starts
an external service or writes into the source checkout.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from unittest.mock import patch


DEVICE_ID = "cuda:default"
EXPECTED_ARTIFACTS = {
    "appearance-result.json", "display.png", "manifest.json",
    "radiance_spectrum.npy", "xyz.npy", "linear_srgb.npy", "input-job.json",
}
RENDER_TIMEOUT_S = 300
CANCEL_TIMEOUT_S = 180
START_TIMEOUT_S = 20


class RuntimeFactsError(RuntimeError):
    """A safe preflight code plus private child output for local diagnosis."""

    def __init__(self, code: str, completed: subprocess.CompletedProcess[bytes]):
        super().__init__(code)
        self.code = code
        self.stdout = completed.stdout or b""
        self.stderr = completed.stderr or b""
        self.diagnostic = {
            "stage": "runtime_facts", "child_returncode": completed.returncode,
            "stdout_bytes": len(self.stdout), "stderr_bytes": len(self.stderr),
            "stdout_sha256": hashlib.sha256(self.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(self.stderr).hexdigest(),
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _error_code(exc: BaseException) -> str:
    value = getattr(exc, "code", None)
    if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,79}", value):
        return value
    value = str(exc).split(":", 1)[0]
    if re.fullmatch(r"[a-z][a-z0-9_]{1,79}", value):
        return value
    return "acceptance_failed"


def _safe_progress(value) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("stage", "status"):
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", item):
            result[key] = item
    for key in ("fraction", "completed_wavelengths", "total_wavelengths",
                "estimated_remaining_s"):
        item = value.get(key)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            result[key] = item
    return result


def _safe_process_env(temp_root: Path) -> None:
    """Keep only OS/driver variables, and redirect caches to the owned root."""
    keep = {"PATH", "SYSTEMROOT", "WINDIR", "HOMEDRIVE", "HOMEPATH", "COMSPEC",
            "LD_LIBRARY_PATH", "LD_PRELOAD", "DYLD_LIBRARY_PATH"}
    safe = {key: value for key, value in os.environ.items()
            if key.upper() in keep or key.upper().startswith(("CUDA_", "NVIDIA_"))}
    os.environ.clear()
    os.environ.update(safe)
    home = temp_root / "home"
    cache = temp_root / "cache"
    home.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "TMP": str(temp_root), "TEMP": str(temp_root), "TMPDIR": str(temp_root),
        "MPLCONFIGDIR": str(cache / "matplotlib"),
        "XDG_CACHE_HOME": str(cache / "xdg"),
        "CUDA_CACHE_PATH": str(cache / "cuda"),
        "PYTHONNOUSERSITE": "1",
    })
    for name in ("MPLCONFIGDIR", "XDG_CACHE_HOME", "CUDA_CACHE_PATH"):
        Path(os.environ[name]).mkdir(parents=True, exist_ok=True)


def _runtime_facts(interpreter: Path, source_root: Path, env: dict[str, str]) -> dict:
    code = r'''import importlib.metadata, json, mitsuba as mi, drjit
print(json.dumps({"mitsuba": importlib.metadata.version("mitsuba"),
                  "drjit": importlib.metadata.version("drjit"),
                  "variants": sorted(mi.variants())}, separators=(",", ":")))'''
    child_env = dict(env)
    child_env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [str(interpreter), "-B", "-s", "-P", "-c", code], cwd=str(source_root), env=child_env,
        stdin=subprocess.DEVNULL, capture_output=True,
        timeout=30, check=False)
    if completed.returncode != 0:
        raise RuntimeFactsError("appearance_runtime_import_failed", completed)
    if len(completed.stdout or b"") > 8192:
        raise RuntimeFactsError("appearance_runtime_stdout_oversize", completed)
    try:
        facts = json.loads((completed.stdout or b"").strip().splitlines()[-1].decode("ascii"))
    except (UnicodeError, ValueError, IndexError) as exc:
        raise RuntimeFactsError("appearance_runtime_facts_invalid", completed) from exc
    if not isinstance(facts, dict):
        raise RuntimeFactsError("appearance_runtime_facts_invalid", completed)
    if (facts.get("mitsuba") != "3.9.0" or facts.get("drjit") != "1.4.0"
            or "cuda_ad_rgb" not in facts.get("variants", [])):
        raise RuntimeFactsError("appearance_runtime_versions_or_variant_mismatch", completed)
    return {"mitsuba": facts["mitsuba"], "drjit": facts["drjit"],
            "cuda_ad_rgb_variant_installed": True}


def _link_runtime(service_root: Path, interpreter: Path) -> Path:
    """Expose the validated venv at the service's normal discovery location.

    Only the private temporary root is modified. On Windows, use a directory
    symlink when available, then a local NTFS junction without changing any
    machine policy. The target venv remains read-only from this helper.
    """
    if interpreter.parent.name.casefold() == "scripts":
        relative_python = Path("Scripts") / "python.exe"
    elif interpreter.parent.name.casefold() == "bin":
        relative_python = Path("bin") / "python"
    else:
        raise RuntimeError("runtime_interpreter_layout_invalid")
    # Keep the caller's virtual-environment directory spelling here. Its
    # python executable may itself be a symlink (the isolated 007 runtime
    # intentionally uses this arrangement).
    environment_root = interpreter.parent.parent.absolute()
    if not environment_root.is_dir() or not (environment_root / relative_python).is_file():
        raise RuntimeError("runtime_environment_missing")
    destination = service_root / "build" / "mitsuba_appearance_env"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("runtime_link_destination_not_empty")
    is_junction = False
    try:
        os.symlink(environment_root, destination, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise RuntimeError("runtime_link_creation_failed") from None
        if any(char in str(value) for value in (destination, environment_root)
               for char in '&|<>^%!' + '"\r\n'):
            raise RuntimeError("runtime_link_path_unsupported") from None
        command = f'mklink /J "{destination}" "{environment_root}"'
        try:
            completed = subprocess.run(
                [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", command],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("runtime_junction_creation_failed") from None
        if completed.returncode != 0:
            raise RuntimeError("runtime_junction_creation_failed")
        is_junction = True
    expected = destination / relative_python
    if not destination.is_dir() or expected.resolve(strict=True) != interpreter.resolve(strict=True):
        try:
            os.rmdir(destination) if is_junction else destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError("runtime_link_target_mismatch")
    return destination, expected, is_junction, environment_root


def _remove_runtime_link(link: Path, is_junction: bool, target: Path) -> None:
    """Remove only the private link itself; never recurse into its target."""
    try:
        os.rmdir(link) if is_junction else link.unlink(missing_ok=True)
    finally:
        if not target.is_dir():
            raise RuntimeError("runtime_target_disappeared")


def _synthetic_asset(root: Path) -> Path:
    """Make a tiny v7 test asset through the project's normal serializer."""
    import numpy as np
    from coat_sim.core import film_process_import, simulation_assets
    from coat_sim.core.film_design import new_design, validate_design
    from coat_sim.core.film_design_service import simulation_settings
    from coat_sim.core.film_process_import import (
        FROZEN_KIND, FROZEN_SCHEMA_VERSION, build_film_process_import,
        validate_frozen_design_snapshot,
    )
    from coat_sim.core.optical_constants import validate_optical_set
    from coat_sim.core.process_profiles import validate_process_profile

    substrate_profile = {
        "schema_version": 1, "name": "Synthetic acceptance substrate",
        "surface": {"kind": "flat"},
        "outline": {"kind": "rectangle", "width_mm": 2.0, "height_mm": 2.0},
    }
    design = new_design("Synthetic appearance asset", "acceptance-fixture")
    design["substrate"].update({
        "material_id": "acceptance-glass", "optical_set_id": "acceptance-glass-index",
        "thickness": {"value": 1.0, "unit": "mm"},
    })
    design["front_layers"] = [{
        "id": "acceptance-layer", "material_id": "acceptance-film",
        "optical_set_id": "acceptance-film-index",
        "thickness": {"value": 100.0, "unit": "nm"},
        "note": "Synthetic acceptance-only layer",
    }]
    design = validate_design(design)

    def optical(identifier: str, index: float) -> dict:
        return validate_optical_set({
            "id": identifier, "name": "Synthetic " + identifier,
            "kind": "tabulated", "wavelength_unit": "nm",
            "wavelength_min": 500.0, "wavelength_max": 600.0,
            "wavelengths": [500.0, 600.0], "n": [index, index], "k": [0.0, 0.0],
        })

    snapshot = {
        "schema_version": FROZEN_SCHEMA_VERSION, "kind": FROZEN_KIND,
        "design": design, "settings": simulation_settings(),
        "materials": [
            {"id": "acceptance-glass", "name": "Synthetic glass",
             "optical_sets": [optical("acceptance-glass-index", 1.5)],
             "notes": "Synthetic acceptance fixture"},
            {"id": "acceptance-film", "name": "Synthetic film",
             "optical_sets": [optical("acceptance-film-index", 1.9)],
             "notes": "Synthetic acceptance fixture"},
        ],
    }
    snapshot["sha256"] = film_process_import._sha256(snapshot)
    snapshot = validate_frozen_design_snapshot(snapshot)
    machine = {"sources": [{"file": "synthetic-source.csource",
                            "profile": {"kind": "point"}}]}
    imported = build_film_process_import(snapshot, "front", {"acceptance-film": 1}, machine)
    process = validate_process_profile({
        "schema_version": 7, "name": "Synthetic acceptance process",
        "machine_file": "synthetic-machine.cmachine",
        "substrate_file": "synthetic-substrate.csubstrate",
        **{key: imported[key] for key in (
            "steps", "film_design_snapshot", "film_design_import")},
    })
    field = np.full((2, 2), 100.0, dtype=np.float64)
    step_id = "synthetic-acceptance-step"
    field_manifest = {
        "schema_version": 1, "captured": True, "field_dtype": "float64",
        "field_units": "nm", "field_shape": [2, 2],
        "coordinates": {"frame": "substrate_local_xy", "x_array": "x",
                         "y_array": "y", "unit": "mm"},
        "material_coordinate": {"axis": "substrate_normal",
                                 "origin": "original_substrate_front",
                                 "positive_direction": "outward", "unit": "nm"},
        "layer_value_semantics": "per_step_growth_increment; cumulative_ordered_sum_is_interface_offset",
        "sequence_order": "process_recipe_order",
        "stack_order": "first_step_is_substrate_adjacent_then_later_steps_overcoat",
        "contributor_semantics": "included_in_total_field",
        "valid_mask": {"semantics": "finite_nodes_are_valid; outside_domain_is_nan",
                       "array_source": "layer_field_*"},
        "contribution_source_slot": None,
        "fields": [{"step_id": step_id, "array_key": "layer_field_00001",
                    "step_index": 1, "source_slot": 1, "target_nm": 100.0,
                    "included_in_total": True, "completed": True,
                    "layer_id": "acceptance-layer", "surface": "front",
                    "material_id": "acceptance-film"}],
    }
    step_result = {"step_id": step_id, "step_index": 1, "source_slot": 1,
                   "target_nm": 100.0, "completed": True,
                   "layer_id": "acceptance-layer", "surface": "front",
                   "material_id": "acceptance-film"}
    result = {
        "field": field, "field_nm": field,
        "x": np.asarray([-1.0, 1.0], dtype=np.float64),
        "y": np.asarray([-1.0, 1.0], dtype=np.float64),
        "layer_fields": {step_id: field.copy()},
        "layer_field_manifest": field_manifest,
        "step_results": [step_result],
        "effective_settings": {"substrate_profile": substrate_profile},
        "device": "CPU", "algorithm": "synthetic acceptance fixture",
    }
    with patch("coat_sim.core.result_visualization.render_result_visualizations",
               return_value={"files": []}):
        prepared = simulation_assets.prepare_simulation_asset(
            root, result=result, settings={"substrate_profile": substrate_profile},
            machine=machine, process=process, elapsed_s=0.0,
            requested_device="CPU", record_history=False)
    target = root / "data" / "assets" / "synthetic-appearance.coatasset"
    target.parent.mkdir(parents=True, exist_ok=True)
    simulation_assets.save_simulation_asset(
        root, prepared["run_id"], "synthetic appearance", confirmed=True,
        path=target, overwrite=False, include_results=True)
    return target


def _render_inputs(width: int, height: int, spp: int) -> dict:
    return {
        "wavelength_nm": [500.0, 600.0],
        "camera": {"position_mm": [0.0, 0.0, -20.0],
                   "target_mm": [0.0, 0.0, 0.0],
                   "up": [0.0, 1.0, 0.0], "fov_y_deg": 35.0},
        "area_source": {"shape": "rectangle", "position_mm": [0.0, 0.0, 30.0],
                        "normal": [0.0, 0.0, -1.0], "up": [0.0, 1.0, 0.0],
                        "size_mm": [20.0, 20.0],
                        "spectrum": {"wavelength_nm": [500.0, 600.0],
                                     "value": [1.0, 1.0], "kind": "relative"}},
        "environment_spectrum": {"wavelength_nm": [500.0, 600.0],
                                 "value": [0.0, 0.0], "kind": "relative"},
        "resolution": {"width": width, "height": height, "spp": spp, "seed": 0},
    }


def _request(width: int, height: int, spp: int) -> dict:
    return {
        "render_inputs": _render_inputs(width, height, spp),
        "geometry_thickness_mode": "explicit_override",
        "geometry_thickness_mm": 12.0,
        "display_exposure": 0.012,
        "backend": "gpu_cuda", "precision": "mixed",
    }


def _wait_running(client, job_id: str, deadline: float, progress: list[dict]) -> dict:
    while time.monotonic() < deadline:
        current = client.status(job_id)
        state = current.get("state")
        progress.append({"state": state if state in {
            "queued", "running", "succeeded", "failed", "cancelled"} else "unknown",
            "progress": _safe_progress(current.get("progress"))})
        if state == "running":
            return current
        if state in {"succeeded", "failed", "cancelled"}:
            raise RuntimeError("cancel_probe_render_completed_before_cancel")
        if state not in {"queued", "running"}:
            raise RuntimeError("remote_job_state_invalid")
        time.sleep(0.25)
    raise RuntimeError("cancel_probe_start_timeout")


def _wait_terminal(client, job_id: str, deadline: float, progress: list[dict]) -> dict:
    while time.monotonic() < deadline:
        current = client.status(job_id)
        state = current.get("state")
        progress.append({"state": state if state in {
            "queued", "running", "succeeded", "failed", "cancelled"} else "unknown",
            "progress": _safe_progress(current.get("progress"))})
        if state in {"succeeded", "failed", "cancelled"}:
            return current
        if state not in {"queued", "running"}:
            raise RuntimeError("remote_job_state_invalid")
        time.sleep(0.25)
    raise RuntimeError("remote_job_timeout")


def _write_evidence(path: Path | None, source_root: Path, payload: dict) -> None:
    if path is None:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return
    path = path.expanduser().absolute()
    try:
        path.resolve(strict=False).relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError("evidence_destination_inside_source")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")


def _write_private_preflight_bytes(directory: Path, source_root: Path,
                                   exc: RuntimeFactsError) -> None:
    """Keep raw native output in a separate caller-owned, non-shared folder."""
    directory = directory.expanduser().absolute()
    if directory.resolve(strict=False).is_relative_to(source_root):
        raise ValueError("private_diagnostics_inside_source")
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, content in (("runtime-stdout.bin", exc.stdout),
                          ("runtime-stderr.bin", exc.stderr)):
        with (directory / name).open("xb") as stream:
            stream.write(content)


def run(args) -> dict:
    source_root = Path(args.source_root).resolve(strict=True)
    source_zip = Path(args.source_zip).resolve(strict=True)
    if not source_zip.is_file() or source_zip.is_symlink():
        raise ValueError("source_zip_invalid")
    source_digest = _sha256_file(source_zip)
    interpreter = Path(args.runtime_python).expanduser().absolute()
    if not interpreter.is_file():
        raise ValueError("runtime_interpreter_invalid")

    payload = {
        "schema": "coat-sim.remote-gpu-acceptance.v1", "started_utc": _utc_now(),
        "status": "running", "source_zip_sha256": source_digest,
        "device_id": DEVICE_ID, "requested_backend": "gpu_cuda",
        "requested_precision": "mixed", "progress": {"cancel": [], "render": []},
        "artifacts": [],
    }
    with ExitStack() as resources:
        temporary = resources.enter_context(
            tempfile.TemporaryDirectory(prefix="coat-remote-gpu-acceptance-"))
        temp_root = Path(temporary).resolve(strict=True)
        if temp_root.is_relative_to(source_root):
            raise ValueError("temporary_root_inside_source")
        _safe_process_env(temp_root)
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(source_root))
        service_root = temp_root / "service-root"
        client_root = temp_root / "client-root"
        service_root.mkdir()
        client_root.mkdir()
        from coat_sim.scheduler.remote_appearance import (
            appearance_runtime_environment, appearance_runtime_interpreter,
            materialize_appearance_result,
        )
        runtime_link, linked_interpreter, is_junction, runtime_target = _link_runtime(
            service_root, interpreter)
        # ExitStack is LIFO: unlink the private junction before TemporaryDirectory
        # removes its parent. This never traverses or deletes the runtime target.
        resources.callback(_remove_runtime_link, runtime_link, is_junction, runtime_target)
        selected = Path(appearance_runtime_interpreter(service_root, "gpu_cuda")).resolve(strict=True)
        if (selected != interpreter.resolve(strict=True)
                or linked_interpreter.resolve(strict=True) != selected):
            raise RuntimeError("runtime_interpreter_discovery_mismatch")
        runtime_env = appearance_runtime_environment(service_root, str(interpreter), "gpu_cuda")
        payload["runtime"] = _runtime_facts(interpreter, source_root, runtime_env)

        # The bearer credential is random, process-local, and never serialized.
        token = secrets.token_urlsafe(32)
        from coat_sim.scheduler.remote_server import RemoteComputeService
        from coat_sim.scheduler.remote_client import RemoteTransport
        from coat_sim.scheduler.client import SchedulerClient
        from coat_sim.scheduler.jobs import JobSpec

        service = RemoteComputeService(
            service_root, host="127.0.0.1", port=0, token=token,
            insecure_loopback=True, max_connections=4,
            max_blob_bytes=32 * 1024 * 1024, blob_quota_bytes=64 * 1024 * 1024)
        service_errors: list[str] = []

        def serve() -> None:
            try:
                service.run()
            except BaseException as exc:  # keep OS/path details out of evidence
                service_errors.append(_error_code(exc))

        server_thread = threading.Thread(target=serve, name="remote-gpu-acceptance", daemon=True)
        server_thread.start()
        client = None
        try:
            deadline = time.monotonic() + START_TIMEOUT_S
            while service.http is None and time.monotonic() < deadline:
                if service_errors:
                    raise RuntimeError("loopback_service_start_failed")
                time.sleep(0.05)
            if service.http is None:
                raise RuntimeError("loopback_service_start_timeout")
            base_url = f"http://127.0.0.1:{service.http.server_port}"
            transport = RemoteTransport(
                base_url, token=token, allow_loopback_http=True,
                device_id=DEVICE_ID, pending_root=client_root / "pending")
            client = SchedulerClient(root=client_root, transport=transport)
            caps = client.capabilities()
            devices = caps.get("devices") if isinstance(caps, dict) else None
            row = next((entry for entry in devices or []
                        if isinstance(entry, dict) and entry.get("id") == DEVICE_ID), None)
            cap_summary = {
                "id": row.get("id") if row else None,
                "backend": row.get("backend") if row else None,
                "available": row.get("available") if row else False,
                "binding": row.get("binding") if row else None,
                "runtime_probe": row.get("runtime_probe") if row else None,
                "physical_id_unreported": bool(row and row.get("physical_id") == ""),
            }
            payload["capability"] = cap_summary
            if not (row and row.get("backend") == "gpu_cuda"
                    and row.get("available") is True
                    and row.get("binding") == "default"
                    and row.get("runtime_probe") == "passed"
                    and row.get("physical_id") == ""
                    and "appearance_render" in (row.get("executors") or [])):
                payload["status"] = "blocked_gpu_probe_unavailable"
                payload["safe_reason"] = (row.get("reason") if row and isinstance(row.get("reason"), str)
                                           and re.fullmatch(r"[a-z0-9_]{2,80}", row["reason"])
                                           else "capability_gate_not_passed")
                payload["finished_utc"] = _utc_now()
                return payload

            asset = _synthetic_asset(client_root)
            asset_descriptor = transport.register_appearance_asset(asset)
            payload["input_asset"] = {
                "role": asset_descriptor.get("role"), "bytes": asset_descriptor.get("bytes"),
                "sha256": asset_descriptor.get("sha256"),
            }
            if (asset_descriptor.get("role") != "appearance_bundle"
                    or asset_descriptor.get("name") != "input.coatasset"
                    or asset_descriptor.get("sha256") != _sha256_file(asset)
                    or asset_descriptor.get("bytes") != asset.stat().st_size):
                raise RuntimeError("appearance_asset_descriptor_mismatch")

            def spec(width: int, height: int, spp: int) -> JobSpec:
                return JobSpec("appearance_render", {"request": _request(width, height, spp)}, {
                    "appearance_artifact": {"artifact_id": asset_descriptor["artifact_id"],
                                             "sha256": asset_descriptor["sha256"]},
                    "device_binding": {"schema_version": 1, "device_id": DEVICE_ID},
                })

            cancel_id = "gpu-cancel-" + secrets.token_hex(8)
            cancel_receipt = client.submit(spec(640, 480, 64), job_id=cancel_id)
            if cancel_receipt.get("requested_device_id") != DEVICE_ID:
                raise RuntimeError("cancel_probe_device_binding_missing")
            cancel_progress: list[dict] = payload["progress"]["cancel"]
            _wait_running(client, cancel_id, time.monotonic() + CANCEL_TIMEOUT_S,
                          cancel_progress)
            cancel_reply = client.cancel(cancel_id)
            cancel_progress.append({"state": "cancel_requested", "progress": None})
            cancel_status = _wait_terminal(
                client, cancel_id, time.monotonic() + CANCEL_TIMEOUT_S,
                cancel_progress)
            payload["cancellation"] = {
                "requested": bool(cancel_reply.get("cancel_requested")),
                "terminal_state": cancel_status.get("state"),
            }
            if (cancel_reply.get("cancel_requested") is not True
                    or cancel_status.get("state") != "cancelled"):
                raise RuntimeError("cancel_probe_not_cancelled")

            render_id = "gpu-render-" + secrets.token_hex(8)
            render_receipt = client.submit(spec(8, 6, 1), job_id=render_id)
            if render_receipt.get("requested_device_id") != DEVICE_ID:
                raise RuntimeError("render_device_binding_missing")
            render_status = _wait_terminal(
                client, render_id, time.monotonic() + RENDER_TIMEOUT_S,
                payload["progress"]["render"])
            payload["render_job"] = {
                "requested_device_id": render_status.get("requested_device_id"),
                "executed_device_id": render_status.get("executed_device_id"),
                "terminal_state": render_status.get("state"),
            }
            if (render_status.get("state") != "succeeded"
                    or render_status.get("requested_device_id") != DEVICE_ID
                    or render_status.get("executed_device_id") != DEVICE_ID):
                raise RuntimeError("gpu_render_attestation_failed")
            result = client.result(render_id)
            if (result.get("kind") != "appearance_render"
                    or result.get("backend") != "gpu_cuda"
                    or result.get("precision") != "mixed"
                    or result.get("render_status") != "rendered_unqualified"
                    or result.get("diagnostics_pass") is not True):
                raise RuntimeError("gpu_result_attestation_failed")
            output_dir = client_root / "data" / "acceptance-output"
            materialized = materialize_appearance_result(client, render_id, result, output_dir)
            files = sorted(path for path in output_dir.iterdir() if path.is_file())
            if ({path.name for path in files} != EXPECTED_ARTIFACTS or len(files) != 7):
                raise RuntimeError("gpu_result_artifact_set_invalid")
            artifact_hashes = [{"name": path.name, "bytes": path.stat().st_size,
                                "sha256": _sha256_file(path)} for path in files]
            index = json.loads((output_dir / "appearance-result.json").read_text(encoding="utf-8"))
            manifest_path = output_dir / "manifest.json"
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            if (index.get("backend") != "gpu_cuda" or index.get("precision") != "mixed"
                    or index.get("render_status") != "rendered_unqualified"
                    or index.get("diagnostics_pass") is not True
                    or manifest.get("precision") != "mixed"
                    or manifest.get("status") != "rendered_unqualified"
                    or manifest.get("execution", {}).get("variant") != "cuda_ad_rgb"
                    or manifest.get("runtime", {}).get("variant") != "cuda_ad_rgb"):
                raise RuntimeError("gpu_manifest_attestation_failed")
            payload["render_result"] = {
                "backend": result["backend"], "precision": result["precision"],
                "render_status": result["render_status"], "diagnostics_pass": True,
                "variant": "cuda_ad_rgb", "manifest_sha256": _sha256_file(manifest_path),
                "index_sha256": _sha256_file(output_dir / "appearance-result.json"),
                "artifact_count": len(artifact_hashes), "artifacts": artifact_hashes,
                "materialized_index_sha256": materialized["sha256"],
            }
            payload["status"] = "passed"
        finally:
            service.scheduler.stopping.set()
            server_thread.join(timeout=10)
            if server_thread.is_alive():
                payload["status"] = "failed"
                payload["error_code"] = "loopback_service_shutdown_timeout"
            if service_errors:
                payload["status"] = "failed"
                payload["error_code"] = "loopback_service_failed"
    payload["finished_utc"] = _utc_now()
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--source-zip", required=True,
                        help="frozen source archive; SHA-256 only is recorded")
    parser.add_argument("--runtime-python", required=True,
                        help="exact validated Mitsuba/Dr.Jit Python used by the service")
    parser.add_argument("--evidence-out", help="new JSON file outside the source tree")
    parser.add_argument("--private-diagnostics-dir",
                        help="new private directory for raw preflight child bytes; never share it")
    args = parser.parse_args(argv)
    try:
        payload = run(args)
    except BaseException as exc:
        payload = {
            "schema": "coat-sim.remote-gpu-acceptance.v1", "started_utc": _utc_now(),
            "finished_utc": _utc_now(), "status": "failed",
            "error_code": _error_code(exc),
        }
        if isinstance(exc, RuntimeFactsError):
            payload["preflight_diagnostic"] = exc.diagnostic
            if args.private_diagnostics_dir:
                try:
                    _write_private_preflight_bytes(
                        Path(args.private_diagnostics_dir),
                        Path(args.source_root).resolve(strict=True), exc)
                    payload["private_preflight_bytes_saved"] = True
                except (OSError, ValueError):
                    payload["private_preflight_bytes_saved"] = False
        try:
            source_root = Path(args.source_root).resolve(strict=True)
            _write_evidence(Path(args.evidence_out) if args.evidence_out else None,
                            source_root, payload)
        except Exception:
            print(json.dumps(payload, sort_keys=True, indent=2))
        return 2
    try:
        _write_evidence(Path(args.evidence_out) if args.evidence_out else None,
                        Path(args.source_root).resolve(strict=True), payload)
    except Exception as exc:
        print(json.dumps({"schema": payload.get("schema"), "status": "failed",
                          "error_code": _error_code(exc)}, sort_keys=True, indent=2))
        return 2
    return 0 if payload.get("status") == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
