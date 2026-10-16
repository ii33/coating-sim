"""Parse local, data-only theme packages consumed by ThemeService.

This module resolves package-relative resource paths but never loads images or
audio, executes package content, follows URLs, changes settings, or mutates the
process theme service. ThemeService owns selection, token application, and the
read-only resource bridge exposed to QML.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Mapping

from coat_sim.ui.theme_tokens import COLOR_TOKEN_KEYS, OPACITY_TOKEN_KEYS, THEME_TOKEN_KEYS


MANIFEST_NAME = "theme.json"
MAX_MANIFEST_BYTES = 1_048_576
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNPACKED_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_FILE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_FILES = 512
MAX_COMPRESSION_RATIO = 1000
BACKGROUND_MODES = frozenset({"cover", "contain", "stretch", "tile", "center"})
BRAND_COLOR_KEYS = frozenset({"primary", "secondary", "accent"})
THEME_MODES = ("light", "dark")
MODE_PREFERENCES = frozenset({"light", "dark", "system", "inherit"})
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"})
_ICON_EXTENSIONS = _IMAGE_EXTENSIONS | {".ico"}
_SOUND_EXTENSIONS = frozenset({".wav", ".ogg"})
_PACKAGE_RESOURCE_EXTENSIONS = _ICON_EXTENSIONS | _SOUND_EXTENSIONS
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_PACKAGE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SOUND_EVENT = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_COLOR_TOKENS = frozenset(COLOR_TOKEN_KEYS)
_OPACITY_TOKENS = frozenset(OPACITY_TOKEN_KEYS)
_KNOWN_TOKENS = frozenset(THEME_TOKEN_KEYS)

TokenValue = str | int | float


class ThemePackageError(ValueError):
    """Raised when a package manifest is malformed or unsafe to resolve."""


@dataclass(frozen=True, slots=True)
class ThemeBackground:
    image: Path | None
    mode: str


@dataclass(frozen=True, slots=True)
class ThemeMascot:
    floating_icon: Path | None
    accessible_name: str


@dataclass(frozen=True, slots=True)
class ThemeModePackage:
    mode: str
    background: ThemeBackground | None
    taskbar_icon: Path | None
    mascot: ThemeMascot | None
    colors: Mapping[str, str]
    token_overrides: Mapping[str, TokenValue]


@dataclass(frozen=True, slots=True)
class ThemePackage:
    package_id: str
    name: str
    root: Path
    modes: Mapping[str, ThemeModePackage]
    sounds: Mapping[str, Path | None]
    warnings: tuple[str, ...]

    def for_mode(self, mode: str) -> ThemeModePackage:
        """Return the explicit or built-in-fallback data for light/dark mode."""

        if not isinstance(mode, str) or mode not in THEME_MODES:
            raise ThemePackageError("package mode must be 'light' or 'dark'")
        return self.modes[mode]

    def token_overrides_for_service(self, mode: str) -> dict[str, TokenValue]:
        """Return one mode's token patch as a copy for ThemeService."""

        return dict(self.for_mode(mode).token_overrides)


@dataclass(frozen=True, slots=True)
class ThemeSelection:
    """Independent theme identity and mode preference for one UI scope."""

    theme_id: str | None = None
    mode: str = "inherit"

    def __post_init__(self) -> None:
        if self.theme_id is not None and (
            not isinstance(self.theme_id, str) or not _PACKAGE_ID.fullmatch(self.theme_id)
        ):
            raise ThemePackageError("theme_id must use lowercase letters, digits, '.', '_' or '-'")
        if not isinstance(self.mode, str) or self.mode not in MODE_PREFERENCES:
            raise ThemePackageError("mode must be light, dark, system, or inherit")


@dataclass(frozen=True, slots=True)
class ResolvedThemeSelection:
    theme_id: str
    mode: str


@dataclass(frozen=True, slots=True)
class InstalledThemePackage:
    package: ThemePackage
    directory: Path


