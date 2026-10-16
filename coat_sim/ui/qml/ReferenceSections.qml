import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ListView {
    id: root
    property var sections: []
    implicitHeight: 460
    clip: true
    spacing: 10
    cacheBuffer: 180
    model: sections
    ScrollBar.vertical: ScrollBar { }
    delegate: ThemeControls.ThemeSurface {
        required property var modelData
        width: ListView.view.width
        padding: 10
        contentSpacing: 0
        Column {
            Layout.fillWidth: true
            width: parent.width
            spacing: 5
            ThemeControls.ThemeLabel { width: parent.width; text: modelData.title; textFormat: Text.PlainText; font.bold: true; wrapMode: Text.Wrap; color: modelData.warning ? (typeof Theme !== "undefined" ? Theme.snapshot.colorWarning : "#b07730") : Theme.snapshot.colorText }
            TextEdit {
                width: parent.width
                text: modelData.html ? modelData.html : modelData.body
                readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; color: palette.text
                textFormat: modelData.html ? TextEdit.RichText : TextEdit.PlainText
                onLinkActivated: link => { if (/^https?:\/\//i.test(link)) Qt.openUrlExternally(link) }
            }
            ThemeControls.ThemeLabel { visible: modelData.source.length > 0 && modelData.url.length === 0; width: parent.width; text: "依据：" + modelData.source; textFormat: Text.PlainText; wrapMode: Text.Wrap; opacity: 0.8 }
            ThemeControls.ThemeButton { visible: /^https?:\/\//i.test(modelData.url); text: modelData.source || "打开原始来源"; width: Math.min(implicitWidth, parent.width); onClicked: Qt.openUrlExternally(modelData.url) }
        }
    }
}
