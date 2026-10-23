"""Project-local film editing/persistence and existing numerical-service adapter.

The global material catalog is an optional read-only picker input. The project
store owns every persisted film/template and its complete optical closure;
global records are captured only when a document explicitly references them.

Every project additionally owns immutable packaged-seed starters (ideal Air)
that take precedence over the global catalog; see ``project_material_defaults``.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Protocol, TYPE_CHECKING, cast

if TYPE_CHECKING:
    from coat_sim.project_session import ProjectBinding, ProjectSessionHost

from .film_design import expand_expression, new_design, validate_design
from .film_design_service import FilmDesignService
from .film_expression_store import _normalize_template
from .optical_constants import evaluate_optical_set
from .plugins import PluginRegistry
from .project_dependencies import ProjectError, normalize_optical_material, reject_links
from .project_dependencies import document_material_references, external_document
from .project_material_defaults import project_starter_materials, project_starter_usage
from .project_paths import project_document_handle
from .tfcalc_import import (build_tfcalc_design, read_tfcalc_file,
                            register_tfcalc_plugin)


class ProjectFilmStore(Protocol):
    @property
    def root(self) -> Path: ...

    @property
    def manifest(self) -> dict[str, Any]: ...

    def resolve_document(self, document_id: str) -> dict[str, Any]: ...

    def create_document(self, kind: str, document: Mapping[str, Any], *,
                        materials: Mapping[str, Any] | None = None) -> str: ...

    def import_document(self, path: Path | str, *,
                        materials: Mapping[str, Any] | None = None) -> str: ...

    def save_document(self, document_id: str, document: Mapping[str, Any], *,
                      materials: Mapping[str, Any] | None = None) -> None: ...

    def save_film_document(self, document: Mapping[str, Any], *, document_id: str | None = None,
                           expected_revision: str | None = None,
                           materials: Mapping[str, Any] | None = None) -> str: ...


@dataclass(frozen=True)
class ProjectFilmDocument:
    """Detached saved value; its revision is the managed document SHA-256."""

    project_id: str
    document_id: str
    project_revision: int
    revision: str
    design: dict[str, Any]
    handle: Path


class ProjectOpticalSnapshot:
    """An immutable owned snapshot of explicit project optical resources.

    Frozen-film records have no catalog role metadata. A caller may supply
    explicit picker usage; absent usage, records are available to every role.
    Returned values are detached and cannot change admitted calculations.
    """

    def __init__(self, records: Iterable[Mapping[str, Any]], *,
                 usage: Mapping[str, Iterable[str]] | None = None):
        self._initialize(tuple(normalize_optical_material(row) for row in records), usage)

    def _initialize(self, values: tuple[dict[str, Any], ...],
                    usage: Mapping[str, Iterable[str]] | None) -> None:
        # Values reach this method only from normalization or an already-owned
        # snapshot. Public reads always detach them; internal composition may
        # share immutable-owned records without copying/revalidating datasets.
        self._values = values
        self._by_id = {row["id"]: row for row in values}
        identifiers = [row["id"].casefold() for row in self._values]
        if len(identifiers) != len(set(identifiers)):
            raise ProjectError("dependency_conflict", "Project optical material IDs are not unique")
        known = {row["id"] for row in self._values}
        purposes = frozenset({"film", "substrate", "medium"})
        self._usage = ({identity: purposes for identity in known} if usage is None
                       else {identity: frozenset(roles) for identity, roles in usage.items()})
        if set(self._usage) - known or any(roles - purposes for roles in self._usage.values()):
            raise ProjectError("dependency_conflict", "Project material picker usage is invalid")

    @classmethod
    def _combine(cls, primary: ProjectOpticalSnapshot,
                 fallback: ProjectOpticalSnapshot, *, all_primary_roles: bool = False
                 ) -> ProjectOpticalSnapshot:
        """Compose owned snapshots; never admit unvalidated caller records."""
        identities = {row["id"].casefold() for row in primary._values}
        remaining = tuple(row for row in fallback._values
                          if row["id"].casefold() not in identities)
        usage = {row["id"]: ({"film", "substrate", "medium"} if all_primary_roles
                             else primary._usage.get(row["id"], ()))
                 for row in primary._values}
        usage.update({row["id"]: fallback._usage.get(row["id"], ()) for row in remaining})
        combined = cls.__new__(cls)
        combined._initialize((*primary._values, *remaining), usage)
        return combined

    def _same_values(self, other: ProjectOpticalSnapshot) -> bool:
        return self._values == other._values and self._usage == other._usage

    def _records(self) -> list[dict[str, Any]]:
        return deepcopy(list(self._values))

    def get(self, material_id: str) -> dict[str, Any]:
        row = self._by_id.get(material_id)
        if row is None:
            raise ProjectError("missing_dependency", "Project optical material is missing: " + material_id)
        return deepcopy(row)

    def options(self, purpose: str = "film") -> list[dict[str, Any]]:
        return self.options_many((purpose,))[purpose]

    def options_many(self, purposes: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        selected = tuple(dict.fromkeys(purposes))
        if any(purpose not in {"film", "substrate", "medium"} for purpose in selected):
            raise ValueError("Material purpose must be film/substrate/medium")
        return {purpose: [{**deepcopy(row), "readonly": True} for row in self._values
                          if purpose in self._usage.get(row["id"], ())]
                for purpose in selected}

    def evaluate(self, material_id: str, optical_set_id: str, wavelength_nm: float,
                 assume_lossless: bool = False) -> complex:
        if type(assume_lossless) is not bool:
            raise ValueError("A lossless assumption must be an explicit bool")
        material = self.get(material_id)
        optical = next((row for row in material["optical_sets"]
                        if row["id"] == optical_set_id), None)
        if optical is None:
            raise ProjectError("missing_dependency", "Project optical set is missing: " + optical_set_id)
        n, k = evaluate_optical_set(optical, wavelength_nm, "nm")
        if k is None and not assume_lossless:
            raise ValueError("Unknown k requires an explicit lossless assumption")
        return complex(n, 0.0 if k is None else k)


class ProjectFilmService:
    """Service-injection seam for FilmDesignSession/FilmDesignQuickController.

    Managed path handles are stable logical addresses, not files to overwrite.
    Existing standalone file dialogs still require a project-aware UI adapter.
    Explicit create/import/load/save methods expose document IDs and revisions.
    One service instance supplies one editing client. Its legacy load/save
    revision cache is not changed by the explicit headless film operations.
    """

    def __init__(self, project_store: ProjectFilmStore, *,
                 session_host: ProjectSessionHost | None = None,
                 binding: ProjectBinding | None = None,
                 global_material_library: Any | None = None):
        if (session_host is None) != (binding is None):
            raise ValueError("A project session host and binding must be supplied together")
        self._session_host = session_host
        self.binding = binding
        if session_host is not None and binding is not None:
            if session_host.store_for(binding) is not project_store:
                raise ProjectError("dependency_conflict", "Service store differs from its project session")
        self.project_store = project_store
        self.root = Path(project_store.root).resolve()
        self.project_id: str = project_store.manifest["id"]
        from .material_business import application_material_library
        self.global_material_library = (application_material_library()
                                        if global_material_library is None
                                        else global_material_library)
        self.global_catalog_error = ""
        self._lock = RLock()
        self._loaded_revisions: dict[str, str] = {}
        self.plugins = PluginRegistry()
        register_tfcalc_plugin(self.plugins)
        self._draft_materials: ProjectOpticalSnapshot | None = None
        # Packaged-seed starters (currently ideal Air) are owned by every
        # project and take precedence over the mutable global library so a
        # new draft never depends on, and never rewrites, global Air.
        starter_records = project_starter_materials()
        self._starter_materials = ProjectOpticalSnapshot(
            starter_records, usage=project_starter_usage(starter_records))
        self._project_materials = ProjectOpticalSnapshot(())
        self._global_materials = ProjectOpticalSnapshot(())
        self.materials = ProjectOpticalSnapshot(())
        self.refresh_materials()

    @contextmanager
    def _operation(self) -> Iterator[None]:
        # Always enter host before service/store locks; close cannot overtake
        # an admitted mutation, and a stale service cannot retain write access.
        if self._session_host is not None and self.binding is not None:
            with self._session_host.operation_scope(self.binding, close_policy=True):
                with self._lock:
                    yield
        else:
            with self._lock:
                yield

    def _rows(self, kind: str) -> list[dict[str, Any]]:
        rows = []
        for record in self.project_store.manifest["documents"]:
            if record["kind"] != kind:
                continue
            graph = self.project_store.resolve_document(record["id"])
            rows.append(next(row for row in graph["documents"] if row["id"] == record["id"]))
        return rows

    def refresh_materials(self) -> None:
        """Re-read project and global inputs, including cross-window changes."""
        self._refresh_materials(refresh_global=True)

    def _refresh_materials(self, *, refresh_global: bool) -> None:
        with self._operation():
            records = [row["data"]["material"] for row in self._rows("optical_material")]
            project_ids = {row["id"].casefold() for row in records}
            usage: dict[str, set[str]] = {}
            # Full project profiles own explicit picker roles. Unreferenced
            # legacy optical-only records preserve their previous usage rules.
            for row in self._rows("material_profile"):
                profile = row["data"]["material"]
                roles = set(profile.get("roles", [])) & {"film", "substrate", "medium"}
                if "roles" not in profile:
                    category = profile.get("category", "film")
                    roles = ({"substrate", "medium"} if category == "substrate" else
                             {"film"} if category == "film" else set())
                usage.setdefault(profile["id"], set()).update(roles)

            def bind(purpose: str, reference: Mapping[str, Any]) -> None:
                identity = reference.get("material_id")
                if identity:
                    usage.setdefault(identity, set()).add(purpose)

            for row in self._rows("film"):
                design = row["data"]
                bind("substrate", design["substrate"])
                for key in ("incident_medium", "exit_medium"):
                    if design[key]["kind"] == "material":
                        bind("medium", design[key])
                film_rows = [*design["front_layers"], *design["back_layers"],
                             *design["expression"]["symbols"].values()]
                film_rows.extend(design.get("workflow_settings", {}).get(
                    "optimization", {}).get("structure_materials", []))
                for reference in film_rows:
                    bind("film", reference)
            for row in self._rows("expression_template"):
                for reference in row["data"]["expression"]["symbols"].values():
                    bind("film", reference)
            # Poll the mutable global catalog on explicit refresh / pre-save.
            # A publication immediately after that check only needs to refresh
            # project-owned values and roles, not read and normalize it again.
            if refresh_global:
                try:
                    global_rows = self.global_material_library.catalog_records()
                    detached_global, global_usage = [], {}
                    for item in global_rows:
                        profile = item["record"]
                        identity = profile["id"]
                        optical_sets = profile.get("optical_constants") or []
                        if not optical_sets or identity.casefold() in project_ids:
                            continue
                        detached_global.append({
                            "id": identity, "name": profile["name"],
                            "notes": profile.get("notes", ""),
                            "optical_sets": optical_sets,
                        })
                        roles = set(profile.get("roles", [])) & {"film", "substrate", "medium"}
                        if "roles" not in profile:
                            category = profile.get("category", "film")
                            roles = ({"substrate", "medium"} if category == "substrate" else
                                     {"film"} if category == "film" else set())
                        global_usage[identity] = roles
                    candidate_global = ProjectOpticalSnapshot(detached_global, usage=global_usage)
                    if not candidate_global._same_values(self._global_materials):
                        self._global_materials = candidate_global
                    self.global_catalog_error = ""
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    # Do not keep stale global records when the source fails.
                    self._global_materials = ProjectOpticalSnapshot(())
                    self.global_catalog_error = str(exc)
            project = ProjectOpticalSnapshot(records, usage=usage)
            # Precedence: project-owned records > packaged starter seed > mutable
            # global catalog. The starter holds ideal Air so a new project stays
            # stable even when the global Air is edited or tombstoned, and the
            # project never rewrites the global library to restore it.
            owned = ProjectOpticalSnapshot._combine(project, self._starter_materials)
            candidate = ProjectOpticalSnapshot._combine(owned, self._global_materials)
            # Preserve provider identity for no-op refreshes. Sessions can then
            # avoid rebuilding full picker datasets and derived rows on Save.
            if not candidate._same_values(self._project_materials):
                self._project_materials = candidate
                self.materials = self._material_view(self._draft_materials)

    def _material_transfer(self, kind: str, document: Mapping[str, Any],
                           materials: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        """Capture only missing, explicitly referenced optical records.

        Passing even an empty mapping is an explicit caller decision and is
        preserved so ProjectStore can report missing dependencies.
        """
        if materials is not None:
            return materials
        references = document_material_references(kind, dict(document))
        if not references:
            return None
        existing = {row["data"]["material"]["id"].casefold()
                    for row in self._rows("optical_material")}
        captured = {}
        for material_id, _set_id in references:
            if material_id.casefold() in existing:
                continue
            value = self.materials.get(material_id)
            captured[material_id] = {
                "id": value["id"], "name": value["name"],
                "notes": value.get("notes", ""),
                "optical_sets": deepcopy(value["optical_sets"]),
            }
        return captured or None

    def _material_view(self, draft: ProjectOpticalSnapshot | None) -> ProjectOpticalSnapshot:
        if draft is None:
            return self._project_materials
        return ProjectOpticalSnapshot._combine(draft, self._project_materials,
                                              all_primary_roles=True)

    def activate_frozen_draft_materials(self, records: Iterable[Mapping[str, Any]]) -> None:
        """Expose one detached draft's optical closure to editing and numerics.

        Existing project records remain available except where the frozen
        draft owns the same ID. This affects this workspace service only and
        never writes or replaces a managed material.
        """
        candidate = ProjectOpticalSnapshot(records)
        with self._operation():
            active = self._material_view(candidate)
            self._draft_materials = candidate
            self.materials = active

    def clear_frozen_draft_materials(self) -> None:
        with self._operation():
            self._draft_materials = None
            self.materials = self._project_materials

    def material_options(self, purpose: str = "film") -> list[dict[str, Any]]:
        with self._operation():
            return self.materials.options(purpose)

    def material_options_many(self, purposes: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        with self._operation():
            return self.materials.options_many(purposes)

    def picker_material_options_many(self, purposes: Iterable[str], *,
                                     design: Mapping[str, Any] | None = None) -> dict[str, list[dict[str, Any]]]:
        """Return the latest catalog for pickers, independent of frozen numerics.

        A live film editor may keep an older material value for its current
        design while another window updates the project's same-ID material.
        The picker can show the latest catalog row; computations continue to
        use ``self.materials`` and its immutable draft overlay.
        """
        with self._operation():
            if self._draft_materials is None:
                return self._project_materials.options_many(purposes)
            # Latest project/global rows win; draft-only materials remain
            # available without normalizing a second copy of the full catalog.
            picker = ProjectOpticalSnapshot._combine(self._project_materials,
                                                    self._draft_materials)
            # Frozen numerical closures intentionally own complete values, but
            # their existence alone grants no picker usage. Only catalog roles
            # and actual positions in this design establish picker membership.
            usage = {identity: set(roles) for identity, roles in self._project_materials._usage.items()}
            if design is not None:
                def bind(role, reference):
                    identity = reference.get("material_id") if isinstance(reference, Mapping) else None
                    if identity in picker._by_id:
                        usage.setdefault(identity, set()).add(role)
                bind("substrate", design.get("substrate", {}))
                for reference in [*design.get("front_layers", []), *design.get("back_layers", []),
                                  *design.get("expression", {}).get("symbols", {}).values(),
                                  *design.get("workflow_settings", {}).get("optimization", {}).get("structure_materials", [])]:
                    bind("film", reference)
                for endpoint in ("incident_medium", "exit_medium"):
                    reference = design.get(endpoint, {})
                    if reference.get("kind") == "material":
                        bind("medium", reference)
            picker._initialize(picker._values, {identity: roles for identity, roles in usage.items()
                                                if identity in picker._by_id})
            return picker.options_many(purposes)

    def catalog_material(self, material_id: str) -> dict[str, Any]:
        """Read the latest project/global catalog snapshot, not draft overlay."""
        with self._operation():
            return self._project_materials.get(material_id)

    def new_design(self) -> dict[str, Any]:
        return new_design()

    def validate(self, design: Mapping[str, Any]) -> dict[str, Any]:
        return validate_design(design)

    def expand(self, expression: str, symbols: Mapping[str, Any]) -> Any:
        return expand_expression(expression, symbols)

    def document_handle(self, document_id: str) -> Path:
        # The resolve call validates identity/membership before creating a
        # logical handle. No documents/ directory or alias file is created.
        with self._operation():
            graph = self.project_store.resolve_document(document_id)
            root = next(row for row in graph["documents"] if row["id"] == document_id)
            if root["kind"] != "film":
                raise ProjectError("dependency_conflict", "The document is not a film")
            return project_document_handle(self.root, document_id, "film")

    def _film_document(self, document_id: str) -> ProjectFilmDocument:
        graph = self.project_store.resolve_document(document_id)
        row = next(row for row in graph["documents"] if row["id"] == document_id)
        if row["kind"] != "film":
            raise ProjectError("dependency_conflict", "The document is not a film")
        if graph["project_id"] != self.project_id:
            raise ProjectError("dependency_conflict", "The project store identity changed")
        return ProjectFilmDocument(
            self.project_id, document_id, graph["revision"], row["sha256"],
            deepcopy(row["data"]), project_document_handle(self.root, document_id, "film"))

    def load_document(self, document_id: str) -> ProjectFilmDocument:
        with self._operation():
            return self._film_document(document_id)

    def create_film(self, design: Mapping[str, Any] | None = None, *,
                    materials: Mapping[str, Any] | None = None) -> ProjectFilmDocument:
        normalized = self.new_design() if design is None else self.validate(design)
        with self._operation():
            transfer = self._material_transfer("film", normalized, materials)
            identity = self.project_store.save_film_document(normalized, materials=transfer)
            self._refresh_materials(refresh_global=False)
            return self.load_document(identity)

    def import_film(self, path: Path | str, *,
                    materials: Mapping[str, Any] | None = None) -> ProjectFilmDocument:
        if Path(path).suffix.lower() != ".cfilm":
            raise ProjectError("dependency_conflict", "Film import requires a .cfilm document")
        with self._operation():
            _, normalized, _ = external_document(Path(path).expanduser().absolute(), "film")
            transfer = self._material_transfer("film", normalized, materials)
            identity = self.project_store.import_document(path, materials=transfer)
            self._refresh_materials(refresh_global=False)
            return self.load_document(identity)

    def save_document(self, document_id: str, design: Mapping[str, Any], *,
                      expected_revision: str,
                      materials: Mapping[str, Any] | None = None) -> ProjectFilmDocument:
        with self._operation():
            current = self._film_document(document_id)
            if not isinstance(expected_revision, str) or current.revision != expected_revision:
                raise ProjectError("stale_document", "Film changed since the supplied saved revision")
            normalized = self.validate(design)
            if normalized["id"] != current.design["id"]:
                raise ProjectError("dependency_conflict", "Saving cannot replace the film's internal design ID")
            transfer = self._material_transfer("film", normalized, materials)
            self.project_store.save_film_document(normalized, document_id=document_id,
                expected_revision=expected_revision, materials=transfer)
            self._refresh_materials(refresh_global=False)
            return self.load_document(document_id)

    def _handle_id(self, path: Path | str) -> str:
        target = Path(path).expanduser().absolute()
        reject_links(target)
        target = target.resolve()
        for row in self.project_store.manifest["documents"]:
            if row["kind"] != "film":
                continue
            identity = row["id"]
            handles = {
                project_document_handle(self.root, identity, "film"),
            }
            if target in handles:
                return identity
        raise ProjectError("unmanaged_path", "Use a managed film handle; import/create external designs explicitly")

    def load(self, path: Path | str) -> dict[str, Any]:
        with self._operation():
            document = self.load_document(self._handle_id(path))
            self._loaded_revisions[document.document_id] = document.revision
            return document.design

    def save(self, path: Path | str, design: Mapping[str, Any], *,
             overwrite: bool = False) -> dict[str, Any]:
        if type(overwrite) is not bool or not overwrite:
            raise ValueError("Saving an existing managed film requires overwrite=True")
        with self._operation():
            identity = self._handle_id(path)
            revision = self._loaded_revisions.get(identity)
            if revision is None:
                raise ProjectError("revision_required", "Load the managed film before saving it")
            saved = self.save_document(identity, design, expected_revision=revision)
            self._loaded_revisions[identity] = saved.revision
            return saved.design

    def list_saved_designs(self) -> list[dict[str, Any]]:
        with self._operation():
            return [{"design_id": row["id"], "name": row["data"]["name"],
                     "note": row["data"]["note"], "schema_version": row["data"]["schema_version"],
                     "document_revision": row["sha256"]}
                    for row in self._rows("film")]

    def load_saved_design(self, design_id: str) -> dict[str, Any]:
        return self.load(self.document_handle(design_id))

    def save_saved_design(self, design: Mapping[str, Any]) -> dict[str, Any]:
        normalized = self.validate(design)
        with self._operation():
            matches = [row for row in self._rows("film") if row["data"]["id"] == normalized["id"]]
            if len(matches) > 1:
                raise ProjectError("ambiguous_document", "Multiple managed films have this internal design ID")
            if not matches:
                created = self.create_film(normalized)
                self._loaded_revisions[created.document_id] = created.revision
                return created.design
            identity = matches[0]["id"]
            revision = self._loaded_revisions.get(identity)
            if revision is None:
                raise ProjectError("revision_required", "Load the managed film before updating its catalog entry")
            saved = self.save_document(identity, normalized, expected_revision=revision)
            self._loaded_revisions[identity] = saved.revision
            return saved.design

    def list_expression_templates(self) -> list[dict[str, Any]]:
        with self._operation():
            return [{"template_id": row["data"]["template_id"], "name": row["data"]["name"],
                     "note": row["data"]["note"], "schema_version": row["data"]["schema_version"],
                     "document_id": row["id"], "document_revision": row["sha256"]}
                    for row in self._rows("expression_template")]

    def _template_row(self, template_id: str) -> dict[str, Any] | None:
        return next((row for row in self._rows("expression_template")
                     if row["data"]["template_id"] == template_id), None)

    def load_expression_template(self, template_id: str) -> dict[str, Any]:
        with self._operation():
            row = self._template_row(template_id)
            if row is None:
                raise ProjectError("missing_document", "Project expression template is missing")
            self._loaded_revisions[row["id"]] = row["sha256"]
            return deepcopy(row["data"])

    def save_expression_template(self, template: Mapping[str, Any], *,
                                 materials: Mapping[str, Any] | None = None) -> dict[str, Any]:
        normalized = _normalize_template(template)
        with self._operation():
            transfer = self._material_transfer("expression_template", normalized, materials)
            row = self._template_row(normalized["template_id"])
            if row is None:
                identity = self.project_store.create_document(
                    "expression_template", normalized, materials=transfer)
            else:
                revision = self._loaded_revisions.get(row["id"])
                if revision is None:
                    raise ProjectError("revision_required", "Load the managed template before updating it")
                if revision != row["sha256"]:
                    raise ProjectError("stale_document", "Template changed since its loaded revision")
                identity = row["id"]
                self.project_store.save_document(identity, normalized, materials=transfer)
            self._refresh_materials(refresh_global=False)
            saved = self._template_row(normalized["template_id"])
            if saved is None:
                raise ProjectError("missing_document", "Saved expression template cannot be resolved")
            self._loaded_revisions[identity] = saved["sha256"]
            return deepcopy(saved["data"])

    def _numerical_service(self) -> FilmDesignService:
        # Existing frozen CPU evaluation already uses this no-constructor seam.
        # Only validate/freeze/simulate are called; they need no paths/catalog.
        service = FilmDesignService.__new__(FilmDesignService)
        with self._operation():
            service.materials = cast(Any, self.materials)
        return service

    def freeze_design(self, design: Mapping[str, Any], settings: dict[str, Any] | None = None) -> dict:
        return self._numerical_service().freeze_design(design, settings)

    def simulate(self, design: Mapping[str, Any], settings: dict[str, Any] | None = None, *,
                 cancel_event: Any = None) -> dict:
        return self._numerical_service().simulate(design, settings, cancel_event=cancel_event)

    def read_tfcalc(self, path: Path | str) -> Any:
        requested = Path(path).expanduser().absolute()
        reject_links(requested)
        return read_tfcalc_file(requested, self.plugins)

    def build_tfcalc_import(self, document: Any, options: Mapping[str, Any],
                            base_design: Mapping[str, Any]) -> dict[str, Any]:
        from .tfcalc_import import PLUGIN_NAME
        if not self.plugins.is_enabled("film_importer", PLUGIN_NAME):
            raise PermissionError("TFCalc importer is disabled")
        return build_tfcalc_design(document, options, base_design, self.material_options("film"))
