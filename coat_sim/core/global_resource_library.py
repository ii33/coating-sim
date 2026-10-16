"""Global-only resource exchange capabilities, without a hidden project store."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from . import data_files
from .project_binary_assets import DEFAULT_ASSET_LIMITS, AssetReadLimits
from .project_dependencies import ProjectError
from .project_resource_transfer import ProjectResourceExchange


@dataclass(frozen=True)
class _ExchangeBudget:
    # Exchange needs only the existing binary-I/O policy for global/package
    # operations. This is deliberately not a writable ProjectStore.
    asset_limits: AssetReadLimits = DEFAULT_ASSET_LIMITS


class GlobalResourceLibrary:
    """Own short-lived read/transfer tokens and only global publication rights."""

    def __init__(self, *, asset_limits=DEFAULT_ASSET_LIMITS):
        self.store = _ExchangeBudget(asset_limits)
        self.root = data_files.data_path("data")
        self._lock = RLock()
        self._closed = False
        self._exchange = ProjectResourceExchange(self)

    @contextmanager
    def _operation(self, *, mutate=False):
        with self._lock:
            if self._closed:
                raise ProjectError("global_library_closed", "Global resource window has closed")
            yield

    def inspect_resources(self, source="global") -> dict:
        if source == "project":
            raise ProjectError("invalid_selection", "Choose an exact project file for read-only inspection")
        return self._exchange.inspect_resources(source)

    def preview_resource_transfer(self, catalog_id, selected_ids, target="global") -> dict:
        if target != "global" and Path(target).suffix.casefold() != ".cproject":
            raise ProjectError("invalid_selection", "Global manager can import to global libraries or export a new .cproject only")
        return self._exchange.preview_resource_transfer(catalog_id, selected_ids, target)

    def apply_resource_transfer(self, plan_id, decisions=None) -> dict:
        return self._exchange.apply_resource_transfer(plan_id, decisions)

    def cancel_resource_transfer(self, plan_id):
        self._exchange.cancel_resource_transfer(plan_id)

    def release_resource_catalog(self, catalog_id):
        self._exchange.release_resource_catalog(catalog_id)

    @property
    def resource_deletions(self):
        with self._lock:
            if not hasattr(self, "_resource_deletions"):
                from .asset_deletion import AssetDeletionService
                self._resource_deletions = AssetDeletionService(self)
            return self._resource_deletions

    def preview_resource_deletion(self, catalog_id: str, selected_ids: list[str]) -> dict:
        return self.resource_deletions.preview(catalog_id, selected_ids)

    def apply_resource_deletion(self, plan_id: str) -> dict:
        return self.resource_deletions.apply(plan_id)

    def cancel_resource_deletion(self, plan_id: str) -> None:
        self.resource_deletions.cancel(plan_id)

    def list_resource_deletions(self) -> list[dict]:
        return self.resource_deletions.list_receipts()

    def restore_resource_deletion(self, receipt_id: str) -> dict:
        return self.resource_deletions.restore(receipt_id)

    def close(self):
        with self._lock:
            self._closed = True
        self._exchange.close()
