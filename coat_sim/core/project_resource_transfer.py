"""Read-only resource catalogs and explicit, selective project exchange.

Catalogs and plans own detached snapshots, never live editor drafts. A plan is
one-shot after success; canceling or inspecting cannot publish a generation.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import tempfile
from threading import RLock
from typing import Any, Callable
import uuid

from .design_files import design_kind_from_path
from .project_dependencies import (
    EXTENSIONS, MAX_FILE_BYTES, MAX_PROJECT_BYTES, MAX_PROJECT_DOCUMENTS, ProjectError,
    document_material_references, external_document, file_references, json_bytes,
    normalize_document, read_json, reject_links,
)
from .project_store import ProjectStore, _PreparedProject, _manifest


def _digest(value: dict) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def _name(kind: str, data: dict) -> str:
    from .resource_information import resource_display_name
    return resource_display_name(kind, data)


def _information(kind: str, data: dict) -> dict[str, str]:
    """Small detached public metadata, without loading assets or editor state."""
    from .resource_information import resource_information
    if kind in {"material_profile", "optical_material"}:
        material = data["material"]
        return {"summary": material.get("description", ""), "notes": material.get("notes", "")}
    if kind == "binary_asset":
        return {"summary": "", "notes": ""}
    information = resource_information(kind, data)
    return {"summary": information["summary"], "notes": information["notes"]}


def _index(snapshot: dict) -> dict[str, dict]:
    return {row["id"]: row for row in snapshot["manifest"]["documents"]}


def _snapshot(records: dict, data: dict, manifest: dict, assets=None) -> dict:
    manifest = deepcopy(manifest)
    manifest["documents"] = list(deepcopy(records).values())
    for row in manifest["documents"]:
        row["sha256"] = hashlib.sha256(json_bytes(data[row["id"]])).hexdigest()
    result = {"manifest": _manifest(manifest), "documents": deepcopy(data)}
    if assets:
        result["assets"] = deepcopy(assets)
    return result


def _validate_snapshot(snapshot: dict, asset_limits=None) -> None:
    manifest = _manifest(snapshot["manifest"])
    data = snapshot["documents"]
    if set(data) != {row["id"] for row in manifest["documents"]}:
        raise ProjectError("invalid_document", "Resource snapshot does not match its manifest")
    for row in manifest["documents"]:
        value = data[row["id"]]
        encoded = json_bytes(value)
        if len(encoded) > MAX_FILE_BYTES:
            raise ProjectError("capacity_exceeded", "Resource document exceeds the JSON capacity limit")
        if hashlib.sha256(encoded).hexdigest() != row["sha256"]:
            raise ProjectError("integrity_error", "Resource snapshot digest does not match")
        if normalize_document(row["kind"], value) != value:
            raise ProjectError("invalid_document", "Resource snapshot is not normalized")
    preview = _PreparedProject(Path(tempfile.gettempdir()), manifest, "", deepcopy(data), snapshot.get("assets"))
    if asset_limits is not None:
        preview.asset_limits = asset_limits
    preview.snapshot_saved_inputs(verify_assets=False)


def _catalog_dependencies(snapshot: dict) -> dict[str, list[str]]:
    """Profiles also carry their paired frozen optics when explicitly copied."""
    rows = _index(snapshot)
    data = snapshot["documents"]
    optics = {data[key]["material"]["id"]: key for key, row in rows.items()
              if row["kind"] == "optical_material"}
    result = {key: list(row["dependencies"]) for key, row in rows.items()}
    for key, row in rows.items():
        if row["kind"] == "material_profile":
            optical = optics.get(data[key]["material"]["id"])
            if optical is not None and optical not in result[key]:
                result[key].append(optical)
        result[key].sort()
    return result


def _selection(snapshot: dict, identities: Sequence[str]) -> tuple[list[str], list[str]]:
    if (not isinstance(identities, (list, tuple)) or not identities or
            any(not isinstance(key, str) for key in identities) or
            len(set(identities)) != len(identities)):
        raise ProjectError("invalid_selection", "Select a nonempty unique list of resource IDs")
    dependencies = _catalog_dependencies(snapshot)
    if any(key not in dependencies for key in identities):
        raise ProjectError("missing_document", "Selected resource is absent from this catalog")
    selected = list(identities)
    closure: set[str] = set()
    visiting: set[str] = set()

    def visit(key):
        if key in visiting:
            raise ProjectError("dependency_conflict", "Resource dependency cycle")
        if key in closure:
            return
        visiting.add(key)
        for child in dependencies[key]:
            visit(child)
        visiting.remove(key)
        closure.add(key)

    for key in selected:
        visit(key)
    return selected, sorted(closure)


def _business_key(kind: str, data: dict) -> tuple[str, str] | None:
    if kind == "binary_asset":
        return kind, data["role"] + ":" + data["sha256"] + ":" + str(data["size"])
    if kind in {"material_profile", "optical_material"}:
        return kind, data["material"]["id"].casefold()
    if kind == "expression_template":
        return kind, data["template_id"]
    if kind == "film":
        return kind, data["id"]
    return None


def _rewrite_materials(value: Any, mapping: Mapping[str, str]) -> None:
    if isinstance(value, dict):
        if isinstance(value.get("material_id"), str) and value["material_id"] in mapping:
            value["material_id"] = mapping[value["material_id"]]
        for child in value.values():
            _rewrite_materials(child, mapping)
    elif isinstance(value, list):
        for child in value:
            _rewrite_materials(child, mapping)


def _conflict_rows(source: dict, closure: list[str], target: dict,
                   selected: list[str]) -> list[dict]:
    source_rows, target_rows = _index(source), _index(target)
    current = {_business_key(row["kind"], target["documents"][key]): key
               for key, row in target_rows.items()
               if _business_key(row["kind"], target["documents"][key]) is not None}
    dependencies = _catalog_dependencies(source)
    rows = []
    for key in closure:
        row, data = source_rows[key], source["documents"][key]
        business = _business_key(row["kind"], data)
        target_id = current.get(business) if business else None
        identical = target_id and (target["documents"][target_id] == data or row["kind"] == "binary_asset")
        conflict = ("identical" if identical else "different") if target_id else ""
        # A profile's paired optics may conflict even when the full profile does
        # not yet exist. Treat that business identity as one coordinated choice.
        if row["kind"] in {"material_profile", "optical_material"}:
            material_id = data["material"]["id"].casefold()
            for other_kind in ("material_profile", "optical_material"):
                other = current.get((other_kind, material_id))
                source_other = next((identity for identity in closure
                                     if source_rows[identity]["kind"] == other_kind and
                                     source["documents"][identity]["material"]["id"].casefold() == material_id), None)
                if other and source_other and target["documents"][other] != source["documents"][source_other]:
                    conflict = "different"
        choices = (["copy", "overwrite", "skip"] if conflict == "different" else
                   ["reuse", "copy", "skip"] if conflict else ["import", "skip"])
        rows.append({"id": key, "kind": row["kind"], "name": _name(row["kind"], data),
                     "dependencies": dependencies[key], "selected": key in selected,
                     "conflict": conflict, "target_id": target_id or "", "choices": choices})
    return rows


def _merge(source: dict, selected: list[str], target: dict, preview_rows: list[dict],
           decisions: Mapping[str, str], *, asset_limits=None) -> tuple[dict, dict, list[str]]:
    source_rows = _index(source)
    source_data = source["documents"]
    source_snapshot_assets = source.get("assets", {})
    row_by_id = {row["id"]: row for row in preview_rows}
    if not isinstance(decisions, Mapping) or set(decisions) - set(row_by_id):
        raise ProjectError("invalid_selection", "Conflict decision names an unknown resource")
    actions = {}
    material_actions: dict[str, str] = {}
    for key, decision in decisions.items():
        row = row_by_id[key]
        if decision not in row["choices"]:
            raise ProjectError("invalid_selection", "Invalid resource conflict decision: " + str(decision))
        if row["kind"] in {"material_profile", "optical_material"}:
            group = source_data[key]["material"]["id"].casefold()
            if group in material_actions and material_actions[group] != decision:
                raise ProjectError("dependency_conflict", "A material and its frozen optics need the same decision")
            material_actions[group] = decision
    for key, row in row_by_id.items():
        group = (source_data[key]["material"]["id"].casefold()
                 if row["kind"] in {"material_profile", "optical_material"} else None)
        action = decisions.get(key)
        if action is None and group is not None:
            action = material_actions.get(group)
        if action is None:
            action = "reuse" if row["conflict"] == "identical" else "import" if not row["conflict"] else None
        # Resolve only decisions in retained root closures. Skipping a root
        # never forces the user to decide conflicts in its unused descendants.
        if action is not None and action not in row["choices"] and not (group and action in {"copy", "overwrite"}):
            raise ProjectError("invalid_selection", "Decision does not apply to this resource")
        actions[key] = action
    blocked = {key for key, action in actions.items() if action == "skip"}
    roots = [key for key in selected if not (set(_selection(source, [key])[1]) & blocked)]
    closure = _selection(source, roots)[1] if roots else []
    for key in closure:
        if actions[key] is None:
            raise ProjectError("dependency_conflict", "Choose copy, overwrite or skip for conflicting resource: " + row_by_id[key]["name"])
        # A reused parent cannot point at newly copied descendants. Require an
        # explicit parent copy/overwrite rather than silently retaining the old
        # binding or mutating an existing target parent.
        if actions[key] == "reuse":
            descendants = set(_selection(source, [key])[1]) - {key}
            if any(actions[child] == "copy" for child in descendants):
                raise ProjectError("dependency_conflict", "Choose copy for the parent resource when copying its dependency: " + row_by_id[key]["name"])
    records = deepcopy(_index(target))
    data = deepcopy(target["documents"])
    asset_sources = deepcopy(target.get("assets", {}))
    mapping, material_ids, changed = {}, {}, []
    counts = {"created": 0, "copied": 0, "overwritten": 0, "reused": 0,
              "skipped": len(set(row_by_id) - set(closure))}
    occupied_materials = {value["material"]["id"].casefold() for key, value in data.items()
                          if records[key]["kind"] in {"material_profile", "optical_material"}}
    for key in closure:
        row, kind = row_by_id[key], source_rows[key]["kind"]
        action = actions[key]
        if kind in {"material_profile", "optical_material"}:
            original = source_data[key]["material"]["id"]
            if action == "copy" and original not in material_ids:
                copy_id = "material_" + uuid.uuid4().hex
                while copy_id.casefold() in occupied_materials:
                    copy_id = "material_" + uuid.uuid4().hex
                material_ids[original] = copy_id
                occupied_materials.add(copy_id.casefold())
            elif action == "overwrite" and row["target_id"]:
                material_ids.setdefault(original, data[row["target_id"]]["material"]["id"])
        if action == "reuse" and row["target_id"]:
            mapping[key] = row["target_id"]
            counts["reused"] += 1
        elif action == "overwrite" and row["target_id"]:
            mapping[key] = row["target_id"]
            changed.append(mapping[key])
            counts["overwritten"] += 1
        else:
            identity = uuid.uuid4().hex
            while identity in records or identity in mapping.values():
                identity = uuid.uuid4().hex
            mapping[key] = identity
            changed.append(identity)
            counts["copied" if action == "copy" else "created"] += 1
    generation = uuid.uuid4().hex
    source_names = {key + EXTENSIONS[row["kind"]]: key for key, row in source_rows.items()}
    for key in closure:
        target_id, kind = mapping[key], source_rows[key]["kind"]
        if actions[key] == "reuse" and row_by_id[key]["target_id"]:
            continue
        value = deepcopy(source_data[key])
        if kind in {"material_profile", "optical_material"}:
            original = value["material"]["id"]
            if original in material_ids:
                value["material"]["id"] = material_ids[original]
                if actions[key] == "copy":
                    value["material"]["name"] = value["material"]["name"][:110] + " (copy)"
        elif kind in {"film", "expression_template"} and actions[key] == "copy":
            value["id" if kind == "film" else "template_id"] = uuid.uuid4().hex
            value["name"] = value["name"][:110] + " (copy)"
        if kind == "binary_asset":
            from .project_binary_assets import ROLE_SUFFIXES
            value["payload_path"] = f"assets/{generation}/{target_id}{ROLE_SUFFIXES[value['role']]}"
            capability = source_snapshot_assets.get(key)
            if capability is None:
                raise ProjectError("missing_dependency", "Selected binary input has no verified source capability")
            asset_sources[target_id] = deepcopy(capability)
        _rewrite_materials(value, material_ids)
        for owner, field, _ in file_references(kind, value):
            child = source_names[PurePosixPath(owner[field]).name]
            owner[field] = mapping[child] + EXTENSIONS[source_rows[child]["kind"]]
        if kind == "deposition_configuration":
            value["process_document_id"] = mapping[value["process_document_id"]]
        data[target_id] = normalize_document(kind, value)
        records[target_id] = {"id": target_id, "kind": kind,
                              "path": f"objects/{generation}/{target_id}{EXTENSIONS[kind]}",
                              "sha256": "0" * 64, "dependencies": []}
    # Both existing and incoming references are UUID filenames. One preview
    # generation lets the ordinary graph validator resolve their identities.
    for key, row in records.items():
        row["path"] = f"objects/{generation}/{key}{EXTENSIONS[row['kind']]}"
    materials = ProjectStore._material_index(records, data)
    for key, row in records.items():
        row["dependencies"] = ProjectStore._dependencies(key, records, data, materials)
    merged = _snapshot(records, data, target["manifest"], asset_sources)
    _validate_snapshot(merged, asset_limits)
    return merged, {**counts, "id_map": mapping,
                    "selected_ids": [mapping[key] for key in roots]}, changed


def _standalone_snapshot(path: Path) -> dict:
    """Capture only explicitly chosen files and their typed dependency closure."""
    records: dict[str, dict] = {}
    data: dict[str, dict] = {}
    paths: dict[Path, str] = {}
    generation = "0" * 32
    total = 0
    supplied = None
    asset_sources = {}
    origin_files: set[str] = {str(path)}

    def add(kind, document, key):
        nonlocal total
        identity = (PurePosixPath(document["payload_path"]).stem if kind == "binary_asset" else
                    hashlib.sha256(key.encode("utf-8")).hexdigest()[:32])
        normalized = normalize_document(kind, document)
        if identity in data:
            if data[identity] != normalized:
                raise ProjectError("dependency_conflict", "One external resource identity has different content")
            return identity
        total += len(json_bytes(normalized))
        if len(records) >= MAX_PROJECT_DOCUMENTS or total > MAX_PROJECT_BYTES:
            raise ProjectError("capacity_exceeded", "External resource catalog exceeds project capacity")
        records[identity] = {"id": identity, "kind": kind,
                             "path": f"objects/{generation}/{identity}{EXTENSIONS[kind]}",
                             "sha256": "0" * 64, "dependencies": []}
        data[identity] = normalized
        return identity

    def optics(kind, document):
        nonlocal supplied
        refs = document_material_references(kind, document)
        if not refs:
            return
        if supplied is None:
            from .project_global_resources import read_global_resources
            global_snapshot = read_global_resources()
            supplied = {value["material"]["id"]: value for key, value in global_snapshot["documents"].items()
                        if _index(global_snapshot)[key]["kind"] == "optical_material"}
        for material_id, _ in refs:
            if material_id not in supplied:
                raise ProjectError("missing_dependency", "Explicit source needs unavailable optical material: " + material_id)
            add("optical_material", supplied[material_id], "optical:" + material_id)

    def profile(record):
        key = "profile:" + record["id"]
        add("material_profile", {"kind": "coat_sim.project_material_profile", "version": 1,
                                  "material": record}, key)
        add("optical_material", {"kind": "coat_sim.project_optical_material", "version": 1,
                                  "material": {"id": record["id"], "name": record["name"],
                                               "notes": record.get("notes", ""),
                                               "optical_sets": record.get("optical_constants", [])}},
            "optical:" + record["id"])

    def native_simulation(kind, payload):
        from .project_binary_assets import metadata_for_file, ROLE_SUFFIXES
        from .project_simulation_resources import normalize_simulation_document, simulation_asset_references
        document = normalize_simulation_document(kind, {"kind": "coat_sim.project_" + kind,
            "version": 1, "name": path.stem[:120] or kind, "payload": payload}, managed=False)
        for owner, field, role, _slot in simulation_asset_references(kind, document):
            target = Path(owner[field])
            target = Path(os.path.abspath(target if target.is_absolute() else path.parent / target))
            identity = hashlib.sha256((role + ":" + str(target)).encode()).hexdigest()[:32]
            metadata = metadata_for_file(target, role,
                f"assets/{generation}/{identity}{ROLE_SUFFIXES[role]}",
                expected_sha256=owner.get("sha256", owner.get("asset_sha256")))
            add("binary_asset", metadata, "asset:" + str(target))
            asset_sources[identity] = {"path": str(target)}
            owner[field] = identity + ".casset"
            owner["asset_sha256" if field == "asset_path" else "sha256"] = metadata["sha256"]
            if field == "asset_path":
                owner["asset_root"] = "."
        add(kind, document, "file:" + str(path))

    def capture(target, expected=None):
        target = Path(os.path.abspath(target))
        reject_links(target)
        if target in paths:
            identity = paths[target]
            if expected and records[identity]["kind"] != expected:
                raise ProjectError("dependency_conflict", "External dependency kind mismatch")
            return identity
        origin_files.add(str(target))
        kind, value, _ = external_document(target, expected)
        identity = add(kind, value, "file:" + str(target))
        if kind == "binary_asset":
            from .project_binary_assets import stream_asset
            asset_source = {"path": str(target.parent / value["payload_path"])}
            stream_asset(asset_source, value)
            asset_sources[identity] = asset_source
        paths[target] = identity
        for owner, field, child_kind in file_references(kind, data[identity]):
            child = Path(owner[field])
            dependency = capture(child if child.is_absolute() else target.parent / child, child_kind)
            owner[field] = dependency + EXTENSIONS[child_kind]
        optics(kind, data[identity])
        return identity

    if path.suffix.casefold() in {".cray", ".cappearance"}:
        value, _ = read_json(path)
        origin_files.add(str(path))
        if "payload" in value:
            capture(path)
        else:
            native_simulation("raytrace_request" if path.suffix.casefold() == ".cray" else "appearance_configuration", value)
    elif design_kind_from_path(path) or path.suffix.casefold() == ".casset":
        capture(path)
    elif path.suffix.casefold() in {".cmat", ".cmatl"}:
        from .material_business import MaterialLibrary
        reject_links(path)
        try:
            package = MaterialLibrary.parse_package_file(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ProjectError("invalid_document", str(exc)) from exc
        for record in package["records"]:
            profile(record)
    elif path.suffix.casefold() == ".json":
        value, _ = read_json(path)
        kind = {"coat_sim.project_material_profile": "material_profile",
                "coat_sim.project_optical_material": "optical_material"}.get(value.get("kind", ""))
        if value.get("schema") in {"coat_sim.geometric_raytrace_ui_document", "coat_sim.appearance_configuration"}:
            origin_files.add(str(path))
            native_simulation("raytrace_request" if value["schema"] == "coat_sim.geometric_raytrace_ui_document" else "appearance_configuration", value)
        elif kind == "material_profile":
            profile(normalize_document(kind, value)["material"])
        elif kind:
            add(kind, value, "file:" + str(path))
        elif "template_id" in value:
            add("expression_template", value, "file:" + str(path))
            optics("expression_template", value)
        elif value.get("kind") == "coat_sim.project" or path.name.casefold() == "project.json":
            raise ProjectError("invalid_project_file", "Select an explicit .coatproj descriptor")
        else:
            from .materials import validate_material_profile
            try:
                profile(validate_material_profile(value))
            except (ValueError, KeyError, TypeError) as exc:
                raise ProjectError("invalid_document", str(exc)) from exc
    else:
        raise ProjectError("invalid_document", "Unsupported resource source suffix")
    materials = ProjectStore._material_index(records, data)
    for key, row in records.items():
        row["dependencies"] = ProjectStore._dependencies(key, records, data, materials)
    snapshot = _snapshot(records, data, {"kind": "coat_sim.project", "version": 1,
        "id": hashlib.sha256(str(path).encode()).hexdigest()[:32], "name": path.name[:100],
        "revision": 0, "documents": [], "default_configuration_id": None}, asset_sources)
    snapshot["_source_files"] = sorted(origin_files)
    _validate_snapshot(snapshot)
    return snapshot


@dataclass
class _Catalog:
    snapshot: dict
    loader: Callable[[], dict]
    source_type: str
    source_path: str
    fingerprint: str
    signature: tuple | None = None


@dataclass
class _Plan:
    catalog_id: str
    selected: list[str]
    target_type: str
    target_path: str
    target_snapshot: dict
    target_store: ProjectStore | None
    target_service: Any
    rows: list[dict]
    source_snapshot: dict | None = None
    asset_owner: Any = None
    applying: bool = False


class _PlanSpool:
    """An idempotent temporary owner with an exchange-wide byte reservation."""
    def __init__(self, exchange, size):
        self.exchange, self.size, self._cleaned = exchange, size, False
        with exchange._token_lock:
            if exchange._closed:
                raise ProjectError("project_closed", "Resource exchange owner has closed")
            exchange.service.store.asset_limits.check_total(exchange._spool_bytes + exchange._staging_bytes + size)
            exchange._spool_bytes += size
        try:
            self.directory = tempfile.TemporaryDirectory(prefix="coat-resource-plan-")
        except BaseException:
            with exchange._token_lock:
                exchange._spool_bytes -= size
            raise
        self.name = self.directory.name

    def cleanup(self):
        with self.exchange._token_lock:
            if self._cleaned:
                return
            try:
                self.directory.cleanup()
            finally:
                self._cleaned = True
                self.exchange._spool_bytes -= self.size


class ProjectResourceExchange:
    """Per-service owner of short-lived, immutable browser and transfer tokens."""
    def __init__(self, service):
        self.service = service
        self._token_lock = RLock()
        self._closed = False
        self._spool_bytes = 0
        self._staging_bytes = 0
        self._catalogs: dict[str, _Catalog] = {}
        self._plans: dict[str, _Plan] = {}

    @contextmanager
    def _reserve_staging(self, size):
        with self._token_lock:
            if self._closed:
                raise ProjectError("project_closed", "Resource exchange owner has closed")
            self.service.store.asset_limits.check_total(self._spool_bytes + self._staging_bytes + size)
            self._staging_bytes += size
        try:
            yield
        finally:
            with self._token_lock:
                self._staging_bytes -= size

    def _assert_live(self):
        with self._token_lock:
            if self._closed:
                raise ProjectError("project_closed", "The resource exchange owner has closed")
        with self.service._operation():
            pass

    def inspect_resources(self, source="project") -> dict:
        self._assert_live()
        if source == "project":
            source_path = str(self.service.store.project_file)
            source_type = "project"
            def loader():
                return ProjectStore.open(source_path, asset_limits=self.service.store.asset_limits).snapshot_saved_inputs()
        elif source == "global":
            from .project_global_resources import read_global_resources
            source_path, source_type = "global", "global"
            loader = read_global_resources
        else:
            path = Path(os.path.abspath(Path(source).expanduser()))
            reject_links(path)
            source_path = str(path)
            if path.suffix.casefold() == ".coatproj":
                source_type = "project"
                def loader():
                    return ProjectStore.open(path, asset_limits=self.service.store.asset_limits).snapshot_saved_inputs()
            elif path.suffix.casefold() == ".cproject":
                source_type = "package"

                def loader():
                    from coat_sim.project_package import read_project_package
                    return read_project_package(path, asset_limits=self.service.store.asset_limits)

            else:
                source_type = "file"
                def loader():
                    return _standalone_snapshot(path)
        snapshot = loader()
        _validate_snapshot(snapshot, self.service.store.asset_limits)
        catalog_id = uuid.uuid4().hex
        catalog = _Catalog(deepcopy(snapshot), loader, source_type, source_path, _digest(snapshot))
        if source_type in {"project", "package", "file"} or snapshot.get("assets"):
            catalog.signature = self._source_signature(catalog)
        with self.service._operation(), self._token_lock:
            if self._closed:
                raise ProjectError("project_closed", "Resource owner closed while browsing")
            self._catalogs[catalog_id] = catalog
            # A dialog can replace its catalog repeatedly without unbounded RAM.
            if len(self._catalogs) > 16:
                active = {plan.catalog_id for plan in self._plans.values() if plan.applying}
                oldest = next((key for key in self._catalogs if key not in active), None)
                if oldest is not None:
                    self.release_resource_catalog(oldest)
        dependencies = _catalog_dependencies(snapshot)
        rows = [{"id": row["id"], "kind": row["kind"],
                 "name": _name(row["kind"], snapshot["documents"][row["id"]]),
                 **_information(row["kind"], snapshot["documents"][row["id"]]),
                 "dependencies": dependencies[row["id"]], "read_only": True}
                for row in snapshot["manifest"]["documents"]]
        for row in rows:
            if row["kind"] in {"material_profile", "optical_material"}:
                row["material_id"] = snapshot["documents"][row["id"]]["material"]["id"]
        # Usage metadata is presentation-only and preserves the existing material
        # library's role/legacy-category rules. Optical-only records have no
        # declared role in this format, so do not infer a film/substrate use.
        from .materials import material_category
        for row in rows:
            if row["kind"] != "material_profile":
                continue
            material = snapshot["documents"][row["id"]]["material"]
            roles = material.get("roles")
            uses = [role for role in (roles or []) if role in {"film", "substrate", "medium"}]
            if roles is None:
                uses = (["unspecified"] if material.get("optical_only") and "category" not in material
                        else [material_category(material)])
            row["material_uses"] = uses or ["other"]
        return {"catalog_id": catalog_id, "source_type": source_type,
                "source_path": source_path, "name": snapshot["manifest"]["name"],
                "categories": sorted({row["kind"] for row in rows}), "rows": rows,
                "warnings": []}

    @staticmethod
    def _source_signature(catalog):
        from .project_binary_assets import file_signature
        paths = set() if catalog.source_type == "global" else {catalog.source_path}
        if catalog.source_type == "project":
            root = Path(catalog.source_path).parent
            paths.update(str(root / row["path"]) for row in catalog.snapshot["manifest"]["documents"])
        paths.update(catalog.snapshot.get("_source_files", []))
        for source in catalog.snapshot.get("assets", {}).values():
            paths.add(source.get("path", source.get("package")))
        return tuple(file_signature(path) for path in sorted(paths))

    def _catalog(self, catalog_id: str, *, revalidate=True) -> _Catalog:
        with self._token_lock:
            catalog = self._catalogs.get(catalog_id)
        if catalog is None:
            raise ProjectError("expired_catalog", "Resource browser is closed; browse the source again")
        if revalidate:
            if catalog.signature is not None:
                try:
                    if self._source_signature(catalog) != catalog.signature:
                        raise ProjectError("stale_source", "Binary resource source changed after browsing")
                except ProjectError as exc:
                    raise ProjectError("stale_source", "Binary resource source changed or became unavailable") from exc
                if catalog.source_type in {"project", "package"} or (
                        catalog.source_type == "file" and catalog.snapshot.get("assets")):
                    return catalog
            try:
                current = catalog.loader()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ProjectError("stale_source", "Resource source changed or became unavailable; browse it again") from exc
            if _digest(current) != catalog.fingerprint:
                raise ProjectError("stale_source", "Resource source changed after browsing; browse it again")
        return catalog

    def preview_resource_transfer(self, catalog_id, selected_ids, target="project") -> dict:
        self._assert_live()
        catalog = self._catalog(catalog_id)
        selected, closure = _selection(catalog.snapshot, selected_ids)
        store = None
        target_service = None
        if target == "project":
            target_type, target_path = "project", str(self.service.store.project_file)
            store = self.service.store
            target_service = self.service
            snapshot = ProjectStore.open(store.project_file, asset_limits=self.service.store.asset_limits).snapshot_saved_inputs()
        elif target == "global":
            from .project_global_resources import read_global_resources
            target_type, target_path = "global", "global"
            if len({_index(catalog.snapshot)[key]["kind"] for key in selected}) != 1:
                raise ProjectError("invalid_selection", "Global export accepts one resource kind at a time")
            snapshot = read_global_resources()
        else:
            path = Path(os.path.abspath(Path(target).expanduser()))
            reject_links(path)
            target_path = str(path)
            if path.suffix.casefold() == ".coatproj":
                target_type = "project"
                store = (self.service.store if path == self.service.store.project_file
                         else ProjectStore.open(path))
                target_service = self.service if store is self.service.store else None
                host = self.service._session_host
                if host is not None:
                    session = next((item for item in host.sessions()
                                    if item.binding.root == path.parent), None)
                    if session is not None:
                        owned = host.store_for(session.binding)
                        if owned.project_file != path:
                            raise ProjectError("dependency_conflict", "Choose the descriptor owned by the open target project")
                        store = owned
                        workspace = host.resource_for(session.binding, "resource_workspace")
                        if workspace is not None:
                            target_service = workspace.service
                        else:
                            from .project_resource_service import ProjectResourceService
                            target_service = ProjectResourceService(store, session_host=host, binding=session.binding)
                if target_service is not None:
                    with target_service._operation():
                        pass
                snapshot = ProjectStore.open(store.project_file, asset_limits=self.service.store.asset_limits).snapshot_saved_inputs()
            elif path.suffix.casefold() == ".cproject":
                target_type = "package"
                if path.exists():
                    raise ProjectError("destination_exists", "Package destination already exists")
                snapshot = {"manifest": {**deepcopy(catalog.snapshot["manifest"]),
                    "documents": [], "default_configuration_id": None}, "documents": {}}
            else:
                raise ProjectError("invalid_project_file", "Choose a target .coatproj descriptor or new .cproject package")
        if (target_type == "project" and catalog.source_type == "project" and
                Path(catalog.source_path).parent == Path(target_path).parent):
            raise ProjectError("same_project", "Source and target projects must be different")
        rows = _conflict_rows(catalog.snapshot, closure, snapshot, selected)
        identity = uuid.uuid4().hex
        owner = None
        captured_source = None
        asset_ids = [key for key in closure if _index(catalog.snapshot)[key]["kind"] == "binary_asset"]
        if asset_ids:
            from .project_binary_assets import capture_asset, ROLE_SUFFIXES
            budget = sum(catalog.snapshot["documents"][key]["size"] for key in asset_ids)
            self.service.store.asset_limits.check_total(budget)
            owner = _PlanSpool(self, budget)
            try:
                captured_source = deepcopy(catalog.snapshot)
                for key in asset_ids:
                    metadata = catalog.snapshot["documents"][key]
                    captured_source["assets"][key] = capture_asset(catalog.snapshot["assets"][key], metadata,
                        Path(owner.name) / (key + ROLE_SUFFIXES[metadata["role"]]))
                self._catalog(catalog_id)
            except BaseException:
                owner.cleanup()
                raise
        try:
            plan = _Plan(catalog_id, selected, target_type, target_path,
                         deepcopy(snapshot), store, target_service, deepcopy(rows), captured_source, owner)
            with self.service._operation(), self._token_lock:
                if self._closed or catalog_id not in self._catalogs:
                    raise ProjectError("expired_catalog", "Resource browser was closed while previewing")
                self._plans[identity] = plan
                if len(self._plans) > 32:
                    oldest = next((key for key, value in self._plans.items() if not value.applying), None)
                    if oldest is not None:
                        evicted = self._plans.pop(oldest)
                        if evicted.asset_owner is not None:
                            evicted.asset_owner.cleanup()
        except BaseException:
            if owner is not None:
                owner.cleanup()
            raise
        return {"plan_id": identity, "rows": rows, "selected_ids": selected,
                "dependency_ids": sorted(set(closure) - set(selected)),
                "target_type": target_type, "target_path": target_path}
    def apply_resource_transfer(self, plan_id, decisions=None) -> dict:
        self._assert_live()
        with self._token_lock:
            plan = self._plans.get(plan_id)
            if plan is None:
                raise ProjectError("expired_plan", "Resource transfer was canceled or completed; preview again")
            if plan.applying:
                raise ProjectError("transfer_in_progress", "This resource transfer is already being applied")
            plan.applying = True
        try:
            return self._apply_reserved(plan_id, plan, decisions)
        except BaseException:
            with self._token_lock:
                if self._plans.get(plan_id) is plan:
                    plan.applying = False
                    if self._closed:
                        self._plans.pop(plan_id, None)
                        if plan.asset_owner is not None:
                            plan.asset_owner.cleanup()
            raise

    def _apply_reserved(self, plan_id: str, plan: _Plan,
                        decisions: Mapping[str, str] | None) -> dict:
        # Source hashing, archive validation, target capture and graph preparation
        # never hold a session-host/resource lock across heavy read-only I/O.
        catalog = self._catalog(plan.catalog_id)
        if plan.target_type == "project":
            current = ProjectStore.open(plan.target_path, asset_limits=self.service.store.asset_limits).snapshot_saved_inputs()
            if _digest(current) != _digest(plan.target_snapshot):
                raise ProjectError("stale_target", "Target project changed after preview")
        elif plan.target_type == "global":
            from .project_global_resources import read_global_resources
            if _digest(read_global_resources()) != _digest(plan.target_snapshot):
                raise ProjectError("stale_target", "Global library changed after preview")
        elif Path(plan.target_path).exists():
            raise ProjectError("destination_exists", "Package destination appeared after preview")
        merged, result, changed = _merge(plan.source_snapshot or catalog.snapshot, plan.selected, plan.target_snapshot,
                                        plan.rows, {} if decisions is None else decisions,
                                        asset_limits=self.service.store.asset_limits)
        self._catalog(plan.catalog_id)
        if plan.target_type == "project":
            assert plan.target_store is not None
            with plan.target_store.stage_asset_publications(_index(merged), merged["documents"],
                    merged.get("assets"), reservation_scope=self._reserve_staging) as prepared_assets:
                self._catalog(plan.catalog_id)
                with self._publication_scope(plan_id, plan):
                    if changed:
                        plan.target_store._publish(_index(merged), merged["documents"], assets=prepared_assets)
        elif plan.target_type == "global":
            from .project_global_resources import write_global_resources
            if changed and result["selected_ids"]:
                details = write_global_resources(plan.target_snapshot, merged, changed,
                    result["selected_ids"], publication_scope=lambda: self._publication_scope(plan_id, plan))
                result.update(details)
            else:
                with self._publication_scope(plan_id, plan):
                    result["files"] = []
        elif changed:
            from coat_sim.project_package import export_project
            prepared = _PreparedProject(Path(tempfile.gettempdir()), merged["manifest"], "", merged["documents"], merged.get("assets"))
            prepared.asset_limits = self.service.store.asset_limits
            result.update(export_project(prepared, plan.target_path,
                publication_scope=lambda: self._publication_scope(plan_id, plan)))
        else:
            with self._publication_scope(plan_id, plan):
                pass
        result["target_path"] = plan.target_path
        result["replaced_ids"] = [row["target_id"] for row in plan.rows
                                  if row["target_id"] and row["target_id"] in changed]
        with self._token_lock:
            self._plans.pop(plan_id, None)
        if plan.asset_owner is not None:
            plan.asset_owner.cleanup()
        return result

    @contextmanager
    def _publication_scope(self, plan_id: str, plan: _Plan):
        # Never acquire source and target locks in transfer-direction order.
        services = {id(self.service): self.service}
        if plan.target_service is not None:
            services[id(plan.target_service)] = plan.target_service
        ordered = sorted(services.values(), key=lambda value: (str(value.root), id(value)))
        with ExitStack() as stack:
            for service in ordered:
                stack.enter_context(service._operation(mutate=True))
            with self._token_lock:
                if self._closed or self._plans.get(plan_id) is not plan:
                    raise ProjectError("expired_plan", "Resource transfer was canceled before publication")
                catalog = self._catalogs.get(plan.catalog_id)
            if catalog is None:
                raise ProjectError("expired_catalog", "Resource browser closed before publication")
            if catalog.signature is not None:
                try:
                    if self._source_signature(catalog) != catalog.signature:
                        raise ProjectError("stale_source", "Resource source changed during preparation")
                except ProjectError as exc:
                    raise ProjectError("stale_source", "Resource source changed before publication") from exc
            if plan.target_type == "project":
                assert plan.target_store is not None
                plan.target_store._current()
                if plan.target_store.manifest != plan.target_snapshot["manifest"]:
                    raise ProjectError("stale_target", "Target project changed after preview")
            yield

    def cancel_resource_transfer(self, plan_id):
        with self._token_lock:
            plan = self._plans.get(plan_id)
            if plan is not None and plan.applying:
                raise ProjectError("transfer_in_progress", "Publication has started; wait for the transfer result")
            self._plans.pop(plan_id, None)
            if plan is not None and plan.asset_owner is not None:
                plan.asset_owner.cleanup()

    def release_resource_catalog(self, catalog_id):
        with self._token_lock:
            if any(plan.catalog_id == catalog_id and plan.applying for plan in self._plans.values()):
                raise ProjectError("transfer_in_progress", "An active transfer still owns this resource catalog")
            self._catalogs.pop(catalog_id, None)
            for identity, plan in list(self._plans.items()):
                if plan.catalog_id == catalog_id:
                    self._plans.pop(identity, None)
                    if plan.asset_owner is not None:
                        plan.asset_owner.cleanup()


    def close(self):
        """Release idle private bytes; an applying operation retains its owner."""
        with self._token_lock:
            self._closed = True
            for identity, plan in list(self._plans.items()):
                if not plan.applying:
                    self._plans.pop(identity, None)
                    if plan.asset_owner is not None:
                        plan.asset_owner.cleanup()
            active = {plan.catalog_id for plan in self._plans.values()}
            for identity in list(self._catalogs):
                if identity not in active:
                    self._catalogs.pop(identity, None)
