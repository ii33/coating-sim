"""Explicit, versioned extension registry.

Nothing in this module scans entry points, imports plugin directories, or
executes third-party code automatically. A host must explicitly mark a callable
as trusted before it can be registered.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable


PLUGIN_API_VERSION = 1
PLUGIN_CATEGORIES = frozenset({"source_sampler", "analysis", "exporter", "film_importer"})
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class PluginSpec:
    category: str
    name: str
    api_version: int
    handler: Callable[..., Any]
    description: str = ""


class PluginRegistry:
    """Register only explicitly supplied, trusted callables.

    Names are unique within a category across *all* API versions, preventing
    ambiguous resolution when a plugin is upgraded. An unsupported version is
    rejected before registration rather than silently called with another API.
    """

    def __init__(self) -> None:
        self._plugins: dict[tuple[str, str], PluginSpec] = {}
        self._disabled: set[tuple[str, str]] = set()

    def register(self, spec: PluginSpec, *, trusted: bool = False) -> None:
        if trusted is not True:
            raise PermissionError("插件须由宿主显式标记为可信后才能注册")
        if not isinstance(spec, PluginSpec):
            raise TypeError("插件定义必须是 PluginSpec")
        if spec.category not in PLUGIN_CATEGORIES:
            raise ValueError(f"未知插件类别：{spec.category}")
        if not isinstance(spec.name, str) or not _NAME.fullmatch(spec.name):
            raise ValueError("插件名称须为小写字母开头的字母、数字或下划线")
        if type(spec.api_version) is not int or spec.api_version != PLUGIN_API_VERSION:
            raise ValueError(f"插件 API 版本必须是 {PLUGIN_API_VERSION}")
        if not callable(spec.handler):
            raise TypeError("插件 handler 必须可调用")
        key = (spec.category, spec.name)
        if key in self._plugins:
            raise ValueError(f"插件名称冲突：{spec.category}/{spec.name}")
        self._plugins[key] = spec

    def get(self, category: str, name: str, *, api_version: int = PLUGIN_API_VERSION) -> PluginSpec:
        if type(api_version) is not int or api_version != PLUGIN_API_VERSION:
            raise ValueError(f"不支持插件 API 版本：{api_version}")
        try:
            return self._plugins[(category, name)]
        except KeyError as exc:
            raise KeyError(f"未注册插件：{category}/{name}") from exc

    def list(self, category: str | None = None) -> tuple[PluginSpec, ...]:
        if category is not None and category not in PLUGIN_CATEGORIES:
            raise ValueError(f"未知插件类别：{category}")
        return tuple(spec for spec in self._plugins.values()
                     if category is None or spec.category == category)

    def invoke(self, category: str, name: str, /, *args: Any,
               api_version: int = PLUGIN_API_VERSION, **kwargs: Any) -> Any:
        spec = self.get(category, name, api_version=api_version)
        if not self.is_enabled(category, name):
            raise PermissionError(f"插件已禁用：{category}/{name}")
        return spec.handler(*args, **kwargs)

    def set_enabled(self, category: str, name: str, enabled: bool) -> None:
        """Host-controlled in-memory lifecycle; no module loading or unloading."""
        self.get(category, name)
        if type(enabled) is not bool:
            raise TypeError("插件启停值必须为显式布尔值")
        if enabled:
            self._disabled.discard((category, name))
        else:
            self._disabled.add((category, name))

    def is_enabled(self, category: str, name: str) -> bool:
        self.get(category, name)
        return (category, name) not in self._disabled
