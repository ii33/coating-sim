"""Static contract checks for the opt-in Qt Quick theme controls."""

from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROLS = ROOT / "coat_sim" / "ui" / "qml" / "theme_controls"
TOKEN_SOURCE = ROOT / "coat_sim" / "ui" / "theme_tokens.py"
DOC = ROOT / "docs" / "quick-theme-controls.md"
EXPECTED_COMPONENTS = {
    "ThemeButton.qml",
    "InputFocusBehavior.qml",
    "ThemeDoubleChevron.qml",
    "ThemeComboBox.qml",
    "ThemeField.qml",
    "ThemeLabel.qml",
    "ThemeSpinBox.qml",
    "ThemeSection.qml",
    "ThemeDialogBase.qml",
    "ThemeSurface.qml",
    "ThemeSidebar.qml",
    "ThemePopupHeader.qml",
    "ThemeMaterialPicker.qml",
}


def _token_names() -> set[str]:
    tree = ast.parse(TOKEN_SOURCE.read_text(encoding="utf-8"))
    assignments = {
        target.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    def read_tuple(name: str) -> list[str]:
        value = assignments[name]
        assert isinstance(value, ast.Tuple)
        result: list[str] = []
        for element in value.elts:
            if isinstance(element, ast.Starred) and isinstance(element.value, ast.Name):
                result.extend(read_tuple(element.value.id))
            elif isinstance(element, ast.Constant) and isinstance(element.value, str):
                result.append(element.value)
            else:
                raise AssertionError(f"unexpected token declaration node: {element!r}")
        return result

    return set(read_tuple("THEME_TOKEN_KEYS"))


def test_theme_controls_directory_contains_only_the_small_shared_set():
    assert {path.name for path in CONTROLS.glob("*.qml")} == EXPECTED_COMPONENTS


def test_qml_theme_references_match_the_existing_token_contract():
    known_tokens = _token_names()
    used_tokens: set[str] = set()
    for path in CONTROLS.glob("*.qml"):
        used_tokens.update(re.findall(
            r"(?:Theme\.snapshot|\bthemeTokens|\btokens)\.(\w+)",
            path.read_text(encoding="utf-8")))
    assert used_tokens
    assert used_tokens <= known_tokens


def test_controls_keep_native_keyboard_focus_accessibility_and_state_styling():
    button = (CONTROLS / "ThemeButton.qml").read_text(encoding="utf-8")
    combo = (CONTROLS / "ThemeComboBox.qml").read_text(encoding="utf-8")
    field = (CONTROLS / "ThemeField.qml").read_text(encoding="utf-8")
    material_picker = (CONTROLS / "ThemeMaterialPicker.qml").read_text(encoding="utf-8")
    section = (CONTROLS / "ThemeSection.qml").read_text(encoding="utf-8")

    assert "Button {" in button and "focusPolicy: Qt.StrongFocus" in button
    assert "Accessible.name: control.text" in button
    assert "control.activeFocus" in button and "control.hovered" in button
    assert "control.flat" in button and "control.checked" in button
    assert "ComboBox {" in combo and "focusPolicy: Qt.StrongFocus" in combo
    assert "control.delegateModel" in combo and "control.textAt(index)" in combo
    assert "control.activeFocus" in combo and "control.hovered" in combo
    assert "Accessible.description" in combo
    assert "TextField {" in field and "focusPolicy: Qt.StrongFocus" in field
    assert "Accessible.name:" in field and "selectByMouse: true" in field
    assert "field.activeFocus" in field and "field.hovered" in field
    assert "selectionColor: field.themeTokens.colorSelection" in field
    assert "selectedTextColor: field.themeTokens.colorText" in field
    assert "cursorDelegate: Rectangle" in field
    assert "color: field.themeTokens.colorText" in field
    assert "cursorDelegate: Rectangle" in material_picker
    assert "color: control.themeTokens.colorText" in material_picker
    assert "onClicked: section.expanded = !section.expanded" in section
    assert "Accessible.description:" in section


def test_dialog_accessibility_is_attached_to_its_visual_content_item():
    dialog = (CONTROLS / "ThemeDialogBase.qml").read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^    contentItem: Item \{\r?\n(?P<body>.*?)^    \}", dialog
    )
    assert match is not None
    assert "Accessible.role: Accessible.Dialog" in match.group("body")
    assert "Accessible.name: dialog.title" in match.group("body")
    assert not re.search(r"^    Accessible\.(?:role|name):", dialog, re.MULTILINE)


def test_quick_control_docs_explain_import_tokens_and_dpi_behavior():
    text = DOC.read_text(encoding="utf-8")
    assert 'import "theme_controls" as ThemeControls' in text
    assert "Theme.snapshot" in text
    assert "Screen.devicePixelRatio" in text
    assert "ThemeComboBox" in text
    assert "ThemeSpinBox" in text
    assert "图表" in text


def test_shared_components_cover_text_numeric_input_and_panel_surfaces():
    label = (CONTROLS / "ThemeLabel.qml").read_text(encoding="utf-8")
    spin = (CONTROLS / "ThemeSpinBox.qml").read_text(encoding="utf-8")
    surface = (CONTROLS / "ThemeSurface.qml").read_text(encoding="utf-8")
    app_window = (ROOT / "coat_sim" / "ui" / "qml" / "AppWindow.qml").read_text(
        encoding="utf-8"
    )

    assert "property bool muted" in label
    assert re.search(r"(?:Theme\.snapshot|themeTokens)\.colorTextMuted", label)
    assert "SpinBox {" in spin and "control.valueFromText" in spin
    assert "control.increase()" in spin and "control.decrease()" in spin
    assert "default property alias contentData" in surface
    assert "palette.buttonText:" in app_window
    assert "palette.placeholderText:" in app_window
    assert "palette.disabled.buttonText:" in app_window
    assert "palette.highlightedText:" in app_window


def test_sidebar_keeps_caller_owned_content_and_vertical_scroll():
    sidebar = (CONTROLS / "ThemeSidebar.qml").read_text(encoding="utf-8")
    assert "default property alias body: contentColumn.data" in sidebar
    assert 'property string title: ""' in sidebar
    assert "property real sidebarWidth: 320" in sidebar
    assert "ScrollView {" in sidebar
    assert "ScrollBar.horizontal.policy: ScrollBar.AlwaysOff" in sidebar
    assert "width: scroll.availableWidth" in sidebar
    assert "Loader {" not in sidebar

