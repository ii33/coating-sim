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
    titleBase: "膜厚敏感度 · 只读历史报告"
    width: 840
    height: 620
    minimumWidth: 620
    minimumHeight: 420
    initialMaximized: false
    readonly property bool unsupportedWorkflow: controller.activeWorkflow !== "table"
                                                  && controller.activeWorkflow !== "ideal"
    statusText: unsupportedWorkflow ? "仅支持当前敏感度报告格式"
                : controller.activeWorkflow === "table" ? controller.tableStatus
                : controller.idealStatus
    statusSeverity: unsupportedWorkflow ? "warning"
                     : (controller.activeWorkflow === "table" ? controller.tableError.length
                        : controller.idealError.length) ? "error" : "info"
    color: window.themeColors.colorWindow
    ScrollView {
        anchors.fill: parent
        anchors.margins: window.themeColors.spaceLg
        anchors.topMargin: window.contentTopMargin + window.themeColors.spaceLg
        contentWidth: availableWidth
        clip: true
        ColumnLayout {
            width: parent.width
            Label {
                objectName: "filmSensitivityUnsupportedHistoryNotice"
                Layout.fillWidth: true
                visible: window.unsupportedWorkflow
                text: "此敏感度历史格式不受支持。内容保持只读，不会转换为其他类型、重算或应用。"
                color: window.themeColors.colorTextMuted
                wrapMode: Text.WordWrap
            }
            FilmSensitivityWorkflowResultPanel {
                Layout.fillWidth: true
                visible: window.controller.activeWorkflow === "table"
                controller: window.controller
                themeTokens: window.themeColors
                workflowKind: "table"
            }
            FilmSensitivityWorkflowResultPanel {
                Layout.fillWidth: true
                visible: window.controller.activeWorkflow === "ideal"
                controller: window.controller
                themeTokens: window.themeColors
                workflowKind: "ideal"
            }
            ThemeControls.ThemeButton {
                visible: !window.unsupportedWorkflow
                text: "保存完整冻结请求与报告…"
                onClicked: reportFile.open()
            }
        }
    }
    FileDialog {
        id: reportFile
        title: "另存完整敏感性报告"
        fileMode: FileDialog.SaveFile
        nameFilters: ["JSON report (*.json)"]
        defaultSuffix: "json"
        onAccepted: window.controller.saveReportUrl(selectedFile, false)
    }
    Connections {
        target: window.controller
        function onOverwriteRequested(path) {
            overwriteDialog.targetPath = path
            overwriteDialog.open()
        }
    }
    ThemeControls.ThemeDialogBase {
        id: overwriteDialog
        property string targetPath: ""
        title: "替换已有报告？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Label {
            text: overwriteDialog.targetPath
            color: window.themeColors.colorText
            wrapMode: Text.WordWrap
        }
        onAccepted: window.controller.saveReport(targetPath, true)
    }
}
