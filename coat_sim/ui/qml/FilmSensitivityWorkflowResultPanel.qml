pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: resultPanel
    required property var controller
    required property var themeTokens
    required property string workflowKind
    readonly property bool hasResult: workflowKind === "table"
        ? controller.tableHasResult : controller.idealHasResult
    readonly property string summary: workflowKind === "table"
        ? controller.tableResultSummary : controller.idealResultSummary
    readonly property string status: workflowKind === "table"
        ? controller.tableStatus : controller.idealStatus
    readonly property string jobId: workflowKind === "table"
        ? controller.tableJobId : controller.idealJobId
    readonly property string error: workflowKind === "table"
        ? controller.tableError : controller.idealError

    Label {
        objectName: "filmSensitivityWorkflowResultSummary"
        Layout.fillWidth: true
        visible: resultPanel.hasResult
        text: resultPanel.summary
        color: resultPanel.themeTokens.colorText
        wrapMode: Text.WordWrap
        Accessible.name: text
    }
    Label {
        objectName: "filmSensitivityWorkflowStatus"
        Layout.fillWidth: true
        text: resultPanel.status + (resultPanel.jobId.length ? "\n作业 " + resultPanel.jobId : "")
        color: resultPanel.themeTokens.colorTextMuted
        wrapMode: Text.WordWrap
    }
    Label {
        objectName: "filmSensitivityWorkflowError"
        Layout.fillWidth: true
        visible: resultPanel.error.length > 0
        text: resultPanel.error
        color: resultPanel.themeTokens.colorError
        wrapMode: Text.WordWrap
    }
}