def load_theme_package(package_root: str | Path) -> ThemePackage:
    """Load ``theme.json`` from one local package directory."""

    root = _package_root(package_root)
    manifest = root / MANIFEST_NAME
    try:
        resolved_manifest = manifest.resolve(strict=True)
        resolved_manifest.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ThemePackageError(f"package manifest is missing or escapes its directory: {manifest}") from exc
    if not resolved_manifest.is_file():
        raise ThemePackageError(f"package manifest is not a file: {resolved_manifest}")
    try:
        if resolved_manifest.stat().st_size > MAX_MANIFEST_BYTES:
            raise ThemePackageError("theme.json exceeds the 1 MiB size limit")
        text = resolved_manifest.read_text(encoding="utf-8")
        payload = json.loads(text, object_pairs_hook=_unique_object)
    except ThemePackageError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ThemePackageError(f"could not read theme.json: {exc}") from exc
    return parse_theme_package(payload, root)


def parse_theme_package(payload: object, package_root: str | Path) -> ThemePackage:
    """Validate manifest data and resolve any declared local resources.

    Missing optional resource files are returned as ``None`` and recorded in
    ``warnings`` so a host can retain its built-in asset. Invalid declarations
    (including absolute paths, URLs, and traversal) fail with a clear error.
    """

    root = _package_root(package_root)
    data = _object(payload, "theme package")
    _only_keys(data, {"formatVersion", "id", "name", "modes", "sounds"}, "theme package")
    version = data.get("formatVersion")
    if type(version) is not int or version != 1:
        raise ThemePackageError("formatVersion must be the integer 1")

    package_id = _text(data.get("id"), "id", maximum=64)
    if not _PACKAGE_ID.fullmatch(package_id):
        raise ThemePackageError("id must use lowercase letters, digits, '.', '_' or '-'")
    name = _text(data.get("name"), "name", maximum=80)

    mode_data = _object(data.get("modes"), "modes")
    _only_keys(mode_data, set(THEME_MODES), "modes")
    if not mode_data:
        raise ThemePackageError("modes must declare at least light or dark")

    warnings: list[str] = []
    modes: dict[str, ThemeModePackage] = {}
    for mode in THEME_MODES:
        if mode not in mode_data:
            warnings.append(f"modes.{mode} is absent; built-in defaults will be used")
        modes[mode] = _parse_mode(mode, mode_data.get(mode, {}), root, warnings)
    sounds = _parse_sounds(data.get("sounds", {}), root, warnings)

    return ThemePackage(
        package_id=package_id,
        name=name,
        root=root,
        modes=MappingProxyType(modes),
        sounds=MappingProxyType(sounds),
        warnings=tuple(warnings),
    )


def resolve_theme_selection(
    global_selection: ThemeSelection,
    window_selection: ThemeSelection | None = None,
    *,
    system_mode: str | None = None,
) -> ResolvedThemeSelection:
    """Resolve independent window identity/mode overrides over global values.

    ``inherit`` at window scope uses the corresponding global field. If the
    global mode is also ``inherit``, it behaves like ``system``. The host must
    pass the platform-resolved ``system_mode`` when that preference is active.
    """

    if not isinstance(global_selection, ThemeSelection):
        raise ThemePackageError("global_selection must be a ThemeSelection")
    if window_selection is not None and not isinstance(window_selection, ThemeSelection):
        raise ThemePackageError("window_selection must be a ThemeSelection or None")
    if global_selection.theme_id is None:
        raise ThemePackageError("global selection must choose a theme_id")

    theme_id = (
        window_selection.theme_id
        if window_selection is not None and window_selection.theme_id is not None
        else global_selection.theme_id
    )
    mode_preference = (
        window_selection.mode
        if window_selection is not None and window_selection.mode != "inherit"
        else global_selection.mode
    )
    if mode_preference == "inherit":
        mode_preference = "system"
    if mode_preference == "system":
        if system_mode not in THEME_MODES:
            raise ThemePackageError("system_mode must be resolved to 'light' or 'dark'")
        mode = system_mode
    else:
        mode = mode_preference
    return ResolvedThemeSelection(theme_id=theme_id, mode=mode)


def _package_root(value: str | Path) -> Path:
    try:
        root = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise ThemePackageError(f"package directory is unavailable: {value}") from exc
    if not root.is_dir():
        raise ThemePackageError(f"package root is not a directory: {root}")
    return root


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ThemePackageError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ThemePackageError(f"{label} must be a JSON object")
    return value


