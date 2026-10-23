"""Headless saved-input projections for ray and appearance workspaces."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import math
from pathlib import PurePosixPath
import re

from .project_dependencies import ProjectError, identifier, json_bytes, portable_path

GLOBAL_SIMULATION_FOLDERS = {"raytrace_request": "raytrace", "appearance_configuration": "appearance"}
SIMULATION_EXTENSIONS = {"raytrace_request": ".cray", "appearance_configuration": ".cappearance",
                         "binary_asset": ".casset"}
RAY_UI_SCHEMA = "coat_sim.geometric_raytrace_ui_document"
APPEARANCE_SCHEMA = "coat_sim.appearance_configuration"
_CONTEXT_FIELDS = {"schema", "version", "asset_path", "asset_root", "asset_sha256",
    "process_profile_sha256", "process_profile", "substrate_profile", "captured_step_ids",
    "layer_field_units", "layer_field_coordinate_frame"}
NUMERIC_DRAFT_FIELDS = frozenset({"geometry_thickness_mm", "camera_x", "camera_y", "camera_z",
    "target_x", "target_y", "target_z", "up_x", "up_y", "up_z", "fov_y_deg",
    "area_x", "area_y", "area_z", "area_target_x", "area_target_y", "area_target_z",
    "area_up_x", "area_up_y", "area_up_z", "area_size_x", "area_size_y", "area_scale",
    "environment_scale", "width", "height", "spp", "seed", "wavelength_start_nm",
    "wavelength_end_nm", "wavelength_step_nm"})
BOOL_DRAFT_FIELDS = frozenset({"area_enabled", "environment_enabled", "area_absolute_calibration",
                             "environment_absolute_calibration"})
OTHER_DRAFT_FIELDS = frozenset({"area_kind", "environment_kind", "area_spectrum", "environment_spectrum"})
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _managed_reference(value, label):
    if not isinstance(value, str):
        raise ProjectError("invalid_document", label + " must be a managed binary input filename")
    relative = portable_path(value)
    path = PurePosixPath(relative)
    if path.name != relative or path.suffix != ".casset":
        raise ProjectError("unsafe_path", label + " must be a flat UUID.casset reference")
    identifier(path.stem)


def simulation_asset_references(kind, data):
    payload = data["payload"]
    if kind == "raytrace_request":
        asset = payload["deposition_asset"]
        if asset is not None:
            yield asset, "path", "deposition_asset", "deposition"
    elif kind == "appearance_configuration" and payload["context"] is not None:
        yield payload["context"], "asset_path", "deposition_asset", "deposition"
        for target in ("area", "environment"):
            spectrum = payload["draft"][target + "_spectrum"]
            if spectrum is not None:
                yield spectrum, "path", "spd_csv", target + "_spectrum"


def normalize_simulation_document(kind: str, data: Mapping, *, managed=True) -> dict:
    required = {"kind", "version", "name", "payload"}
    if (not isinstance(data, Mapping) or not required.issubset(data) or
            set(data) - required - {"description", "notes"} or
            data.get("kind") != "coat_sim.project_" + kind or
            type(data.get("version")) is not int or data["version"] != 1):
        raise ProjectError("invalid_document", "Invalid saved simulation input envelope")
    if kind not in {"raytrace_request", "appearance_configuration"}:
        raise ProjectError("invalid_document", "Unsupported saved simulation input kind")
    name = data["name"]
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise ProjectError("invalid_document", "Simulation input name must be 1–120 characters")
    result = deepcopy(dict(data))
    result["name"] = name.strip()
    for field in ("description", "notes"):
        if field in result and (not isinstance(result[field], str) or len(result[field]) > 20000):
            raise ProjectError("invalid_document", field + " must be text up to 20000 characters")
    payload = result["payload"]
    json_bytes(payload)  # All saved text is finite JSON, never runtime objects.
    if kind == "raytrace_request":
        if (not isinstance(payload, dict) or set(payload) != {"schema", "schema_version", "request", "deposition_asset"} or
                payload["schema"] != RAY_UI_SCHEMA or type(payload["schema_version"]) is not int or payload["schema_version"] != 1):
            raise ProjectError("invalid_document", "Unsupported ray-trace workspace input projection")
        request = payload["request"]
        required_request = {"schema", "schema_version", "units", "polarization", "samples", "spectrum", "surfaces"}
        if (not isinstance(request, dict) or not required_request.issubset(request) or
                set(request) - required_request - {"detector", "limits"} or
                request["schema"] != "coat_sim.geometric_raytrace" or
                type(request["schema_version"]) is not int or request["schema_version"] != 1 or
                request["units"] != {"length": "mm", "wavelength": "nm", "index": "n+i*k"} or
                request["polarization"] not in {"s", "p", "unpolarized"} or
                any(not isinstance(request[field], list) for field in ("samples", "surfaces", "spectrum"))):
            raise ProjectError("invalid_document", "Invalid saved ray-trace request shape")
        from .geometric_raytrace import _read_request, request_limits
        try:
            request_limits(request)
            # A new, explicitly non-executable workspace may have no samples or
            # surfaces yet. Populated requests use the existing strict parser.
            if request["samples"] and request["surfaces"]:
                _read_request(request)
        except (ValueError, TypeError, KeyError) as exc:
            raise ProjectError("invalid_document", "Invalid ray-trace request: " + str(exc)) from exc
        asset = payload["deposition_asset"]
        if asset is not None and (not isinstance(asset, dict) or set(asset) != {"path", "sha256"} or
                not isinstance(asset["sha256"], str) or not _SHA.fullmatch(asset["sha256"])):
            raise ProjectError("invalid_document", "Ray-trace deposition reference needs path and SHA-256")
    else:
        fields = {"schema", "version", "context", "draft", "geometry_thickness_mode", "backend", "precision"}
        if (not isinstance(payload, dict) or set(payload) != fields or payload["schema"] != APPEARANCE_SCHEMA or
                type(payload["version"]) is not int or payload["version"] != 1 or
                payload["geometry_thickness_mode"] not in {"inherited", "overridden"} or
                payload["backend"] not in {"cpu", "gpu_cuda"} or
                payload["precision"] not in {"native", "float32", "mixed"}):
            raise ProjectError("invalid_document", "Invalid appearance input projection")
        context, draft = payload["context"], payload["draft"]
        if context is None:
            if draft != {}:
                raise ProjectError("invalid_document", "An unbound appearance input must have an empty draft")
            return result
        if (not isinstance(context, dict) or set(context) != _CONTEXT_FIELDS or
                context["schema"] != "coat_sim.appearance_launch_context" or
                type(context["version"]) is not int or context["version"] != 1 or
                not isinstance(context["process_profile"], dict) or not isinstance(context["substrate_profile"], dict) or
                not isinstance(context["asset_sha256"], str) or not _SHA.fullmatch(context["asset_sha256"]) or
                context["process_profile_sha256"] != _sha(context["process_profile"]) or
                not isinstance(context["captured_step_ids"], list) or
                any(not isinstance(item, str) for item in context["captured_step_ids"]) or
                any(not isinstance(context[field], str) for field in ("layer_field_units", "layer_field_coordinate_frame", "asset_root"))):
            raise ProjectError("invalid_document", "Invalid frozen appearance context")
        if managed:
            context["asset_root"] = "."
        if not isinstance(draft, dict) or set(draft) != NUMERIC_DRAFT_FIELDS | BOOL_DRAFT_FIELDS | OTHER_DRAFT_FIELDS:
            raise ProjectError("invalid_document", "Appearance draft must contain exactly the saved input fields")
        for field in NUMERIC_DRAFT_FIELDS:
            value = draft[field]
            if field == "geometry_thickness_mm" and value == "" and payload["geometry_thickness_mode"] == "inherited":
                continue
            try:
                if isinstance(value, bool) or not isinstance(value, (str, int, float)) or not math.isfinite(float(value)):
                    raise ValueError()
            except (ValueError, TypeError, OverflowError) as exc:
                raise ProjectError("invalid_document", "Appearance draft field must be finite numeric text: " + field) from exc
        if any(type(draft[field]) is not bool for field in BOOL_DRAFT_FIELDS):
            raise ProjectError("invalid_document", "Appearance input flags must be bool")
        for target in ("area", "environment"):
            if draft[target + "_kind"] not in {"relative", "absolute"}:
                raise ProjectError("invalid_document", "Invalid appearance spectrum kind")
            spectrum = draft[target + "_spectrum"]
            if spectrum is not None:
                if not isinstance(spectrum, dict) or set(spectrum) - {"path", "sha256", "wavelength_nm", "value"} or not {"path", "wavelength_nm", "value"}.issubset(spectrum):
                    raise ProjectError("invalid_document", "Invalid appearance scalar spectrum input")
                waves, values = spectrum["wavelength_nm"], spectrum["value"]
                if (not isinstance(waves, list) or not isinstance(values, list) or len(waves) != len(values) or len(waves) < 2 or
                        any(type(item) not in (int, float) or not math.isfinite(item) for item in waves + values) or
                        any(value < 0 for value in values) or any(b <= a for a, b in zip(waves, waves[1:], strict=False))):
                    raise ProjectError("invalid_document", "Scalar spectrum samples must be finite, ordered and nonnegative")
                if "sha256" in spectrum and (not isinstance(spectrum["sha256"], str) or not _SHA.fullmatch(spectrum["sha256"])):
                    raise ProjectError("invalid_document", "Invalid spectrum input SHA-256")
    if managed:
        for owner, field, _role, _slot in simulation_asset_references(kind, result):
            _managed_reference(owner[field], field)
    return result


def native_simulation_payload(store, kind: str, document: Mapping, *, validate=True) -> dict:
    result = deepcopy(dict(document))
    for owner, field, role, _slot in simulation_asset_references(kind, result):
        identity = PurePosixPath(owner[field]).stem
        if validate:
            path = store.binary_asset_path(identity)
        else:
            metadata = store._read_record(store._index()[identity])
            path = store.root / metadata["payload_path"]
        owner[field] = str(path)
        if role == "spd_csv" and validate:
            from .project_binary_assets import read_scalar_spectrum
            actual = read_scalar_spectrum(path)
            if any(owner[key] != actual[key] for key in ("wavelength_nm", "value")):
                raise ProjectError("integrity_error", "Saved spectrum samples differ from the owned CSV input")
    payload = result["payload"]
    if kind == "appearance_configuration" and payload["context"] is not None:
        payload["context"]["asset_root"] = str(store.root)
    return payload


def capture_simulation_asset(service, role: str, path, *, expected_sha256=None, cancel_event=None) -> dict:
    """Explicitly import a reusable input without creating a simulation config.

    File-picker acceptance is the mutation boundary. After success the owned
    input remains a project resource even if its referring draft is abandoned.
    """
    import os
    from pathlib import Path
    import uuid
    from .project_binary_assets import ROLE_SUFFIXES, file_signature, metadata_for_file

    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise ProjectError("cancelled", "Input import was cancelled before publication")

    def result(identity, metadata, reused):
        return {"document_id": identity, "path": str(service.root / metadata["payload_path"]),
                "sha256": metadata["sha256"], "size": metadata["size"], "role": role,
                "reused": reused}

    if not isinstance(role, str) or role not in ROLE_SUFFIXES:
        raise ProjectError("invalid_document", "Unsupported binary input role")
    check_cancel()
    with service._operation(mutate=True):
        records, data = service.store._read_operation_inputs()
        baseline = service.store.manifest
    source = Path(os.path.abspath(Path(path).expanduser()))
    signature = file_signature(source)
    identity, generation = uuid.uuid4().hex, uuid.uuid4().hex
    metadata = metadata_for_file(source, role,
        f"assets/{generation}/{identity}{ROLE_SUFFIXES[role]}", expected_sha256=expected_sha256)
    duplicate = next((old for old, row in records.items() if row["kind"] == "binary_asset" and
        all(data[old].get(key) == metadata[key] for key in ("role", "size", "sha256"))), None)
    if duplicate is not None:
        # Do not reuse a corrupt owned file merely because its metadata agrees.
        service.store.binary_asset_path(duplicate)
        with service._operation(mutate=True):
            check_cancel()
            service.store._current()
            if service.store.manifest != baseline or file_signature(source) != signature:
                raise ProjectError("stale_source", "Project or selected input changed during import")
            return result(duplicate, data[duplicate], True)
    records[identity] = {"id": identity, "kind": "binary_asset",
        "path": f"objects/{generation}/{identity}.casset", "sha256": "0" * 64, "dependencies": []}
    data[identity] = metadata
    check_cancel()
    with service.store.stage_asset_publications(records, data, {identity: {"path": str(source)}}) as prepared:
        check_cancel()
        with service._operation(mutate=True):
            check_cancel()
            service.store._current()
            if service.store.manifest != baseline:
                raise ProjectError("stale_project", "Project changed while importing input; import again")
            if file_signature(source) != signature:
                raise ProjectError("stale_source", "Selected input changed before import publication")
            service.store._publish(records, data, assets=prepared)
    return result(identity, metadata, False)


def save_simulation_resource(service, kind: str, payload: Mapping, *, document_id=None,
                             expected_revision=None, name=None, asset_sources=None,
                             description=None, notes=None):
    """Capture explicit input files and publish their JSON owner atomically."""
    import os
    from pathlib import Path
    import tempfile
    import uuid
    from .project_binary_assets import (ROLE_SUFFIXES, capture_asset, metadata_for_file,
                                        read_scalar_spectrum)
    from .project_dependencies import normalize_document
    from .project_paths import project_document_handle
    from .project_store import _PreparedProject
    if kind not in {"raytrace_request", "appearance_configuration"}:
        raise ProjectError("invalid_document", "Unsupported saved simulation input kind")
    supplied = {} if asset_sources is None else dict(asset_sources)
    if set(supplied) - {"deposition", "area_spectrum", "environment_spectrum"}:
        raise ProjectError("invalid_document", "Unknown simulation asset source slot")
    with service._operation(mutate=True):
        records, data = service.store._read_operation_inputs()
        baseline = service.store.manifest
        original = None
        if document_id is not None:
            row = records.get(document_id)
            if row is None or row["kind"] != kind:
                raise ProjectError("missing_document", "Simulation save requires an existing document of the same kind")
            expected = expected_revision or service._loaded_revisions.get(document_id)
            if expected is None:
                raise ProjectError("revision_required", "Load the simulation resource before updating it")
            if row["sha256"] != expected:
                raise ProjectError("stale_document", "Simulation input changed since it was loaded")
            original = data[document_id]
    title = name if name is not None else original["name"] if original else ("光追配置" if kind == "raytrace_request" else "外观配置")
    document = {"kind": "coat_sim.project_" + kind, "version": 1, "name": title,
                "payload": deepcopy(dict(payload))}
    if original is not None:
        for field in ("description", "notes"):
            if field in original:
                document[field] = original[field]
    if description is not None:
        document["description"] = description
    if notes is not None:
        document["notes"] = notes
    document = normalize_simulation_document(kind, document, managed=False)
    incoming = {}
    total = 0
    generation = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="coat-project-input-") as raw:
        temporary = Path(raw)
        for owner, field, role, slot in simulation_asset_references(kind, document):
            source = Path(os.path.abspath(Path(supplied.get(slot, owner[field])).expanduser()))
            expected_hash = owner.get("sha256", owner.get("asset_sha256"))
            existing = next((identity for identity, row in records.items()
                if row["kind"] == "binary_asset" and data[identity].get("role") == role and
                isinstance(data[identity].get("payload_path"), str) and
                source == service.root / data[identity]["payload_path"]), None)
            if existing is not None:
                metadata = data[existing]
                service.store.binary_asset_path(existing)
                if expected_hash is not None and expected_hash != metadata["sha256"]:
                    raise ProjectError("integrity_error", "Input reference differs from the selected owned asset")
                identity = existing
                captured = {"path": str(source)}
            else:
                identity = uuid.uuid4().hex
                metadata = metadata_for_file(source, role,
                    f"assets/{generation}/{identity}{ROLE_SUFFIXES[role]}", expected_sha256=expected_hash)
                total += metadata["size"]
                service.store.asset_limits.check_total(total)
                captured = capture_asset({"path": str(source)}, metadata, temporary / (identity + ROLE_SUFFIXES[role]))
                duplicate = next((old for old, row in records.items() if row["kind"] == "binary_asset" and
                    all(data[old].get(key) == metadata[key] for key in ("role", "size", "sha256"))), None)
                if duplicate is not None:
                    # A matching envelope is not proof that previously owned
                    # bytes are still intact. Inputs added in this transaction
                    # already have a verified private copy instead.
                    if duplicate not in incoming:
                        service.store.binary_asset_path(duplicate)
                    identity, metadata = duplicate, data[duplicate]
                else:
                    records[identity] = {"id": identity, "kind": "binary_asset",
                        "path": f"objects/{generation}/{identity}.casset", "sha256": "0" * 64, "dependencies": []}
                    data[identity] = metadata
                    incoming[identity] = captured
            owner[field] = identity + ".casset"
            if field == "asset_path":
                owner["asset_root"] = "."
                owner["asset_sha256"] = metadata["sha256"]
            else:
                owner["sha256"] = metadata["sha256"]
            if role == "spd_csv":
                actual = read_scalar_spectrum(captured["path"])
                if any(owner[key] != actual[key] for key in ("wavelength_nm", "value")):
                    raise ProjectError("integrity_error", "Spectrum table differs from the selected CSV input")
        normalized = normalize_document(kind, document)
        if document_id is None:
            document_id = service.store._add_document(records, data, kind, normalized)
        else:
            data[document_id] = normalized
        service.store._rebind_file_references(records, data, [document_id])
        records.required_ids.add(document_id)
        with service.store.stage_asset_publications(records, data, incoming, existing_spool_bytes=total) as prepared:
            with service._operation(mutate=True):
                service.store._current()
                if service.store.manifest != baseline:
                    raise ProjectError("stale_project", "Project changed while simulation inputs were captured; save again")
                revisions = service.store._publish(records, data, assets=prepared)
                # Capture this commit's receipt before admitting another save
                # or close. Forked services can commit after the writer lock
                # releases, so neither revisions nor paths may use live rows.
                revision = revisions[document_id]
                service._loaded_revisions[document_id] = revision
                from .api import Document
                value = deepcopy(data[document_id])
                snapshot = _PreparedProject(service.root,
                    {**baseline, "documents": list(records.values())}, "", data)
                saved = Document(kind, str(project_document_handle(service.root, document_id, kind)), value, (),
                    {"payload": native_simulation_payload(snapshot, kind, value, validate=False),
                     "revision": revision})
        return saved
