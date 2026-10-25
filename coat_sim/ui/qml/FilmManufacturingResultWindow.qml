pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "."
import "theme_controls" as ThemeControls

AppWindow {
    id: window
    required property var controller
    titleBase: "工艺薄层修复 · 只读历史报告"
    width: 1100
    height: 760
    minimumWidth: 400
    minimumHeight: 320
    initialMaximized: false
    statusText: controller.statusText
    statusSeverity: controller.error.length ? "error" : "info"
    color: window.themeColors.colorWindow
    ScrollView {
        id: viewport
        anchors.fill: parent
        anchors.margins: window.themeColors.spaceLg
        anchors.topMargin: window.contentTopMargin + window.themeColors.spaceLg
        contentWidth: availableWidth
        clip: true
        ColumnLayout {
            width: viewport.availableWidth
            spacing: window.themeColors.spaceMd
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                text: "只读冻结历史报告，未绑定当前来源设计，不能应用。原始光学目标没有放宽；没有完整独立审计的候选为未知/未通过。"
                wrapMode: Text.Wrap
            }
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                visible: text.length > 0
                text: window.controller.error
                wrapMode: Text.Wrap
            }
            FilmManufacturingComparison {
                Layout.fillWidth: true
                controller: window.controller
                themeTokens: window.themeColors
            }
            ThemeControls.ThemeButton {
                text: "保存完整冻结请求与报告…"
                enabled: window.controller.hasResult
                onClicked: reportFile.open()
            }
        }
    }
    FileDialog {
        id: reportFile
        title: "另存完整工艺修复报告"
        fileMode: FileDialog.SaveFile
        nameFilters: ["JSON report (*.json)"]
        defaultSuffix: "json"
        onAccepted: window.controller.saveReportUrl(selectedFile, false)
    }
    Connections {
        target: window.controller
        function onOverwriteRequested(path) { overwriteDialog.targetPath = path; overwriteDialog.open() }
    }
    ThemeControls.ThemeDialogBase {
        id: overwriteDialog
        property string targetPath: ""
        title: "替换已有报告？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: ThemeControls.ThemeLabel {
            text: overwriteDialog.targetPath
            wrapMode: Text.Wrap
        }
        onAccepted: window.controller.saveReport(targetPath, true)
    }
}
