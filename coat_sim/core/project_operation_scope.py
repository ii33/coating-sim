"""Pure capture and validation of project-local mutation scopes.

A capture is evidence about persisted bytes, not a validated input snapshot.
Unchanged documents retain their original manifest rows and bytes. Only current
schema targets and the graph affected by them enter strict validation. Explicit
removal can discard an unsupported test asset, but never cascade or assume that
an unreadable possible referrer has no references.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
from pathlib import PurePosixPath
import stat

from .project_dependencies import (
    EXTENSIONS, KINDS, MAX_FILE_BYTES, MAX_PROJECT_BYTES, MAX_PROJECT_DOCUMENTS,
    ProjectError, document_material_references, identifier, json_bytes,
    normalize_document, parse_json, portable_path, reject_links,
)
from .project_paths import managed_document_path


# This is a closed current-schema capability contract, not a permissive fallback.
# A new managed kind must explicitly declare every kind it can directly reference.
_REFERENCE_KINDS = {
    "source": frozenset(), "mask": frozenset(), "substrate": frozenset(),
    "optical_material": frozenset(), "material_profile": frozenset(),
    "binary_asset": frozenset(),
    "machine": frozenset({"source", "mask"}),
    "process": frozenset({"machine", "substrate"}),
    "deposition_configuration": frozenset({"process"}),
    "film": frozenset({"optical_material"}),
    "expression_template": frozenset({"optical_material"}),
    "raytrace_request": frozenset({"binary_asset"}),
    "appearance_configuration": frozenset({"binary_asset"}),
    "project_bbs_post": frozenset({"project_bbs_post", "binary_asset"}),
}
_CURRENT_SCHEMA_VERSIONS = {
    "source": 2, "machine": 5, "process": 7, "substrate": 1,
    "film": 1, "expression_template": 1,
}
_ENVELOPE_KINDS = {
    "deposition_configuration": "coat_sim.deposition_configuration",
    "optical_material": "coat_sim.project_optical_material",
    "material_profile": "coat_sim.project_material_profile",
    "binary_asset": "coat_sim.project_binary_asset",
    "project_bbs_post": "coat_sim.project_bbs_post",
    "raytrace_request": "coat_sim.project_raytrace_request",
    "appearance_configuration": "coat_sim.project_appearance_configuration",
}


def _sha(content):
    return hashlib.sha256(content).hexdigest()


class OpaqueDocument(dict):
    """Display-only evidence for JSON that could not be parsed as an object.

    It is deliberately distinguishable from an ordinary document. Publication
    never normalizes or serializes this marker as if it were saved input data.
    """
    def __init__(self, *, name, raw_digest, reason):
        super().__init__(name=name)
        self.raw_digest = raw_digest
        self.reason = reason


class ScopedRecords(dict):
    """Working membership and immutable-by-convention capture evidence."""
    def __init__(self, records, fingerprint, canonical_digests):
        super().__init__(deepcopy(records))
        self.fingerprint = fingerprint
        self.base_records = deepcopy(records)
        self.canonical_digests = dict(canonical_digests)
        self.required_ids: set[str] = set()


def _document_digest(document):
    if isinstance(document, OpaqueDocument):
        return "opaque:" + document.raw_digest
    return _sha(json_bytes(document))


def _capabilities():
    if set(_REFERENCE_KINDS) != set(KINDS):
        raise ProjectError("reference_unconfirmed", "Managed kinds have an incomplete typed-reference capability contract")


def _read_managed(store, row):
    path = store.root / managed_document_path(row["path"], row["kind"], row["id"])
    reject_links(path)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ProjectError("unsafe_path", "Project document is not an ordinary file: " + row["id"])
        if info.st_size > MAX_FILE_BYTES:
            raise ProjectError("capacity_exceeded", "Project JSON document exceeds size limit")
        with path.open("rb") as stream:
            content = stream.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError as exc:
        raise ProjectError("missing_dependency", "Project document is missing: " + row["id"]) from exc
    except OSError as exc:
        raise ProjectError("io_error", "Cannot read project document: " + row["id"]) from exc
    if len(content) > MAX_FILE_BYTES:
        raise ProjectError("capacity_exceeded", "Project JSON document exceeds size limit")
    digest = _sha(content)
    if digest != row["sha256"]:
        raise ProjectError("integrity_error", "Project document digest does not match: " + row["id"])
    try:
        document = parse_json(content)
        if not isinstance(document, dict):
            raise ProjectError("invalid_document", "Project document must be a JSON object")
    except (ProjectError, RecursionError) as exc:
        if isinstance(exc, ProjectError) and exc.code not in {"invalid_json", "invalid_document"}:
            raise
        document = OpaqueDocument(name=PurePosixPath(row["path"]).stem,
                                  raw_digest=digest, reason=getattr(exc, "code", "invalid_json"))
    size = len(content) if isinstance(document, OpaqueDocument) else max(len(content), len(json_bytes(document)))
    return document, size


def capture_inputs(store, *, max_project_bytes=None):
    """Capture the full manifest without accepting unrelated document schemas."""
    _capabilities()
    manifest = store._current()
    fingerprint = store._fingerprint
    records = {row["id"]: deepcopy(row) for row in manifest["documents"]}
    data, digests, total = {}, {}, 0
    total_limit = MAX_PROJECT_BYTES if max_project_bytes is None else min(MAX_PROJECT_BYTES, max_project_bytes)
    for identity, row in records.items():
        document, size = _read_managed(store, row)
        total += size
        if total > total_limit:
            raise ProjectError("capacity_exceeded", "Managed project input JSON exceeds the safety capacity limit")
        data[identity] = document
        digests[identity] = _document_digest(document)
    store._current()
    if fingerprint != store._fingerprint:
        raise ProjectError("stale_project", "Project changed during mutation capture")
    return ScopedRecords(records, fingerprint, digests), data


def _reference_schema(kind, document):
    """Confirm the *current* reference grammar without migrating old documents."""
    if isinstance(document, OpaqueDocument) or not isinstance(document, Mapping):
        raise ProjectError("reference_unconfirmed", "Possible referrer is not a readable project JSON object")
    if kind in _CURRENT_SCHEMA_VERSIONS:
        if type(document.get("schema_version")) is not int or document["schema_version"] != _CURRENT_SCHEMA_VERSIONS[kind]:
            raise ProjectError("reference_unconfirmed", "Possible referrer does not use the current reference schema: " + kind)
    elif kind in _ENVELOPE_KINDS:
        if (document.get("kind") != _ENVELOPE_KINDS[kind]
                or type(document.get("version")) is not int or document["version"] != 1):
            raise ProjectError("reference_unconfirmed", "Possible referrer does not use the current reference schema: " + kind)
    elif kind != "mask":
        raise ProjectError("reference_unconfirmed", "Unknown typed-reference schema: " + kind)


def _logical_indexes(records, data, *, required_kinds, allow_ambiguous=False):
    """Identity checks do not accept the rest of an unrelated material schema."""
    optical: dict[str, str] = {}
    seen: dict[str, dict[str, str]] = {"optical_material": {}, "material_profile": {}, "expression_template": {}}
    for identity, row in records.items():
        kind = row["kind"]
        if kind not in seen:
            continue
        document = data[identity]
        try:
            if isinstance(document, OpaqueDocument):
                raise KeyError("opaque")
            key = document["template_id"] if kind == "expression_template" else document["material"]["id"]
            if not isinstance(key, str) or not key or len(key) > 160:
                raise KeyError("identity")
        except (KeyError, TypeError):
            if kind in required_kinds:
                raise ProjectError("reference_unconfirmed", "Project logical identity cannot be confirmed: " + identity) from None
            continue
        folded = key.casefold()
        if folded in seen[kind]:
            if not allow_ambiguous:
                raise ProjectError("dependency_conflict", "Duplicate project logical identity: " + key)
            # An unsupported/deleted old asset is not granted a made-up ID.
            # A referrer needing an ambiguous old optical ID fails extraction.
            optical = {name: value for name, value in optical.items() if name.casefold() != folded}
            continue
        seen[kind][folded] = identity
        if kind == "optical_material":
            optical[key] = identity
    return optical


def _typed_dependencies(identity, records, data, materials):
    """Extract only declared managed references; arbitrary strings are not edges."""
    row, document = records[identity], data[identity]
    kind = row["kind"]
    _reference_schema(kind, document)
    found = set()

    def add(dependency, expected):
        identifier(dependency)
        if dependency not in records:
            raise ProjectError("missing_dependency", "Typed project reference is absent: " + dependency)
        if records[dependency]["kind"] != expected:
            raise ProjectError("dependency_conflict", "Typed project reference has the wrong kind: " + dependency)
        found.add(dependency)

    def file_reference(value, expected):
        reference = portable_path(value)
        path = PurePosixPath(reference)
        if path.name != reference or path.suffix != EXTENSIONS[expected]:
            raise ProjectError("dependency_conflict", "Typed project reference must be a stable UUID handle")
        add(path.stem, expected)

    if kind == "process":
        file_reference(document["machine_file"], "machine")
        file_reference(document["substrate_file"], "substrate")
    elif kind == "machine":
        for collection, expected in (("sources", "source"), ("masks", "mask")):
            entries = document[collection]
            if not isinstance(entries, list):
                raise TypeError("Typed placement collection must be a list")
            for entry in entries:
                if not isinstance(entry, Mapping):
                    raise TypeError("Typed placement entry must be an object")
                file_reference(entry["file"], expected)
    elif kind == "deposition_configuration":
        add(document["process_document_id"], "process")
    elif kind in {"film", "expression_template"}:
        for material_id, _set_id in document_material_references(kind, document):
            dependency = materials.get(material_id)
            if dependency is None:
                raise ProjectError("missing_dependency", "Project optical material is absent: " + material_id)
            add(dependency, "optical_material")
    elif kind in {"raytrace_request", "appearance_configuration"}:
        from .project_simulation_resources import simulation_asset_references
        for owner, field, _role, _slot in simulation_asset_references(kind, document):
            file_reference(owner[field], "binary_asset")
    elif kind == "project_bbs_post":
        if document["parent_id"] is not None:
            add(document["parent_id"], "project_bbs_post")
        revisions = document["history"]
        if not isinstance(revisions, list):
            raise TypeError("BBS history must be a list")
        for revision in revisions:
            attachments = revision.get("attachments", [])
            if not isinstance(attachments, list):
                raise TypeError("BBS attachments must be a list")
            for attachment in attachments:
                add(attachment["asset_id"], "binary_asset")
    return sorted(found)


def _confirmed_dependencies(identity, records, data, materials):
    try:
        return _typed_dependencies(identity, records, data, materials)
    except (ProjectError, KeyError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, ProjectError) and exc.code == "reference_unconfirmed":
            raise
        raise ProjectError("reference_unconfirmed", "Possible project references cannot be confirmed: " + identity) from exc


def prepare_publication(store, records, data, *, required_ids=()):
    """Validate the exact changed/required scope and return strict and retained rows.

    The three outputs are strict records, their normalized data, and all original
    unchanged rows. Strict and retained records may overlap. This function writes
    nothing, and removal never normalizes an unsupported target into acceptance.
    """
    _capabilities()
    current = store._current()
    if not isinstance(records, ScopedRecords):
        raise ProjectError("invalid_document", "Mutation publication requires a complete scoped capture")
    if records.fingerprint != store._fingerprint:
        raise ProjectError("stale_project", "Project changed after mutation inputs were captured")
    base = records.base_records
    if base != {row["id"]: row for row in current["documents"]}:
        raise ProjectError("stale_project", "Mutation capture differs from the current project manifest")
    if set(records) != set(data) or set(records.canonical_digests) != set(base):
        raise ProjectError("invalid_document", "Mutation capture membership does not match its data")
    if len(records) > MAX_PROJECT_DOCUMENTS:
        raise ProjectError("capacity_exceeded", "Project has too many managed input documents")
    for identity, row in records.items():
        identifier(identity)
        if (not isinstance(row, Mapping) or set(row) != {"id", "kind", "path", "sha256", "dependencies"}
                or row.get("id") != identity or row.get("kind") not in _REFERENCE_KINDS):
            raise ProjectError("invalid_document", "Invalid mutation document record")
        if identity in base and row != base[identity]:
            raise ProjectError("stale_document", "Mutation cannot rewrite an existing manifest record")
    changed = {key for key in records if key not in base
               or _document_digest(data[key]) != records.canonical_digests[key]}
    removed = set(base) - set(records)
    required = set(required_ids) | records.required_ids
    if not required <= set(records):
        raise ProjectError("missing_document", "Required mutation target is absent")
    if current["default_configuration_id"] in removed:
        raise ProjectError("dependency_conflict", "The default project document cannot be removed")
    # Even a simultaneously removed referrer cannot authorize cascading removal.
    if any(removed.intersection(row["dependencies"]) for row in base.values()):
        raise ProjectError("dependency_conflict", "Referenced project documents cannot be removed")

    strict_kinds = {records[key]["kind"] for key in changed | required}
    identity_kinds = {"optical_material", "material_profile", "expression_template"}
    required_identity_kinds = identity_kinds.intersection(strict_kinds)
    for identity in changed | required:
        kind = records[identity]["kind"]
        if kind not in {"film", "expression_template"}:
            continue
        try:
            references = document_material_references(kind, data[identity])
        except (KeyError, TypeError, AttributeError) as exc:
            raise ProjectError("invalid_document", "Current material references are invalid: " + identity) from exc
        if references:
            required_identity_kinds.add("optical_material")
    materials = _logical_indexes(records, data, required_kinds=required_identity_kinds)

    # Old material identity is needed only to prove incoming references to edited
    # or removed optical records. Read those bytes on demand, not a second full
    # project-sized deep copy of every JSON document.
    original_data = dict(data)
    for key in (changed | removed).intersection(base):
        if base[key]["kind"] == "optical_material":
            original_data[key], _ = _read_managed(store, base[key])
    for key in removed:
        if key not in original_data:
            original_data[key] = OpaqueDocument(name=key, raw_digest=base[key]["sha256"], reason="removed")
    old_materials = _logical_indexes(base, original_data, required_kinds=set(), allow_ambiguous=True)

    affected = changed | removed
    confirmed = {}
    while True:
        target_kinds = {records[key]["kind"] if key in records else base[key]["kind"] for key in affected}
        added = set()
        for identity, row in base.items():
            if identity in changed or identity in removed:
                continue
            if not (_REFERENCE_KINDS[row["kind"]].intersection(target_kinds)
                    or affected.intersection(row["dependencies"])):
                continue
            if identity not in confirmed:
                confirmed[identity] = _confirmed_dependencies(identity, base, original_data, old_materials)
            dependencies = confirmed[identity]
            if dependencies != sorted(row["dependencies"]):
                raise ProjectError("dependency_conflict", "Manifest dependency IDs do not match actual project references: " + identity)
            if removed.intersection(dependencies):
                raise ProjectError("dependency_conflict", "A typed project reference prevents removal")
            if affected.intersection(dependencies):
                added.add(identity)
        if not added - affected:
            break
        affected.update(added)

    strict_data, dependencies, visiting = {}, {}, set()

    def visit(identity):
        if identity in visiting:
            raise ProjectError("dependency_conflict", "Project dependency cycle")
        if identity in strict_data:
            return
        if identity not in records:
            raise ProjectError("missing_dependency", "Changed project dependency is absent")
        document = data[identity]
        if isinstance(document, OpaqueDocument):
            raise ProjectError("reference_unconfirmed", "Unsupported project data cannot become a mutation input: " + identity)
        try:
            normalized = normalize_document(records[identity]["kind"], document)
            _reference_schema(records[identity]["kind"], document)
            if normalized != document:
                raise ProjectError("invalid_document", "Mutation requires a normalized current-schema document: " + identity)
        except ProjectError as exc:
            if identity in changed or identity in required:
                raise
            raise ProjectError("reference_unconfirmed", "Affected saved project input is unsupported: " + identity) from exc
        visiting.add(identity)
        if identity in changed or identity in required:
            try:
                children = _typed_dependencies(identity, records, data, materials)
            except ProjectError:
                raise
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise ProjectError("invalid_document", "Invalid current project reference fields: " + identity) from exc
        else:
            children = _confirmed_dependencies(identity, records, data, materials)
        if identity not in changed and children != sorted(records[identity]["dependencies"]):
            raise ProjectError("dependency_conflict", "Manifest dependency IDs do not match document references: " + identity)
        for dependency in children:
            visit(dependency)
        dependencies[identity] = children
        strict_data[identity] = normalized
        visiting.remove(identity)

    for identity in sorted((affected - removed) | required):
        visit(identity)
    strict_records = {key: deepcopy(records[key]) for key in strict_data}
    validation_records = {**records, **{key: {**strict_records[key], "dependencies": dependencies[key]}
                                      for key in strict_records}}
    store._validate_graph(validation_records, strict_data)
    store._current()
    retained = {key: deepcopy(row) for key, row in base.items() if key in records and key not in changed}
    return strict_records, strict_data, retained
