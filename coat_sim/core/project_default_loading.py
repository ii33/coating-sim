"""Build an opening plan from the project's last successfully saved configuration.

The persisted manifest, not file timestamps or the last session, owns opening. In
particular, a saved film document is not a live dependency of a deposition
configuration just because its name or design ID resembles an embedded film.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Literal

from .project_dependencies import ProjectError, json_bytes
from .project_store import ProjectStore


DefaultState = Literal["empty_project", "no_default", "ready"]


@dataclass(frozen=True)
class ProjectDefaultPlan:
    """Immutable-by-value opening result; all exposed JSON is detached.

    ``graph`` is the existing resolved project graph, including the selected
    configuration, process runtime, input snapshot, and document closure. An
    inline frozen film, if present, is process provenance rather than a live
    managed ``film`` document. No editor or calculation is constructed here.
    """

    state: DefaultState
    project_id: str
    project_revision: int
    configuration_id: str | None
    process_document_id: str | None
    _graph_bytes: bytes | None = field(default=None, repr=False)
    _film_bytes: bytes | None = field(default=None, repr=False)

    @property
    def configured(self) -> bool:
        return self.state == "ready"

    @property
    def graph(self) -> dict | None:
        return None if self._graph_bytes is None else json.loads(self._graph_bytes)

    @property
    def film_design_snapshot(self) -> dict | None:
        return None if self._film_bytes is None else json.loads(self._film_bytes)

    @property
    def film_materials(self) -> list[dict]:
        """Records in the embedded frozen film, never a global catalog."""
        snapshot = self.film_design_snapshot
        return [] if snapshot is None else snapshot["materials"]


def load_project_default(store: ProjectStore) -> ProjectDefaultPlan:
    """Resolve the selected root once or return an explicit unconfigured state.

    ``ProjectStore.resolve_default`` performs all typed dependency, hash and
    runtime validation. Its ``ProjectError`` is intentionally propagated: an
    invalid selected root must not fall back to another configuration, a film
    in the project, recovery metadata, or an application-wide data directory.
    """
    manifest = store.manifest
    default_id = manifest["default_configuration_id"]
    graph = store.resolve_default()
    if default_id is None:
        if graph is not None:
            raise ProjectError(
                "dependency_conflict", "An unconfigured project resolved a default graph"
            )
        return ProjectDefaultPlan(
            "empty_project" if not manifest["documents"] else "no_default",
            manifest["id"],
            manifest["revision"],
            None,
            None,
        )

    if (
        graph is None
        or graph.get("project_id") != manifest["id"]
        or graph.get("revision") != manifest["revision"]
        or graph.get("root_document_id") != default_id
    ):
        raise ProjectError("stale_project", "Project default changed while it was being loaded")
    configuration = graph.get("configuration")
    if not isinstance(configuration, dict):
        raise ProjectError(
            "invalid_dependency_graph", "Default configuration is absent from its graph"
        )
    process_id = configuration.get("process_document_id")
    processes = [
        row for row in graph["documents"] if row["id"] == process_id and row["kind"] == "process"
    ]
    if len(processes) != 1 or "runtime" not in graph or "input_snapshot" not in graph:
        raise ProjectError("invalid_dependency_graph", "Default process closure is incomplete")
    film = processes[0]["data"].get("film_design_snapshot")
    return ProjectDefaultPlan(
        "ready",
        manifest["id"],
        manifest["revision"],
        default_id,
        process_id,
        json_bytes(graph),
        None if film is None else json_bytes(film),
    )
