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
    titleBase: "膜厚敏感性 · 只读历史报告"
    width: 840
    height: 620
    minimumWidth: 620
    minimumHeight: 420
    initialMaximized: false
    statusText: controller.status
    statusSeverity: controller.error.length ? "error" : "info"
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
                Layout.fillWidth: true
                text: "作业 " + window.controller.jobId + "\n" + window.controller.resultSummary
                color: window.themeColors.colorText
                wrapMode: Text.WordWrap
            }
            Repeater {
                model: window.controller.scenarioResults
                delegate: Label {
                    required property var modelData
                    Layout.fillWidth: true
                    text: modelData.scenario_id + " · 有限场景最大违规 "
                          + Number(modelData.max_error_percent).toPrecision(7) + " 个百分点 · "
                          + (modelData.targets_met ? "原门限采样通过" : "原门限未通过")
                          + " · 归一化违规 " + Number(modelData.max_normalized_error).toPrecision(7)
                          + (modelData.solver_acceptable ? " · 求解容差可接受" : " · 求解容差不可接受")
                    color: window.themeColors.colorText
                    wrapMode: Text.WordWrap
                }
            }
            ThemeControls.ThemeButton {
                text: "保存完整冻结请求与报告…"
                onClicked: reportFile.open()
            }
            Label {
                Layout.fillWidth: true
                text: window.controller.error
                color: window.themeColors.colorError
                wrapMode: Text.WordWrap
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
