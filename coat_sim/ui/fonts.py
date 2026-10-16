"""Bundled, offline CJK fonts shared by every Qt entry point."""

from pathlib import Path

from PySide6.QtGui import QFontDatabase, QGuiApplication


FONT_ROOT = Path(__file__).resolve().parent / "assets" / "fonts" / "noto-cjk"
_registered_application = None
_family = ""


def bundled_font_family() -> str:
    """Register the unmodified OFL fonts once per application, without installation."""
    global _registered_application, _family
    application = QGuiApplication.instance()
    if application is None:
        return ""
    if application is _registered_application:
        return _family
    _registered_application = application
    _family = ""
    for filename in ("NotoSansCJKsc-Regular.otf", "NotoSansCJKsc-Bold.otf"):
        path = FONT_ROOT / filename
        if not path.is_file():
            continue
        identifier = QFontDatabase.addApplicationFont(str(path))
        if identifier >= 0:
            families = QFontDatabase.applicationFontFamilies(identifier)
            if families and not _family:
                _family = families[0]
    return _family
