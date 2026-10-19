"""Single-writer theme service with a separate read-only bridge per QML engine."""

from __future__ import annotations

import math
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Property, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from PySide6.QtQml import QJSValue

from coat_sim.ui.input_focus import install_input_focus_policy

from coat_sim.ui.theme_package import (
    InstalledThemePackage,
    ThemePackage,
    ThemePackageError,
    install_theme_package,
    load_theme_package,
)

from coat_sim.ui.theme_tokens import (
    THEME_MODES,
    build_theme_snapshot,
)

if TYPE_CHECKING:
    from coat_sim.ui.theme_settings_sync import ThemeSettingsSync

_INSTANCE_KEY = object()


class ThemeEngineBridge(QObject):
    """Read-only QML context object owned by exactly one QQmlEngine."""

    snapshotChanged = Signal()
    revisionChanged = Signal()
    modeChanged = Signal()
    preferenceModeChanged = Signal()
    isDarkChanged = Signal()
    themeChanged = Signal()
    assetsChanged = Signal()

    def __init__(
        self,
        snapshot: dict[str, str | float | int],
        revision: int,
        mode: str,
        preference_mode: str,
        theme_id: str,
        theme_name: str,
        assets: dict[str, Any],
        *,
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        install_input_focus_policy()
        self._snapshot = dict(snapshot)
        self._revision = int(revision)
        self._mode = mode
        self._preference_mode = preference_mode
        self._theme_id = theme_id
        self._theme_name = theme_name
        self._assets = dict(assets)
        engine = parent
        self._freeze_token_values = engine.evaluate(
            "(function (value) { return Object.freeze(value); })"
        )
        if self._freeze_token_values.isError() or not self._freeze_token_values.isCallable():
            raise RuntimeError("Could not prepare the read-only QML theme token snapshot")
        self._token_values = self._make_token_values(self._snapshot)

    def _make_token_values(self, snapshot: dict[str, str | float | int]) -> QJSValue:
        engine = self.parent()
        if engine is None:
            return QJSValue()
        value = engine.toScriptValue(dict(snapshot))
        frozen = self._freeze_token_values.call([value])
        if frozen.isError() or not frozen.isObject():
            raise RuntimeError("Could not freeze the QML theme token snapshot")
        return frozen

    @Property("QVariantMap", notify=snapshotChanged)
    def snapshot(self) -> dict[str, str | float | int]:
        # Return a fresh flat map so QML cannot mutate the service's state.
        return dict(self._snapshot)

    @Property(QJSValue, notify=snapshotChanged)
    def tokenValues(self) -> QJSValue:
        """Stable, frozen JS token object for shared QML control bindings."""

        return self._token_values

    @Property(int, notify=revisionChanged)
    def revision(self) -> int:
        return self._revision

    @Property(str, notify=modeChanged)
    def mode(self) -> str:
        return self._mode

    @Property(str, notify=preferenceModeChanged)
    def preferenceMode(self) -> str:
        return self._preference_mode

    @Property(bool, notify=isDarkChanged)
    def isDark(self) -> bool:
        return self._mode.endswith("dark") or self._mode == "dark"

    @Property(str, notify=themeChanged)
    def themeId(self) -> str:
        return self._theme_id

    @Property(str, notify=themeChanged)
    def themeName(self) -> str:
        return self._theme_name

    @Property("QVariantMap", notify=assetsChanged)
    def assets(self) -> dict[str, Any]:
        """Resolved package assets; paths are local-file URLs or empty strings."""

        return {
            **self._assets,
            "sounds": dict(self._assets.get("sounds", {})),
        }

    @Slot(object, int, str, str, str, str, object)
    def _replace_snapshot(
        self,
        snapshot: object,
        revision: int,
        mode: str,
        preference_mode: str,
        theme_id: str,
        theme_name: str,
        assets: object,
    ) -> None:
        next_snapshot = dict(snapshot)  # type: ignore[arg-type]
        next_token_values = self._make_token_values(next_snapshot)
        self._snapshot = next_snapshot
        self._token_values = next_token_values
        self._revision = int(revision)
        mode_changed = mode != self._mode
        preference_changed = preference_mode != self._preference_mode
        theme_changed = theme_id != self._theme_id or theme_name != self._theme_name
        assets_changed = assets != self._assets
        was_dark = self.isDark
        self._mode = mode
        self._preference_mode = preference_mode
        self._theme_id = theme_id
        self._theme_name = theme_name
        self._assets = dict(assets)  # type: ignore[arg-type]
        self.snapshotChanged.emit()
        self.revisionChanged.emit()
        if mode_changed:
            self.modeChanged.emit()
        if preference_changed:
            self.preferenceModeChanged.emit()
        if was_dark != self.isDark:
            self.isDarkChanged.emit()
        if theme_changed:
            self.themeChanged.emit()
        if assets_changed:
            self.assetsChanged.emit()


class ThemeService(QObject):
    """Application theme source of truth.

    Create it once through :meth:`instance`. QML only receives engine-scoped
    ``ThemeEngineBridge`` objects; all mutations stay on this Python service.
    """

    snapshotReplaced = Signal(object, int, str, str, str, str, object)
    packagesChanged = Signal()
    PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "config" / "themes"

    _instance: ThemeService | None = None

    def __init__(self, *, _construction_key: object | None = None) -> None:
        if _construction_key is not _INSTANCE_KEY:
            raise RuntimeError("Use ThemeService.instance() for the process-wide service")
        super().__init__()
        self._preference_mode = "system"
        self._mode = "light"
        from coat_sim.ui.settings import DEFAULT_FONT_POINT_SIZE

        self._font_scale = DEFAULT_FONT_POINT_SIZE / 12.0
        self._font_family_override = ""
        self._density_scale = 1.0
        self._theme_id = ""
        self._theme_name = "Built-in"
        self._assets = _empty_assets()
        self._packages: dict[str, ThemePackage] = {}
        self._package_errors: tuple[str, ...] = ()
        self._applying_palette = False
        self._settings_sync: ThemeSettingsSync | None = None
        self.reload_installed_packages(emit=False)
        app = QGuiApplication.instance()
        if app is not None and not isinstance(app, QGuiApplication):
            app = None
        self._application = app
        self._system_palette = app.palette() if app is not None else None
        if self._system_palette is not None:
            self._mode = _mode_from_palette(self._system_palette)
            app.paletteChanged.connect(self._on_application_palette_changed)
        self._revision = 0
        self._snapshot = build_theme_snapshot(
            self._mode, font_scale=self._font_scale
        )
        # Keep each bridge alive while its engine is alive without retaining the
        # engine itself. The bridge is also parented to the engine for teardown.
        self._bridges: dict[
            int,
            tuple[weakref.ReferenceType[QObject], ThemeEngineBridge, str],
        ] = {}

    @classmethod
    def instance(cls) -> ThemeService:
        """Return the process-level service used as the only theme writer."""

        if cls._instance is None:
            cls._instance = cls(_construction_key=_INSTANCE_KEY)
        return cls._instance

    @property
    def current_mode(self) -> str:
        return self._mode

    @property
    def current_preference_mode(self) -> str:
        return self._preference_mode

    @property
    def current_revision(self) -> int:
        return self._revision

    @property
    def current_theme_id(self) -> str:
        return self._theme_id

    @property
    def current_theme_name(self) -> str:
        return self._theme_name

    @property
    def current_assets(self) -> dict[str, Any]:
        return {
            **self._assets,
            "sounds": dict(self._assets.get("sounds", {})),
        }

    @property
    def package_errors(self) -> tuple[str, ...]:
        return self._package_errors

    @property
    def current_snapshot(self) -> dict[str, str | float | int]:
        return dict(self._snapshot)

    def attach_engine(
        self,
        engine: QObject,
        context_property: str = "Theme",
    ) -> ThemeEngineBridge:
        """Install a read-only bridge in one engine's root context.

        Repeated calls with the same engine and property name return its bridge.
        A second property name for that engine is rejected to prevent accidental
        competing theme objects.
        """

        if not context_property or not context_property.isidentifier():
            raise ValueError("context_property must be a non-empty identifier")
        root_context = getattr(engine, "rootContext", None)
        if root_context is None or not callable(root_context):
            raise TypeError("engine must provide rootContext()")

        engine_key = id(engine)
        current = self._bridges.get(engine_key)
        if current is not None and current[0]() is engine:
            bridge, registered_name = current[1], current[2]
            if registered_name != context_property:
                raise ValueError(
                    "this engine already has a theme bridge under "
                    f"{registered_name!r}"
                )
            return bridge

        bridge = ThemeEngineBridge(
            self._snapshot,
            self._revision,
            self._mode,
            self._preference_mode,
            self._theme_id,
            self._theme_name,
            self._assets,
            parent=engine,
        )
        root_context().setContextProperty(context_property, bridge)
        self.snapshotReplaced.connect(bridge._replace_snapshot)

        service_ref = weakref.ref(self)

        def release(_ref: weakref.ReferenceType[QObject]) -> None:
            service = service_ref()
            if service is not None:
                service._bridges.pop(engine_key, None)

        self._bridges[engine_key] = (
            weakref.ref(engine, release),
            bridge,
            context_property,
        )
        return bridge

    def configure(
        self,
        *,
        mode: str | None = None,
        font_scale: float | None = None,
        density_scale: float | None = None,
        theme_package_id: str | None = None,
        font_family: str | None = None,
    ) -> bool:
        """Atomically replace the complete snapshot and notify all engines.

        Return ``True`` if state changed. The service is intended to be updated
        on its owning (normally GUI) thread; Qt then delivers the replacement to
        each engine bridge using its normal signal connection rules.
        """

        next_preference_mode = self._preference_mode if mode is None else mode
        next_font_scale = self._font_scale if font_scale is None else _scale(
            "font_scale", font_scale
        )
        next_density_scale = (
            self._density_scale
            if density_scale is None
            else _scale("density_scale", density_scale)
        )
        next_font_family = (self._font_family_override if font_family is None
                            else str(font_family).strip())
        if len(next_font_family) > 128:
            raise ValueError("font_family must be at most 128 characters")
        if next_preference_mode not in (*THEME_MODES, "system"):
            raise ValueError(f"unsupported theme mode: {next_preference_mode!r}")

        next_theme_id = self._theme_id if theme_package_id is None else theme_package_id
        if not isinstance(next_theme_id, str):
            raise ValueError("theme_package_id must be text")
        if next_theme_id and next_theme_id not in self._packages:
            # A package can be removed outside the application. Fall back safely
            # instead of making startup fail on a stale persisted selection.
            next_theme_id = ""

        self._ensure_application()
        next_mode = (
            _mode_from_palette(self._system_palette)
            if next_preference_mode == "system" and self._system_palette is not None
            else "light" if next_preference_mode == "system"
            else next_preference_mode
        )

        next_snapshot = build_theme_snapshot(
            next_mode,
            font_scale=next_font_scale,
            density_scale=next_density_scale,
        )
        next_theme_name = "Built-in"
        next_assets = _empty_assets()
        if next_theme_id:
            package = self._packages[next_theme_id]
            package_mode = "dark" if next_mode.endswith("dark") else "light"
            mode_package = package.for_mode(package_mode)
            # Manifest brand colors feed existing semantic tokens. Explicit
            # tokenOverrides remain the final authority for a package.
            if not next_mode.startswith("high-contrast-"):
                for brand_key, token_key in (
                    ("primary", "colorAccent"),
                    ("secondary", "colorSurfaceRaised"),
                    ("accent", "colorAccentHover"),
                ):
                    color = mode_package.colors.get(brand_key)
                    if color is not None:
                        next_snapshot[token_key] = color
            next_snapshot.update(mode_package.token_overrides)
            next_theme_name = package.name
            next_assets = _assets_for_mode(package, package_mode)
        if next_font_family:
            next_snapshot["fontFamily"] = next_font_family
        if (
            next_mode == self._mode
            and next_preference_mode == self._preference_mode
            and next_font_scale == self._font_scale
            and next_density_scale == self._density_scale
            and next_font_family == self._font_family_override
            and next_theme_id == self._theme_id
            and next_theme_name == self._theme_name
            and next_assets == self._assets
        ):
            return False

        self._mode = next_mode
        self._preference_mode = next_preference_mode
        self._font_scale = next_font_scale
        self._density_scale = next_density_scale
        self._font_family_override = next_font_family
        self._theme_id = next_theme_id
        self._theme_name = next_theme_name
        self._snapshot = next_snapshot
        self._assets = next_assets
        app = QGuiApplication.instance()
        if isinstance(app, QGuiApplication):
            application_font = QFont(app.font())
            application_font.setFamily(str(next_snapshot["fontFamily"]))
            application_font.setPointSizeF(float(next_snapshot["fontSizeBody"]))
            app.setFont(application_font)
        self._apply_application_palette()
        self._revision += 1
        self.snapshotReplaced.emit(
            dict(next_snapshot),
            self._revision,
            self._mode,
            self._preference_mode,
            self._theme_id,
            self._theme_name,
            dict(self._assets),
        )
        return True

    def available_themes(self) -> list[dict[str, str]]:
        """Return the built-in choice and installed, validated local packages."""

        return [{"id": "", "name": "Built-in"}] + [
            {"id": theme_id, "name": package.name}
            for theme_id, package in sorted(self._packages.items(), key=lambda item: item[1].name.casefold())
        ]

    def has_theme(self, theme_id: str) -> bool:
        return theme_id == "" or theme_id in self._packages

    def _ensure_package_root(self) -> Path:
        root_path = self.PACKAGE_ROOT
        if root_path.is_symlink():
            raise ThemePackageError("theme package install directory must not be a symbolic link")
        root_path.mkdir(parents=True, exist_ok=True)
        parent = root_path.parent.resolve(strict=True)
        root = root_path.resolve(strict=True)
        try:
            root.relative_to(parent)
        except ValueError as exc:
            raise ThemePackageError("theme package install directory escapes its settings directory") from exc
        if not root.is_dir():
            raise ThemePackageError("theme package install root is not a directory")
        return root

    def reload_installed_packages(self, *, emit: bool = True) -> list[dict[str, str]]:
        """Rescan the data-only theme directory, skipping invalid entries."""

        packages: dict[str, ThemePackage] = {}
        errors: list[str] = []
        try:
            root = self._ensure_package_root()
            for directory in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
                if directory.is_symlink() or not directory.is_dir():
                    continue
                try:
                    resolved = directory.resolve(strict=True)
                    resolved.relative_to(root)
                    package = load_theme_package(resolved)
                    if directory.name != package.package_id:
                        raise ThemePackageError("installed directory name does not match package id")
                    packages[package.package_id] = package
                except (OSError, RuntimeError, ValueError) as exc:
                    errors.append(f"{directory.name}: {exc}")
        except (OSError, ThemePackageError) as exc:
            errors.append(f"theme package directory: {exc}")
        self._packages = packages
        self._package_errors = tuple(errors)
        if self._theme_id and self._theme_id not in packages:
            self.configure(theme_package_id="")
        if emit:
            self.packagesChanged.emit()
        return self.available_themes()

    def install_package(self, archive_path: str | Path) -> InstalledThemePackage:
        """Install a validated theme archive into the user-local package root."""

        installed = install_theme_package(archive_path, self._ensure_package_root())
        self.reload_installed_packages()
        if installed.package.package_id not in self._packages:
            raise ThemePackageError("installed package did not pass the package registry scan")
        return installed

    def set_theme(self, mode: str) -> bool:
        """Switch between system, light, dark, and high-contrast palettes."""

        return self.configure(mode=mode)

    def set_accessibility(
        self,
        *,
        font_scale: float | None = None,
        density_scale: float | None = None,
    ) -> bool:
        """Set user-accessible font and layout-density scales."""

        return self.configure(
            font_scale=font_scale,
            density_scale=density_scale,
        )

    def _ensure_application(self) -> None:
        app = QGuiApplication.instance()
        if app is not None and not isinstance(app, QGuiApplication):
            app = None
        if app is not None and app is not self._application:
            self._application = app
            self._system_palette = app.palette()
            app.paletteChanged.connect(self._on_application_palette_changed)

    def _on_application_palette_changed(self, palette: QPalette) -> None:
        if self._applying_palette:
            return
        if self._preference_mode != "system":
            return
        self._system_palette = QPalette(palette)
        changed = self.configure(mode="system")
        if self._theme_id and not changed:
            self._apply_application_palette()

    def _apply_application_palette(self) -> None:
        app = self._application
        if app is None:
            return
        if self._preference_mode == "system" and not self._theme_id:
            if self._system_palette is not None:
                self._set_application_palette(self._system_palette)
            return
        self._set_application_palette(_palette_from_snapshot(self._snapshot))

    def _set_application_palette(self, palette: QPalette) -> None:
        app = self._application
        if app is None:
            return
        self._applying_palette = True
        try:
            app.setPalette(palette)
        finally:
            self._applying_palette = False


def _scale(name: str, value: Any) -> float:
    minimum, maximum = ((2 / 3), (8 / 3)) if name == "font_scale" else (0.75, 2.0)
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number from {minimum:g} to {maximum:g}") from exc
    if not math.isfinite(numeric) or not minimum <= numeric <= maximum:
        raise ValueError(f"{name} must be a finite number from {minimum:g} to {maximum:g}")
    return numeric


def configure_theme_from_settings(settings: dict[str, object] | None = None) -> ThemeService:
    """Load the shared UI preference into the process theme service."""

    from coat_sim.ui.settings import DEFAULT_FONT_POINT_SIZE, load_ui_settings

    subscribe = settings is None
    if settings is None:
        settings = load_ui_settings()
    configured_point_size = settings.get(
        "font_point_size", DEFAULT_FONT_POINT_SIZE
    )
    if not isinstance(configured_point_size, (str, int, float)):
        configured_point_size = DEFAULT_FONT_POINT_SIZE
    try:
        point_size = float(configured_point_size)
    except (TypeError, ValueError):
        point_size = float(DEFAULT_FONT_POINT_SIZE)
    service = ThemeService.instance()
    service.configure(
        mode=str(settings.get("theme_mode", "system")),
        font_scale=point_size / 12.0,
        density_scale=float(settings.get("density_scale_percent", 100)) / 100.0,
        theme_package_id=str(settings.get("theme_package_id", "")),
        font_family=str(settings.get("font_family", "")),
    )
    if subscribe:
        from .settings import SETTINGS_PATH
        from .theme_settings_sync import ThemeSettingsSync
        sync = getattr(service, "_settings_sync", None)
        if sync is None or sync.path != SETTINGS_PATH.absolute():
            if sync is not None:
                sync.stop()
                sync.deleteLater()
            def apply_saved_theme(values):
                from .settings import apply_font_settings
                package_id = str(values.get("theme_package_id", ""))
                if package_id and not service.has_theme(package_id):
                    service.reload_installed_packages(emit=False)
                configure_theme_from_settings(values)
                apply_font_settings(values)
            sync = ThemeSettingsSync(SETTINGS_PATH, apply_saved_theme, settings, parent=service)
            service._settings_sync = sync
            app = QGuiApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(sync.stop)
    return service


def _empty_assets() -> dict[str, Any]:
    return {
        "backgroundUrl": "",
        "backgroundMode": "cover",
        "applicationIconUrl": "",
        "floatingIconUrl": "",
        "floatingIconAccessibleName": "",
        "sounds": {},
        "primaryColor": "",
        "secondaryColor": "",
        "accentColor": "",
    }


def _local_file_url(path: Path | None) -> str:
    return QUrl.fromLocalFile(str(path)).toString() if path is not None else ""


def _assets_for_mode(package: ThemePackage, mode: str) -> dict[str, Any]:
    selected = package.for_mode(mode)
    background = selected.background
    mascot = selected.mascot
    return {
        "backgroundUrl": _local_file_url(background.image) if background else "",
        "backgroundMode": background.mode if background else "cover",
        "applicationIconUrl": _local_file_url(selected.taskbar_icon),
        "floatingIconUrl": _local_file_url(mascot.floating_icon) if mascot else "",
        "floatingIconAccessibleName": mascot.accessible_name if mascot else "",
        "sounds": {
            event: _local_file_url(path)
            for event, path in package.sounds.items()
            if path is not None
        },
        "primaryColor": selected.colors.get("primary", ""),
        "secondaryColor": selected.colors.get("secondary", ""),
        "accentColor": selected.colors.get("accent", ""),
    }


def _mode_from_palette(palette: QPalette) -> str:
    color = palette.color(QPalette.ColorGroup.Active, QPalette.ColorRole.Window)
    return "dark" if color.lightnessF() < 0.5 else "light"


def _palette_from_snapshot(snapshot: dict[str, str | float | int]) -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: "colorWindow",
        QPalette.ColorRole.WindowText: "colorText",
        QPalette.ColorRole.Base: "colorSurface",
        QPalette.ColorRole.AlternateBase: "colorSurfaceRaised",
        QPalette.ColorRole.Text: "colorText",
        QPalette.ColorRole.Button: "colorSurfaceRaised",
        QPalette.ColorRole.ButtonText: "colorText",
        QPalette.ColorRole.ToolTipBase: "colorSurfaceRaised",
        QPalette.ColorRole.ToolTipText: "colorText",
        QPalette.ColorRole.PlaceholderText: "colorTextMuted",
        QPalette.ColorRole.Highlight: "colorSelection",
        QPalette.ColorRole.HighlightedText: "colorText",
        QPalette.ColorRole.Link: "colorAccent",
        QPalette.ColorRole.LinkVisited: "colorAccentPressed",
        QPalette.ColorRole.Mid: "colorBorder",
        QPalette.ColorRole.Midlight: "colorSurfaceRaised",
        QPalette.ColorRole.Dark: "colorBorderStrong",
        QPalette.ColorRole.Light: "colorSurface",
        QPalette.ColorRole.BrightText: "colorError",
    }
    accent_role = getattr(QPalette.ColorRole, "Accent", None)
    if accent_role is not None:
        roles[accent_role] = "colorAccent"
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        for role, token in roles.items():
            palette.setColor(group, role, QColor(str(snapshot[token])))
    for role, token in roles.items():
        disabled_token = "colorDisabledText" if role in {
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
            QPalette.ColorRole.ToolTipText,
            QPalette.ColorRole.PlaceholderText,
            QPalette.ColorRole.HighlightedText,
            QPalette.ColorRole.Link,
            QPalette.ColorRole.LinkVisited,
            QPalette.ColorRole.BrightText,
        } else "colorDisabledSurface"
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(str(snapshot[disabled_token])))
    return palette
