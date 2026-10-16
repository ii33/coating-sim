"""Strict detached optical edit records; no Qt, jobs, or asset publication.

Unlike saved numerical inputs, recovery preserves incomplete text controls.
Only explicitly typed, already-owned binary inputs may cross this boundary.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re

from .project_dependencies import MAX_FILE_BYTES, ProjectError, reject_links
from .project_simulation_resources import (
    APPEARANCE_SCHEMA, BOOL_DRAFT_FIELDS, NUMERIC_DRAFT_FIELDS,
    OTHER_DRAFT_FIELDS, _CONTEXT_FIELDS,
)

MODULE_KINDS = {"ray_trace": "raytrace_request", "appearance": "appearance_configuration"}
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
RAW_FIELDS = frozenset({"module_key", "document_id", "revision", "information",
                        "model_dirty", "dirty", "state", "pending_information", "operation"})
RECORD_FIELDS = (RAW_FIELDS - {"operation"}) | {"kind", "token", "sha256"}


def detached(value):
    """Reject non-JSON values and keys before JSON can silently coerce them."""
    def check(item, depth=0):
        if depth > 64:
            raise ProjectError("invalid_recovery", "Optical recovery is nested too deeply")
        if item is None or isinstance(item, (str, bool)) or type(item) in (int, float):
            return
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise ProjectError("invalid_recovery", "Optical recovery object keys must be strings")
            for child in item.values():
                check(child, depth + 1)
            return
        if isinstance(item, list):
            for child in item:
                check(child, depth + 1)
            return
        raise ProjectError("invalid_recovery", "Optical recovery must contain only finite JSON")
    check(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_FILE_BYTES:
            raise ProjectError("capacity_exceeded", "Optical recovery exceeds the JSON safety limit")
        return json.loads(encoded)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProjectError("invalid_recovery", "Optical recovery must contain only finite JSON") from exc


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def information_token(document_id, information):
    values = {"name": information["name"], "summary": information["description"],
              "notes": information["notes"]}
    return sha256(json.dumps([document_id or "", values], ensure_ascii=False,
                             sort_keys=True).encode("utf-8")).hexdigest()


def validate_raw(record, module_key=None):
    value = detached(record)
    # Operation is part of the live protocol only, never persisted as a job.
    if not isinstance(value, dict) or set(value) != RAW_FIELDS:
        raise ProjectError("invalid_recovery", "Optical draft fields are invalid")
    module = value["module_key"]
    if not isinstance(module, str) or module not in MODULE_KINDS or (module_key is not None and module != module_key):
        raise ProjectError("invalid_recovery", "Optical draft module does not match its owner")
    identity, revision = value["document_id"], value["revision"]
    if not ((identity is None and revision is None) or
            (isinstance(identity, str) and _ID.fullmatch(identity) and
             isinstance(revision, str) and _SHA.fullmatch(revision))):
        raise ProjectError("invalid_recovery", "Optical draft managed identity is invalid")
    metadata = value["information"]
    if (not isinstance(metadata, dict) or set(metadata) != {"name", "description", "notes"}
            or any(not isinstance(metadata[key], str) or len(metadata[key]) > limit
                   for key, limit in (("name", 120), ("description", 20000), ("notes", 20000)))
            or not metadata["name"].strip()):
        raise ProjectError("invalid_recovery", "Optical draft information is invalid")
    if type(value["dirty"]) is not bool or type(value["model_dirty"]) is not bool:
        raise ProjectError("invalid_recovery", "Optical draft dirty flags must be boolean")
    if value["model_dirty"] and not value["dirty"]:
        raise ProjectError("invalid_recovery", "Edited optical input must remain dirty")
    pending = value["pending_information"]
    if pending is not None:
        if (not isinstance(pending, dict) or set(pending) != {
                "name", "summary", "notes", "document_id", "token", "model_dirty"}
                or pending["document_id"] != identity or type(pending["model_dirty"]) is not bool
                or any(not isinstance(pending[key], str) or len(pending[key]) > limit
                       for key, limit in (("name", 200), ("summary", 20000), ("notes", 20000)))
                or pending["token"] != information_token(identity, metadata)):
            raise ProjectError("invalid_recovery", "Optical unconfirmed information or token is invalid")
        if not value["dirty"]:
            raise ProjectError("invalid_recovery", "Unconfirmed optical information must remain dirty")
    operation = value["operation"]
    if operation is not None and (not isinstance(operation, dict) or set(operation) != {"request_id", "kind"}
            or not isinstance(operation["request_id"], str) or not _ID.fullmatch(operation["request_id"])
            or not isinstance(operation["kind"], str) or operation["kind"] not in {"open", "save", "copy", "restore"}):
        raise ProjectError("invalid_recovery", "Optical draft operation token is invalid")
    _validate_state(module, value["state"])
    return value


def _validate_state(module, state):
    if not isinstance(state, dict):
        raise ProjectError("invalid_recovery", "Optical input state must be an object")
    if module == "ray_trace":
        if (set(state) != {"request_text", "deposition_asset"}
                or not isinstance(state["request_text"], str)):
            raise ProjectError("invalid_recovery", "Ray input recovery accepts only request text and its input asset")
        asset = state["deposition_asset"]
        if asset is not None and (not isinstance(asset, dict) or set(asset) != {"path", "sha256"}):
            raise ProjectError("invalid_recovery", "Ray recovery input asset is invalid")
    else:
        if (set(state) != {"schema", "version", "context", "draft", "geometry_thickness_mode", "backend", "precision"}
                or state["schema"] != APPEARANCE_SCHEMA or type(state["version"]) is not int or state["version"] != 1
                or any(not isinstance(state[key], str) for key in ("geometry_thickness_mode", "backend", "precision"))
                or state["geometry_thickness_mode"] not in {"inherited", "overridden"}
                or state["backend"] not in {"cpu", "gpu_cuda"}
                or state["precision"] not in {"native", "float32", "mixed"}):
            raise ProjectError("invalid_recovery", "Appearance recovery input fields are invalid")
        context, draft = state["context"], state["draft"]
        if context is None:
            if draft != {}:
                raise ProjectError("invalid_recovery", "Unbound appearance input must have an empty draft")
            return
        if (not isinstance(context, dict) or set(context) != _CONTEXT_FIELDS
                or context["schema"] != "coat_sim.appearance_launch_context"
                or type(context["version"]) is not int or context["version"] != 1
                or not isinstance(context["process_profile"], dict)
                or not isinstance(context["substrate_profile"], dict)
                or context["process_profile_sha256"] != digest(context["process_profile"])
                or not isinstance(context["captured_step_ids"], list)
                or any(not isinstance(item, str) for item in context["captured_step_ids"])
                or any(not isinstance(context[key], str) for key in
                       ("asset_root", "layer_field_units", "layer_field_coordinate_frame"))):
            raise ProjectError("invalid_recovery", "Appearance recovery frozen input context is invalid")
        if not isinstance(draft, dict) or set(draft) != NUMERIC_DRAFT_FIELDS | BOOL_DRAFT_FIELDS | OTHER_DRAFT_FIELDS:
            raise ProjectError("invalid_recovery", "Appearance recovery editable fields are invalid")
        if (any(type(draft[key]) not in (str, int, float) for key in NUMERIC_DRAFT_FIELDS)
                or any(type(draft[key]) is not bool for key in BOOL_DRAFT_FIELDS)
                or any(not isinstance(draft[target + "_kind"], str) for target in ("area", "environment"))
                or any(draft[target + "_kind"] not in {"relative", "absolute"} for target in ("area", "environment"))):
            raise ProjectError("invalid_recovery", "Appearance recovery editable value types are invalid")
        for target in ("area", "environment"):
            spectrum = draft[target + "_spectrum"]
            if spectrum is not None and (not isinstance(spectrum, dict)
                    or set(spectrum) - {"path", "sha256", "wavelength_nm", "value"}
                    or not {"path", "wavelength_nm", "value"}.issubset(spectrum)
                    or not isinstance(spectrum["wavelength_nm"], list) or not isinstance(spectrum["value"], list)
                    or len(spectrum["wavelength_nm"]) != len(spectrum["value"])
                    or len(spectrum["wavelength_nm"]) < 2
                    or any(type(item) not in (int, float) for item in spectrum["wavelength_nm"] + spectrum["value"])
                    or any(item < 0 for item in spectrum["value"])
                    or any(b <= a for a, b in zip(spectrum["wavelength_nm"], spectrum["wavelength_nm"][1:], strict=False))):
                raise ProjectError("invalid_recovery", "Appearance recovery spectrum input is invalid")
    for owner, field, digest_field, _role in asset_references(module, state):
        if (not isinstance(owner[field], str) or not owner[field]
                or (not (_role == "spd_csv" and digest_field not in owner) and
                    (not isinstance(owner.get(digest_field), str) or not _SHA.fullmatch(owner[digest_field])))):
            raise ProjectError("invalid_recovery", "Optical recovery input asset requires a path and checksum")


def asset_references(module, state):
    if module == "ray_trace":
        if state["deposition_asset"] is not None:
            yield state["deposition_asset"], "path", "sha256", "deposition_asset"
    elif state["context"] is not None:
        yield state["context"], "asset_path", "asset_sha256", "deposition_asset"
        for target in ("area", "environment"):
            spectrum = state["draft"][target + "_spectrum"]
            if spectrum is not None:
                yield spectrum, "path", "sha256", "spd_csv"


def _assets(store):
    store._current()
    return {row["id"]: store._read_record(row) for row in store.manifest["documents"]
            if row["kind"] == "binary_asset"}


def _asset_identity(store, assets, reference, *, managed):
    identity: str | None
    if managed:
        path = PurePosixPath(reference)
        identity = path.stem
        if path.name != reference or path.suffix != ".casset" or not _ID.fullmatch(identity):
            raise ProjectError("invalid_recovery", "Optical recovery requires a managed binary input reference")
    else:
        source = Path(os.path.abspath(reference))
        reject_links(source)
        identity = next((key for key, data in assets.items()
                         if source == store.root / data["payload_path"]), None)
    if identity not in assets:
        raise ProjectError("unowned_recovery_asset", "Capture optical input files into the project before checkpointing")
    return identity


def _bind_assets(store, record, *, managed, native=False):
    assets = _assets(store)
    for owner, field, digest_field, role in asset_references(record["module_key"], record["state"]):
        identity = _asset_identity(store, assets, owner[field], managed=managed)
        metadata = assets[identity]
        if managed and digest_field not in owner:
            raise ProjectError("invalid_recovery", "Managed optical input references require their checksum")
        if metadata["role"] != role or owner.get(digest_field, metadata["sha256"]) != metadata["sha256"]:
            raise ProjectError("invalid_recovery", "Optical recovery input role or checksum differs from its owned asset")
        owner[digest_field] = metadata["sha256"]
        owner[field] = str(store.root / metadata["payload_path"]) if native else identity + ".casset"
    if record["module_key"] == "appearance" and record["state"]["context"] is not None:
        context = record["state"]["context"]
        if managed and context["asset_root"] != ".":
            raise ProjectError("invalid_recovery", "Appearance recovery must use the managed asset root")
        context["asset_root"] = str(store.root) if native else "."


def snapshot_record(store, raw):
    value = validate_raw(raw)
    if value.pop("operation") is not None:
        raise ProjectError("pending_operation", "Wait for the optical input operation before checkpointing")
    _bind_assets(store, value, managed=False)
    value["kind"] = MODULE_KINDS[value["module_key"]]
    value["token"] = digest({key: item for key, item in value.items() if key not in {"token", "sha256"}})
    value["sha256"] = digest(value)
    return validate_record(store, value)


def validate_record(store, record):
    value = detached(record)
    if not isinstance(value, dict) or set(value) != RECORD_FIELDS:
        raise ProjectError("invalid_recovery", "Optical recovery record fields are invalid")
    module = value["module_key"]
    if (not isinstance(module, str) or module not in MODULE_KINDS or value["kind"] != MODULE_KINDS[module]
            or value["sha256"] != digest({key: item for key, item in value.items() if key != "sha256"})
            or value["token"] != digest({key: item for key, item in value.items() if key not in {"sha256", "token"}})):
        raise ProjectError("invalid_recovery", "Optical recovery kind, token, or checksum is invalid")
    raw = {key: item for key, item in value.items() if key in RAW_FIELDS}
    raw["operation"] = None
    validate_raw(raw)
    identity = value["document_id"]
    if identity is not None:
        row = store._index().get(identity)
        if row is None or row["kind"] != value["kind"] or row["sha256"] != value["revision"]:
            raise ProjectError("stale_recovery", "Optical recovery original is absent or has changed")
    _bind_assets(store, value, managed=True)
    return value


def native_record(store, record, *, as_copy):
    value = validate_record(store, record)
    _bind_assets(store, value, managed=True, native=True)
    value = {key: item for key, item in value.items() if key in RAW_FIELDS}
    value["operation"] = None
    if as_copy:
        value["document_id"] = value["revision"] = None
        value["model_dirty"] = value["dirty"] = True
        pending = value["pending_information"]
        if pending is not None:
            pending["document_id"] = None
            pending["token"] = information_token(None, value["information"])
            pending["model_dirty"] = True
    return deepcopy(value)
