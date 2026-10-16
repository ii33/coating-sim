import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Dialog {
    id: dialog
    objectName: "shellConfirmDialog"
    property string message: ""
    property string confirmTitle: "退出确认"
    property string acceptText: "退出"
    property string acceptButtonObjectName: "shellConfirmAccept"
    property string cancelButtonObjectName: "shellConfirmCancel"
    property string secondaryText: ""
    property string secondaryButtonObjectName: "shellConfirmSecondary"
    signal secondaryRequested()
    readonly property var themeColors: typeof Theme !== "undefined" ? Theme.snapshot : null

    title: dialog.confirmTitle
    modal: true
    focus: true
    dim: true
    closePolicy: Popup.CloseOnEscape
    width: 440
    padding: 18

    background: Rectangle {
        radius: 12
        color: dialog.themeColors ? dialog.themeColors.colorSurface : "#ffffff"
        border.color: dialog.themeColors ? dialog.themeColors.colorBorderStrong : "#81939c"
        border.width: 1
    }

    contentItem: ColumnLayout {
        spacing: 18

        Label {
            Layout.fillWidth: true
            text: dialog.message
            wrapMode: Text.WordWrap
            color: dialog.themeColors ? dialog.themeColors.colorText : "#1d2830"
            font.pointSize: dialog.themeColors ? dialog.themeColors.fontSizeBody : 9
            Accessible.name: text
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            Item { Layout.fillWidth: true }

            Button {
                objectName: dialog.secondaryButtonObjectName
                text: dialog.secondaryText
                visible: dialog.secondaryText.length > 0
                onClicked: {
                    dialog.close()
                    dialog.secondaryRequested()
                }
            }

            Button {
                id: cancelButton
                objectName: dialog.cancelButtonObjectName
                text: "取消"
                onClicked: dialog.reject()
            }

            Button {
                objectName: dialog.acceptButtonObjectName
                text: dialog.acceptText
                highlighted: true
                onClicked: dialog.accept()
            }
        }
    }

    onOpened: Qt.callLater(function() { cancelButton.forceActiveFocus() })
}