def _only_keys(value: Mapping[str, Any], allowed: set[str] | frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ThemePackageError(f"{label} contains unsupported field(s): {', '.join(unknown)}")


def _text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ThemePackageError(f"{label} must be non-empty text of at most {maximum} characters")
    return value


def _color(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HEX_COLOR.fullmatch(value):
        raise ThemePackageError(f"{label} must be a #RRGGBB color without alpha")
    return value.upper()


def _parse_background(value: object, root: Path, warnings: list[str]) -> ThemeBackground | None:
    data = _object(value, "background")
    _only_keys(data, {"image", "mode"}, "background")
    if "image" not in data:
        raise ThemePackageError("background.image is required when background is declared")
    mode = data.get("mode", "cover")
    if not isinstance(mode, str) or mode not in BACKGROUND_MODES:
        raise ThemePackageError(f"background.mode must be one of: {', '.join(sorted(BACKGROUND_MODES))}")
    return ThemeBackground(
        _resource(root, data["image"], "background.image", warnings, _IMAGE_EXTENSIONS), mode
    )


def _parse_mascot(value: object, root: Path, warnings: list[str]) -> ThemeMascot:
    data = _object(value, "mascot")
    _only_keys(data, {"floatingIcon", "accessibleName"}, "mascot")
    if "floatingIcon" not in data:
        raise ThemePackageError("mascot.floatingIcon is required when mascot is declared")
    accessible_name = _text(data.get("accessibleName", "看板娘"), "mascot.accessibleName", maximum=80)
    icon = _resource(root, data["floatingIcon"], "mascot.floatingIcon", warnings, _IMAGE_EXTENSIONS)
    return ThemeMascot(icon, accessible_name)


def _parse_mode(
    mode: str, value: object, root: Path, warnings: list[str]
) -> ThemeModePackage:
    data = _object(value, f"modes.{mode}")
    _only_keys(data, {"background", "taskbarIcon", "mascot", "colors", "tokenOverrides"}, f"modes.{mode}")
    background = _parse_background(data["background"], root, warnings) if "background" in data else None
    taskbar_icon = (
        _resource(root, data["taskbarIcon"], f"modes.{mode}.taskbarIcon", warnings, _ICON_EXTENSIONS)
        if "taskbarIcon" in data
        else None
    )
    mascot = _parse_mascot(data["mascot"], root, warnings) if "mascot" in data else None
    colors = _parse_brand_colors(data.get("colors", {}))
    token_overrides = _parse_token_overrides(data.get("tokenOverrides", {}))
    return ThemeModePackage(
        mode=mode,
        background=background,
        taskbar_icon=taskbar_icon,
        mascot=mascot,
        colors=MappingProxyType(colors),
        token_overrides=MappingProxyType(token_overrides),
    )


def _parse_sounds(value: object, root: Path, warnings: list[str]) -> dict[str, Path | None]:
    data = _object(value, "sounds")
    sounds: dict[str, Path | None] = {}
    for event, relative_path in data.items():
        if len(event) > 64 or not _SOUND_EVENT.fullmatch(event):
            raise ThemePackageError(f"invalid semantic sound event name: {event!r}")
        sounds[event] = _resource(root, relative_path, f"sounds.{event}", warnings, _SOUND_EXTENSIONS)
    return sounds


def _parse_brand_colors(value: object) -> dict[str, str]:
    data = _object(value, "colors")
    _only_keys(data, BRAND_COLOR_KEYS, "colors")
    return {key: _color(color, f"colors.{key}") for key, color in data.items()}


def _parse_token_overrides(value: object) -> dict[str, TokenValue]:
    data = _object(value, "tokenOverrides")
    overrides: dict[str, TokenValue] = {}
    for key, token_value in data.items():
        if key not in _KNOWN_TOKENS:
            raise ThemePackageError(f"unknown theme token: {key}")
        overrides[key] = _validate_token_value(key, token_value)
    return overrides


def _validate_token_value(key: str, value: object) -> TokenValue:
    if key in _COLOR_TOKENS:
        return _color(value, f"tokenOverrides.{key}")
    if key == "fontFamily":
        return _text(value, f"tokenOverrides.{key}", maximum=128)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ThemePackageError(f"tokenOverrides.{key} must be a finite JSON number")

    if key in _OPACITY_TOKENS:
        minimum, maximum = 0.0, 1.0
    elif key in {"fontScale", "densityScale"}:
        minimum, maximum = 0.75, 2.0
    elif key.startswith("fontSize"):
        minimum, maximum = 0.01, 72.0
    elif key.startswith("fontWeight"):
        return _bounded_integer(key, value, 100, 900)
    elif key == "lineHeightBody":
        minimum, maximum = 0.01, 4.0
    elif key.startswith("controlHeight"):
        minimum, maximum = 0.01, 1000.0
    elif key.startswith("radius"):
        minimum, maximum = 0.0, 100.0
    elif key in {"borderWidth", "focusRingWidth"}:
        minimum, maximum = 0.0, 20.0
    elif key.startswith("duration"):
        return _bounded_integer(key, value, 0, 10000)
    elif key.startswith("space"):
        minimum, maximum = 0.0, 1000.0
    else:
        raise ThemePackageError(f"no value rule is defined for theme token: {key}")

    if not minimum <= value <= maximum:
        raise ThemePackageError(f"tokenOverrides.{key} must be between {minimum:g} and {maximum:g}")
    return value


def _bounded_integer(key: str, value: int | float, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ThemePackageError(f"tokenOverrides.{key} must be an integer from {minimum} to {maximum}")
    return value


def _resource(
    root: Path,
    value: object,
    label: str,
    warnings: list[str],
    extensions: frozenset[str],
) -> Path | None:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ThemePackageError(f"{label} must be a non-empty package-relative path")
    raw_parts = value.split("/")
    windows_path = PureWindowsPath(value)
    if (
        "\\" in value
        or ":" in value
        or value.startswith("/")
        or windows_path.drive
        or any(ord(character) < 32 for character in value)
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise ThemePackageError(f"{label} must be a normalized relative path inside the package")
    pure_path = PurePosixPath(value)
    if pure_path.is_absolute():
        raise ThemePackageError(f"{label} must be a relative path")
    if Path(value).suffix.casefold() not in extensions:
        permitted = ", ".join(sorted(extensions))
        raise ThemePackageError(f"{label} must use one of these file extensions: {permitted}")
    try:
        resolved = root.joinpath(*pure_path.parts).resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ThemePackageError(f"{label} escapes the theme package directory") from exc
    if not resolved.is_file():
        warnings.append(f"{label} resource '{value}' is missing; the built-in default will be used")
        return None
    return resolved


def install_theme_package(archive_path: str | Path, install_root: str | Path) -> InstalledThemePackage:
    """Safely install one data-only theme ZIP below ``install_root``.

    The archive is validated and extracted manually into a temporary directory;
    no archive-provided paths or scripts are executed. The destination package
    ID must not already exist.
    """

    archive = Path(archive_path)
    try:
        if not archive.is_file():
            raise ThemePackageError("theme archive is not a regular file")
        if archive.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ThemePackageError("theme archive exceeds the 64 MiB compressed size limit")
        root_arg = Path(install_root)
        root_arg.mkdir(parents=True, exist_ok=True)
        root = root_arg.resolve(strict=True)
        if not root.is_dir():
            raise ThemePackageError("theme install root is not a directory")
        with zipfile.ZipFile(archive, "r") as bundle:
            entries = _validate_archive(bundle)
            with tempfile.TemporaryDirectory(prefix=".theme-install-", dir=root) as temporary:
                stage = Path(temporary).resolve(strict=True)
                _extract_archive(bundle, entries, stage)
                package = load_theme_package(stage)
                destination = root / package.package_id
                if destination.exists() or destination.is_symlink():
                    raise ThemePackageError(f"theme package is already installed: {package.package_id}")
                stage.rename(destination)
        installed = load_theme_package(destination)
        return InstalledThemePackage(package=installed, directory=destination.resolve(strict=True))
    except ThemePackageError:
        raise
    except (
        OSError,
        RuntimeError,
        EOFError,
        zlib.error,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        NotImplementedError,
    ) as exc:
        raise ThemePackageError(f"could not install theme archive: {exc}") from exc


def _validate_archive(
    bundle: zipfile.ZipFile,
) -> tuple[tuple[zipfile.ZipInfo, tuple[str, ...]], ...]:
    entries = bundle.infolist()
    if len(entries) > MAX_ARCHIVE_FILES:
        raise ThemePackageError(f"theme archive contains more than {MAX_ARCHIVE_FILES} entries")
    if not entries:
        raise ThemePackageError("theme archive is empty")

    total_size = 0
    seen: set[str] = set()
    files: set[str] = set()
    validated: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    has_manifest = False
    for info in entries:
        raw_name = info.filename
        if not isinstance(raw_name, str) or not raw_name or "\\" in raw_name or ":" in raw_name:
            raise ThemePackageError("theme archive contains an invalid entry path")
        if any(ord(character) < 32 or ord(character) == 127 for character in raw_name) or raw_name.startswith("/"):
            raise ThemePackageError("theme archive contains an absolute or control-character path")
        is_directory = info.is_dir()
        normalized = raw_name[:-1] if is_directory and raw_name.endswith("/") else raw_name
        parts = normalized.split("/")
        if not normalized or any(part in {"", ".", ".."} for part in parts):
            raise ThemePackageError("theme archive paths must be normalized package-relative paths")
        for part in parts:
            device_name = part.split(".", 1)[0].rstrip(" .").casefold()
            reserved_device = device_name in {"con", "prn", "aux", "nul"} or (
                len(device_name) == 4
                and device_name[:3] in {"com", "lpt"}
                and device_name[3] in "123456789"
            )
            if part.endswith((".", " ")) or reserved_device:
                raise ThemePackageError("theme archive contains a Windows-reserved path component")
        if PureWindowsPath(normalized).drive or PurePosixPath(normalized).is_absolute():
            raise ThemePackageError("theme archive paths must be relative")
        key = "/".join(parts).casefold()
        if key in seen:
            raise ThemePackageError(f"theme archive contains a duplicate path: {raw_name}")
        seen.add(key)

        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        expected_type = stat.S_IFDIR if is_directory else stat.S_IFREG
        if file_type not in {0, expected_type}:
            raise ThemePackageError(f"theme archive contains a link or special file: {raw_name}")
        if info.flag_bits & 0x1:
            raise ThemePackageError("encrypted theme archives are not supported")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ThemePackageError(f"unsupported compression method for: {raw_name}")

        if not is_directory:
            if info.file_size < 0 or info.file_size > MAX_ARCHIVE_FILE_BYTES:
                raise ThemePackageError(f"theme archive file exceeds the 32 MiB limit: {raw_name}")
            total_size += info.file_size
            if total_size > MAX_UNPACKED_BYTES:
                raise ThemePackageError("theme archive exceeds the 128 MiB unpacked size limit")
            if info.file_size and (
                info.compress_size <= 0 or info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
            ):
                raise ThemePackageError(f"theme archive compression ratio is too high: {raw_name}")
            if normalized != MANIFEST_NAME and Path(normalized).suffix.casefold() not in _PACKAGE_RESOURCE_EXTENSIONS:
                raise ThemePackageError(f"unsupported theme package file type: {raw_name}")
            files.add(key)
            if normalized == MANIFEST_NAME:
                has_manifest = True
        validated.append((info, tuple(parts)))

    for file_key in files:
        if any(other.startswith(file_key + "/") for other in seen):
            raise ThemePackageError(f"theme archive file conflicts with a directory: {file_key}")
    if not has_manifest:
        raise ThemePackageError("theme archive must contain theme.json at its root")
    return tuple(validated)


def _extract_archive(
    bundle: zipfile.ZipFile,
    entries: tuple[tuple[zipfile.ZipInfo, tuple[str, ...]], ...],
    stage: Path,
) -> None:
    total_size = 0
    for info, parts in entries:
        target = stage.joinpath(*parts)
        try:
            target.resolve(strict=False).relative_to(stage)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ThemePackageError(f"theme archive path escapes its staging directory: {info.filename}") from exc
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        file_size = 0
        try:
            with bundle.open(info, "r") as source, target.open("xb") as destination:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    file_size += len(chunk)
                    total_size += len(chunk)
                    if file_size > MAX_ARCHIVE_FILE_BYTES or total_size > MAX_UNPACKED_BYTES:
                        raise ThemePackageError("theme archive exceeded its extraction size limit")
                    if file_size > info.file_size:
                        raise ThemePackageError(f"theme archive size metadata mismatch: {info.filename}")
                    destination.write(chunk)
        except ThemePackageError:
            raise
        except (OSError, RuntimeError, EOFError, zlib.error, zipfile.BadZipFile, NotImplementedError) as exc:
            raise ThemePackageError(f"could not extract theme archive entry {info.filename!r}: {exc}") from exc
        if file_size != info.file_size:
            raise ThemePackageError(f"theme archive size metadata mismatch: {info.filename}")
