import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ThemeControls.ThemeDialogBase {
    id: dialog
    objectName: "filmPlotSettingsDialog"
    property var controller
    property string localError: ""
    title: "绘图参数"
    modal: true
    standardButtons: Dialog.NoButton
    width: Math.min(700, Overlay.overlay ? Overlay.overlay.width - dialog.themeTokens.spaceXl * 2 : 700)
    height: Overlay.overlay
            ? Math.max(180, Math.min(640,
                 Overlay.overlay.height - dialog.themeTokens.spaceXl * 2))
            : 640
    anchors.centerIn: Overlay.overlay
    onOpened: { localError = ""; settingsForm.loadFromController() }
    onRejected: { localError = ""; settingsForm.loadFromController() }

    function applySettings() {
        var accepted = controller.setPlotSettings(settingsForm.settingsPayload())
        localError = accepted ? "" : controller.validationError
        settingsForm.localError = localError
        return accepted
    }

    contentItem: ColumnLayout {
        Accessible.role: Accessible.Dialog
        Accessible.name: dialog.title
        spacing: dialog.themeTokens.spaceMd
        ScrollView {
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth
            FilmPlotSettingsForm {
                id: settingsForm
                width: parent.width
                controller: dialog.controller
                themeTokens: dialog.themeTokens
            }
        }
        Label {
            Layout.fillWidth: true
            visible: dialog.localError.length > 0
            text: dialog.localError
            color: dialog.themeTokens.colorError
            wrapMode: Text.WordWrap
        }
        RowLayout {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignRight
            spacing: dialog.themeTokens.spaceSm
            ThemeControls.ThemeButton {
                objectName: "filmPlotSettingsCancelButton"
                text: "取消"
                Accessible.name: "取消绘图参数"
                onClicked: dialog.reject()
            }
            ThemeControls.ThemeButton {
                objectName: "filmPlotSettingsApplyButton"
                text: "应用"
                primary: true
                onClicked: if (dialog.applySettings()) dialog.close()
            }
        }
    }
}
