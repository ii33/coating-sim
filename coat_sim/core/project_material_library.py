"""Complete project-local material profiles with explicit package import/export."""
from __future__ import annotations
from copy import deepcopy
import hashlib
from pathlib import Path
import uuid
from .material_business import (MaterialLibrary, MATERIAL_PACKAGE_KIND,
                                LIBRARY_PACKAGE_KIND, PACKAGE_VERSION,
                                application_material_library)
from .materials import validate_material_profile, material_completeness, check_material_name
from .project_dependencies import ProjectError, json_bytes


def _envelope(record):
    return {"kind": "coat_sim.project_material_profile", "version": 1, "material": record}


def _fingerprint(record):
    return None if record is None else hashlib.sha256(json_bytes(record)).hexdigest()


class ProjectMaterialLibrary(MaterialLibrary):
    """Read global catalog values and persist edits only in one project."""
    def __init__(self, service, *, global_library=None):
        self.service = service
        self.directory = service.root
        # This provider is used for detached reads only. Project writes always
        # go through the bound ProjectResourceService/ProjectStore.
        self.global_library = (application_material_library() if global_library is None
                                else global_library)
        self.global_catalog_error = ""

    def _rows(self):
        return [(row, self.service.load_document(row["id"]).data["material"])
                for row in self.service.list_documents("material_profile")]

    def _global_rows(self):
        return self.global_library.catalog_records()

    def catalog_records(self):
        local = self._rows()
        local_by_key = {record["id"].casefold() for _, record in local}
        rows = [{"record": deepcopy(record), "builtin": False, "readonly": False,
                 "overridden": False, "origin": "project"} for _, record in local]
        self.global_catalog_error = ""
        try:
            global_rows = self._global_rows()
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # A damaged/unavailable global catalog must not hide project-owned
            # profiles. It is a read-only picker source, not a prerequisite for
            # opening or saving this project's own material records.
            self.global_catalog_error = str(exc)
            global_rows = []
        for item in global_rows:
            record = item["record"]
            if record["id"].casefold() in local_by_key:
                continue
            rows.append({"record": deepcopy(record), "builtin": bool(item.get("builtin")),
                         "readonly": True, "overridden": bool(item.get("overridden")),
                         "origin": "global"})
        return rows

    def _all_effective_records(self):
        return [deepcopy(item["record"]) for item in self.catalog_records()]

    def read(self, identifier):
        match = next((record for _, record in self._rows() if record["id"] == identifier), None)
        if match is not None:
            return {"record": deepcopy(match), "readonly": False, "editable": True,
                    "builtin": False, "overridden": False, "origin": "project",
                    "completeness": material_completeness(match)}
        try:
            item = self.global_library.read(identifier)
        except (ValueError, KeyError) as exc:
            raise ProjectError("missing_document", "Material profile is missing: " + str(identifier)) from exc
        record = item["record"]
        return {"record": deepcopy(record), "readonly": True, "editable": True,
                "builtin": bool(item.get("builtin")), "overridden": bool(item.get("overridden")),
                "origin": "global", "completeness": material_completeness(record)}

    def revisions(self, identifier):
        revisions = {}
        for kind in ("material_profile", "optical_material"):
            for row in self.service.list_documents(kind):
                material = self.service.load_document(row["id"]).data["material"]
                if material["id"] == identifier:
                    revisions[row["id"]] = row["revision"]
        return revisions

    def _optical_operation(self, record, new, updates, revisions, *, allow_existing=False):
        optical = {"kind": "coat_sim.project_optical_material", "version": 1,
                   "material": {"id": record["id"], "name": record["name"],
                                "notes": record.get("notes", ""),
                                "optical_sets": deepcopy(record.get("optical_constants", []))}}
        matching = []
        for row in self.service.list_documents("optical_material"):
            existing = self.service.load_document(row["id"]).data["material"]
            if existing["id"].casefold() == record["id"].casefold():
                matching.append((row, existing))
        if matching:
            if not allow_existing:
                raise ProjectError("dependency_conflict", "Project optical material exists; use an explicit import conflict decision")
            row, existing = matching[0]
            if existing["id"] != record["id"]:
                raise ProjectError("dependency_conflict", "Material and optical IDs differ only by case")
            updates[row["id"]] = optical
            revisions.setdefault(row["id"], row["revision"])
        else:
            new.append(("optical_material", optical))

    def write(self, profile, *, update=False, expected_revisions=None):
        record = validate_material_profile(profile)
        with self.service._operation(mutate=True):
            check_material_name(record, [data for _, data in self._rows()])
            matches = [(row, data) for row, data in self._rows() if data["id"].casefold() == record["id"].casefold()]
            if update and (not matches or matches[0][1]["id"] != record["id"]):
                raise ProjectError("missing_document", "Material update needs an existing exact project ID")
            if not update and matches:
                raise ProjectError("dependency_conflict", "Project material exists; explicit update is required")
            new, updates = [], {}
            revisions = {} if expected_revisions is None else dict(expected_revisions)
            if update:
                row = matches[0][0]
                updates[row["id"]] = _envelope(record)
                revisions.setdefault(row["id"], row["revision"])
            else:
                new.append(("material_profile", _envelope(record)))
            # A local profile may copy-on-write an optical dependency already
            # present in this project. This never reaches the global catalog.
            self._optical_operation(record, new, updates, revisions, allow_existing=True)
            self.service.store.write_documents(new, updates=updates, expected_revisions=revisions)
            return {"record": deepcopy(record), "readonly": False, "editable": True,
                    "builtin": False, "created": not update}

    def delete(self, identifier):
        with self.service._operation(mutate=True):
            match = next(((row, record) for row, record in self._rows() if record["id"] == identifier), None)
            if match is None:
                raise ProjectError("missing_document", "Project material profile is missing")
            self.service.store.remove_document(match[0]["id"], expected_revision=match[0]["revision"])

    def _optical_rows(self):
        return {self.service.load_document(row["id"]).data["material"]["id"].casefold():
                (row, self.service.load_document(row["id"]).data["material"])
                for row in self.service.list_documents("optical_material")}

    def preview_import(self, package):
        if not isinstance(package, dict) or package.get("kind") not in {MATERIAL_PACKAGE_KIND, LIBRARY_PACKAGE_KIND}:
            raise ProjectError("invalid_document", "Invalid material package")
        records = [validate_material_profile(row) for row in package["records"]]
        if len({row["id"].casefold() for row in records}) != len(records):
            raise ProjectError("dependency_conflict", "Duplicate imported material IDs")
        current = {record["id"].casefold(): record for _, record in self._rows()}
        optics = self._optical_rows()
        rows, fingerprints, optical_fingerprints = [], {}, {}
        for record in records:
            key = record["id"].casefold()
            existing, optical = current.get(key), optics.get(key)
            conflict = "custom" if existing else "optical" if optical else ""
            rows.append({"id": record["id"], "name": record["name"], "conflict": conflict,
                         "copy_id": "material_" + uuid.uuid4().hex if conflict else "",
                         "target_id": existing["id"] if existing else optical[1]["id"] if optical else record["id"]})
            fingerprints[record["id"]] = _fingerprint(existing)
            optical_fingerprints[record["id"]] = _fingerprint(optical[1] if optical else None)
        plan = {"version": 1, "kind": package["kind"], "records": records,
                "rows": rows, "fingerprints": fingerprints, "optical_fingerprints": optical_fingerprints}
        return {"plan": plan, "rows": deepcopy(rows), "count": len(rows)}

    def apply_import(self, plan, decisions):
        if not isinstance(plan, dict) or plan.get("version") != 1 or not isinstance(decisions, dict):
            raise ProjectError("invalid_document", "Invalid material import plan or decisions")
        records, rows = plan["records"], plan["rows"]
        if len(records) != len(rows) or set(decisions) - {record["id"] for record in records}:
            raise ProjectError("invalid_document", "Import plan no longer matches its decisions")
        with self.service._operation(mutate=True):
            current = {record["id"].casefold(): (row, record) for row, record in self._rows()}
            optics = self._optical_rows()
            new: list[tuple[str, dict]] = []
            updates: dict[str, dict] = {}
            revisions: dict[str, str] = {}
            counts = {"created": 0, "copied": 0, "overwritten": 0, "skipped": 0}
            targets = set(current) | set(optics)
            effective_profiles = {key: value[1] for key, value in current.items()}
            for record, row in zip(records, rows, strict=True):
                record = validate_material_profile(record)
                if row.get("id") != record["id"]:
                    raise ProjectError("invalid_document", "Invalid material import row")
                identity = record["id"]
                existing, optical = current.get(identity.casefold()), optics.get(identity.casefold())
                if (_fingerprint(existing[1] if existing else None) != plan["fingerprints"].get(identity) or
                        _fingerprint(optical[1] if optical else None) != plan["optical_fingerprints"].get(identity)):
                    raise ProjectError("stale_document", "Project materials changed after import preview")
                conflict = "custom" if existing else "optical" if optical else ""
                if row.get("conflict") != conflict:
                    raise ProjectError("invalid_document", "Import conflict differs from its preview")
                decision = decisions.get(identity, "import" if not conflict else None)
                if decision == "skip":
                    counts["skipped"] += 1
                    continue
                allow_existing = False
                if not conflict:
                    if decision != "import":
                        raise ProjectError("invalid_document", "A new material must be explicitly imported")
                    new.append(("material_profile", _envelope(record)))
                    counts["created"] += 1
                elif decision == "overwrite":
                    allow_existing = True
                    record["id"] = existing[1]["id"] if existing else optical[1]["id"]
                    if existing:
                        updates[existing[0]["id"]] = _envelope(record)
                        revisions[existing[0]["id"]] = existing[0]["revision"]
                    else:
                        new.append(("material_profile", _envelope(record)))
                    counts["overwritten"] += 1
                elif decision == "copy":
                    target = row.get("copy_id")
                    if not isinstance(target, str) or target.casefold() in targets:
                        raise ProjectError("dependency_conflict", "Material copy ID is unavailable")
                    targets.add(target.casefold())
                    record["id"] = target
                    record["name"] = record["name"][:110] + " (copy)"
                    new.append(("material_profile", _envelope(validate_material_profile(record))))
                    counts["copied"] += 1
                else:
                    raise ProjectError("invalid_document", "Choose copy, overwrite or skip for a conflict")
                check_material_name(record, effective_profiles.values())
                effective_profiles[record["id"].casefold()] = record
                self._optical_operation(record, new, updates, revisions, allow_existing=allow_existing)
            self.service.store.write_documents(new, updates=updates, expected_revisions=revisions)
            return counts

    def export_single_file(self, identifier, path):
        package = {"kind": MATERIAL_PACKAGE_KIND, "version": PACKAGE_VERSION,
                   "material": self.read(identifier)["record"]}
        return self.write_export_file(package, Path(path))

    def export_library_file(self, path, include_ids=None):
        package = self.export_library_package(include_ids)
        return self.write_export_file(package, Path(path))
