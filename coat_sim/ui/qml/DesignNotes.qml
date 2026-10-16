import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls
ColumnLayout {
    id: root
    required property var editor
    property bool expanded: false
    Layout.fillWidth: true
    spacing: 6
    ToolButton {
        visible: !root.expanded
        text: details.visible ? "描述与备注 ▾" : "描述与备注 ▸"
        onClicked: details.visible = !details.visible
    }
    ColumnLayout {
        id: details
        visible: root.expanded
        Layout.fillWidth: true
        spacing: 6
        ThemeControls.ThemeField {
            Layout.fillWidth: true
            placeholderText: "描述"
            text: root.editor.description
            maximumLength: 20000
            selectByMouse: true
            onTextEdited: root.editor.setMetadata("description", text)
        }
        ScrollView {
            Layout.fillWidth: true
            Layout.preferredHeight: 100
            TextArea {
                placeholderText: "备注"
                text: root.editor.notes
                wrapMode: Text.Wrap
                selectByMouse: true
                onTextChanged: if (activeFocus) root.editor.setMetadata("notes", text)
            }
        }
    }
}
