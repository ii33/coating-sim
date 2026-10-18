"""Versioned, backend-independent assistant toolkit contracts (stdlib only)."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

API_VERSION = '1'

class ToolkitError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def plain(value):
    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value

@dataclass(frozen=True)
class ToolSpec:
    name: str
    capability: str
    description: str
    parameters: Mapping[str, Any]
    effect: str = 'read'
    version: str = API_VERSION

    def __post_init__(self):
        object.__setattr__(self, 'parameters', _freeze(self.parameters))

    def to_dict(self):
        return {key: plain(getattr(self, key)) for key in ('name', 'capability', 'description', 'parameters', 'effect', 'version')}

@dataclass(frozen=True)
class ToolContext:
    allowed_capabilities: frozenset[str] = frozenset()
    allowed_roots: tuple[Path, ...] = ()
    authorize: Callable[[ToolSpec, Mapping[str, Any]], bool] = field(default=lambda spec, args: False, repr=False)
    cancelled: Callable[[], bool] = field(default=lambda: False, repr=False)
    providers: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        object.__setattr__(self, 'allowed_capabilities', frozenset(self.allowed_capabilities))
        object.__setattr__(self, 'allowed_roots', tuple(Path(root).absolute() for root in self.allowed_roots))
        object.__setattr__(self, 'providers', MappingProxyType(dict(self.providers)))
