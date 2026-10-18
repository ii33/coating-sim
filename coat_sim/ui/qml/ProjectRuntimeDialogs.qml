import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ApplicationWindow {
    id: window
    objectName: "projectRuntimeDialogs"
    width: 560
    height: 260
    minimumWidth: 480
    minimumHeight: 220
    visible: false
    title: "工程确认"
    // These two narrow facades are installed by the independent runtime.
    // qmllint disable unqualified
    readonly property var projectBridge: runtimeProjects
    readonly property var desktopBridge: runtimeBridge
    // qmllint enable unqualified
    property string closeProjectId: ""
    property string reviewKind: "error"
    property string managementProjectId: ""
    property string managementProjectName: ""
    function openManagement() { management.openFor(managementProjectId) }
    onReviewKindChanged: {
        width = reviewKind === "management" ? Math.max(minimumWidth, Math.min(760, Screen.width - 40)) : 560
        height = reviewKind === "management" ? Math.max(minimumHeight, Math.min(680, Screen.height - 40)) : 260
        x = Math.max(0, Math.round((Screen.width - width) / 2))
        y = Math.max(0, Math.round((Screen.height - height) / 2))
        if (reviewKind !== "management" && management)
            management.dismissForReview()
    }
    // qmllint disable unqualified
    readonly property var colors: typeof Theme !== "undefined" ? Theme.snapshot : null
    // qmllint enable unqualified
    color: colors ? colors.colorSurface : "white"
    font.family: colors ? colors.fontFamily : "Sans Serif"
    font.pointSize: colors ? colors.fontSizeBody : 10
    onClosing: function(close) {
        close.accepted = false
        if (reviewKind !== "releaseWarning")
            window.rejectReview()
    }
    function rejectReview() {
        if (reviewKind === "releaseWarning") {
            projectBridge.acknowledgeReleaseWarning()
        } else if (reviewKind === "project")
            projectBridge.cancelCloseProject(closeProjectId)
        else if (reviewKind === "shutdown")
            desktopBridge.cancelShutdown()
        else if (reviewKind === "management")
            management.dismissForReview()
        window.hide()
    }
    ProjectRuntimeManagement {
        id: management
        projectBridge: window.projectBridge
        projectName: window.managementProjectName
        themeTokens: window.colors
        onDismissed: if (window.reviewKind === "management") window.hide()
    }
    ColumnLayout {
        visible: window.reviewKind !== "management"
        anchors.fill: parent
        anchors.margins: window.colors ? window.colors.spaceLg : 20
        spacing: window.colors ? window.colors.spaceMd : 18
        ThemeControls.ThemeLabel {
            Layout.fillWidth: true
            Layout.fillHeight: true
            wrapMode: Text.Wrap
            color: window.colors ? window.colors.colorText : "#1d2830"
            text: window.reviewKind === "project"
                  ? "此工程有未保存修改。返回保存会保留工程信息与各模块当前内容，请保存相关修改后再关闭；丢弃并关闭会放弃此工程的全部未保存修改。其他工程不受影响，已提交的服务端计算继续运行。"
                  : window.reviewKind === "shutdown"
                    ? "退出此工程进程将放弃未保存修改，是否继续？其他工程不受影响。"
                    : window.projectBridge.projectError
        }
        RowLayout {
            spacing: window.colors ? window.colors.spaceSm : 10
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            ThemeControls.ThemeButton {
                objectName: "projectRuntimeReturnToSave"
                visible: window.reviewKind === "project"
                text: "返回保存"
                onClicked: {
                    window.hide()
                    window.projectBridge.returnToSaveProject(window.closeProjectId)
                }
            }
            ThemeControls.ThemeButton {
                objectName: "projectRuntimeCancelClose"
                text: window.reviewKind === "releaseWarning" ? "确认并退出"
                      : window.reviewKind === "error" ? "关闭提示" : "取消"
                onClicked: window.rejectReview()
            }
            ThemeControls.ThemeButton {
                objectName: "projectRuntimeDiscardClose"
                visible: window.reviewKind === "project" || window.reviewKind === "shutdown"
                text: "丢弃并关闭"
                onClicked: {
                    window.hide()
                    if (window.reviewKind === "project")
                        window.projectBridge.confirmCloseProject(window.closeProjectId)
                    else
                        window.desktopBridge.confirmShutdown()
                }
            }
        }
    }
}
