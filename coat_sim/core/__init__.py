"""Stable service entry points for desktop and automation clients."""

from .api import CoatService, Document, ServiceError, ValidationResult
from .plugins import PluginRegistry, PluginSpec

__all__ = [
    "CoatService", "Document", "ServiceError", "ValidationResult",
    "PluginRegistry", "PluginSpec",
]

