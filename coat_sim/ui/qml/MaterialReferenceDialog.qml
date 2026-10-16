import QtQuick
import QtQuick.Controls

ThemePageDialog {
    id: dialog
    property var sections: []
    title: "材料参考详情"
    modal: true
    standardButtons: Dialog.Close
    width: Math.min(880, parent ? parent.width - 32 : 880)
    height: Math.min(720, parent ? parent.height - 32 : 720)
    anchors.centerIn: parent
    contentItem: ReferenceSections { sections: dialog.visible ? dialog.sections : [] }
}
