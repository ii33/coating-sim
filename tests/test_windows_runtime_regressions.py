"""Cross-platform frozen bytes and film-generator source-value regressions."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QLocale, QObject, Property, Qt, QUrl
from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlExpression
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from coat_sim.appearance.gpu import _write_input_job, json_hash
from coat_sim.core.process_profiles import source_selection_options
from coat_sim.ui.film_stack import FilmStackController
from coat_sim.ui.theme_tokens import build_theme_snapshot
from tests.qt_test_guard import qml_item_window
from tests.test_input_snapshot import fixture as process_fixture


QML_ROOT = Path(__file__).resolve().parents[1] / "coat_sim/ui/qml"


def test_gpu_input_job_bytes_are_exact_even_with_windows_text_translation(tmp_path, monkeypatch):
    path = tmp_path / "input-job.json"
    job = {"材料": "ZnS", "layers": [{"d_nm": 123.456789, "label": "row\nnext"}],
           "metadata": {"z": None, "a": True}}
    before = deepcopy(job)
    original_write_text = Path.write_text
    text_writes = []

    def windows_write_text(self, data, *args, **kwargs):
        text_writes.append(self)
        # Reproduce Windows newline translation on Linux too. An accidental
        # switch back to text output must fail this regression on every host.
        return original_write_text(self, data.replace("\n", "\r\n"), *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", windows_write_text)
    _write_input_job(path, job)
    expected = (json.dumps(job, sort_keys=True, separators=(",", ":"),
                           allow_nan=False) + "\n").encode("utf-8")
    assert path.read_bytes() == expected
    assert path.stat().st_size == len(expected)
    assert expected.endswith(b"\n") and b"\r" not in expected
    assert not text_writes
    assert json.loads(expected) == before == job
    assert json_hash(json.loads(expected)) == json_hash(job)


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf])
def test_gpu_input_job_rejects_nonfinite_values_without_overwriting(tmp_path, invalid):
    path = tmp_path / "input-job.json"
    path.write_bytes(b"existing frozen input\n")
    with pytest.raises(ValueError):
        _write_input_job(path, {"field_nm": invalid})
    assert path.read_bytes() == b"existing frozen input\n"


class _Editor(QObject):
    """Only the editor contract needed by the real FilmStackController."""
    def __init__(self, service, data, path):
        super().__init__()
        self.service, self._data, self._path = service, deepcopy(data), path
        self._step_page = 0
        self.edit_count = 0
        self.status = ""
        runtime = service.load("process", path).runtime
        self._source_preview = {"sources": source_selection_options(runtime["machine"])}
        self._film_stack = FilmStackController(self)

    @Property(dict, constant=True)
    def sourcePreview(self):
        return self._source_preview

    @Property(QObject, constant=True)
    def filmStack(self):
        return self._film_stack

    def _edit(self):
        self.edit_count += 1

    def _set_status(self, value):
        self.status = value


@pytest.fixture
def film_dialog(tmp_path):
    app = QApplication.instance() or QApplication([])
    service, data = process_fixture(tmp_path, mask_count=0)
    editor = _Editor(service, data, tmp_path / "recipe.cproc")
    engine = QQmlEngine()
    engine.rootContext().setContextProperty("Theme", {"snapshot": build_theme_snapshot("light")})
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(QML_ROOT / "FilmStackDialog.qml")))
    assert component.isReady(), [error.toString() for error in component.errors()]
    window = QQuickWindow()
    window.resize(1100, 800)
    dialog = component.createWithInitialProperties({"editor": editor, "parent": window.contentItem()})
    assert dialog is not None, [error.toString() for error in component.errors()]

    def evaluate(source):
        expression = QQmlExpression(QQmlEngine.contextForObject(dialog), dialog, source)
        value, _undefined = expression.evaluate()
        assert not expression.hasError(), expression.error().toString()
        return value.toVariant() if hasattr(value, "toVariant") else value

    previous_locale = QLocale()
    QLocale.setDefault(QLocale("en_US"))
    window.show()
    evaluate("tabs.currentIndex = 1; open()")
    QTest.qWait(30)
    try:
        yield SimpleNamespace(editor=editor, dialog=dialog, window=window, evaluate=evaluate)
    finally:
        evaluate("close()")
        editor._film_stack.close()
        window.close()
        dialog.deleteLater()
        engine.deleteLater()
        window.deleteLater()
        editor.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        QLocale.setDefault(previous_locale)


def _source(session, field_name, value):
    session.evaluate(f"{field_name}.setNumericSourceValue({json.dumps(value)})")
    QTest.qWait(20)
    return session.evaluate(field_name)


def _blur(session, field):
    session.evaluate("tabs.forceActiveFocus(Qt.OtherFocusReason)")
    QTest.qWait(20)
    assert not field.property("activeFocus")


def _edit(session, field, text):
    field.forceActiveFocus(Qt.FocusReason.OtherFocusReason)
    QTest.qWait(20)
    surface = qml_item_window(field)
    assert surface is not None
    QTest.keyClick(surface, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(surface, Qt.Key.Key_Backspace)
    special = {".": Qt.Key.Key_Period, "-": Qt.Key.Key_Minus}
    for character in text:
        key = special.get(character) or getattr(Qt.Key, "Key_" + character.upper())
        QTest.keyClick(surface, key)
    QTest.qWait(20)
    assert field.property("text") == text
    assert field.property("numericDisplayValue") == text


@pytest.mark.parametrize(("locale", "display"), [("en_US", "7,000"), ("de_DE", "7.000")])
def test_equal_stack_defaults_use_source_in_grouped_locales(film_dialog, locale, display):
    session = film_dialog
    QLocale.setDefault(QLocale(locale))
    field = session.evaluate("totalThickness")
    assert field.property("numericDisplayValue") == "7000"
    session.evaluate("totalThickness.syncNumericDisplay()")
    assert field.property("text") == display
    payload = session.evaluate("buildParameters()")
    assert payload["total_thickness_nm"] == 7000
    assert session.editor.filmStack.build(payload, False), session.editor.filmStack.state
    steps = session.editor._data["steps"]
    assert len(steps) == 60
    assert sum(step["target_nm"] for step in steps) == pytest.approx(7000)


@pytest.mark.parametrize(("field_name", "method", "parameter", "source"), [
    ("totalThickness", 0, "total_thickness_nm", "7123.456789"),
    ("wavelength", 2, "reference_wavelength_nm", "1234.567891"),
])
def test_film_generator_focus_edit_and_blur_preserve_unrounded_source(
        film_dialog, field_name, method, parameter, source):
    session = film_dialog
    session.evaluate(f"method.currentIndex = {method}")
    session.evaluate('for (let i = 0; i < generatorMappings.count; ++i) '
                     'generatorMappings.setProperty(i, "indexValue", "2")')
    field = _source(session, field_name, source)
    assert field.property("text") != source
    for _ in range(2):
        field.forceActiveFocus(Qt.FocusReason.OtherFocusReason)
        QTest.qWait(20)
        assert field.property("text") == source
        _blur(session, field)
        assert field.property("numericDisplayValue") == source
        assert session.evaluate("buildParameters()")[parameter] == float(source)

    edited = "2345.678912"
    _edit(session, field, edited)
    assert session.evaluate("buildParameters()")[parameter] == float(edited)
    _blur(session, field)
    assert field.property("text") == "2,345.679"
    assert field.property("numericDisplayValue") == edited
    payload = session.evaluate("buildParameters()")
    assert payload[parameter] == float(edited)
    assert session.editor.filmStack.build(payload, False), session.editor.filmStack.state
    targets = [row["target_nm"] for row in session.editor._data["steps"]]
    expected = float(edited) / (60 if method == 0 else 8)
    assert targets == pytest.approx([expected] * 60)


@pytest.mark.parametrize("invalid", ["", "0", "-1", "bad", "1e999"])
@pytest.mark.parametrize(("field_name", "method"), [("totalThickness", 0), ("wavelength", 2)])
def test_film_generator_invalid_edits_are_rejected_without_mutating_draft(
        film_dialog, field_name, method, invalid):
    session = film_dialog
    session.evaluate(f"method.currentIndex = {method}")
    session.evaluate('for (let i = 0; i < generatorMappings.count; ++i) '
                     'generatorMappings.setProperty(i, "indexValue", "2")')
    field = session.evaluate(field_name)
    before = deepcopy(session.editor._data)
    _edit(session, field, invalid)
    _blur(session, field)
    payload = session.evaluate("buildParameters()")
    assert not session.editor.filmStack.build(payload, False)
    assert session.editor.filmStack.state["message"].startswith("无法生成层表：")
    assert session.editor._data == before
    assert session.editor.edit_count == 0
