pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "." as LocalComponents
import "theme_controls" as ThemeControls

LocalComponents.AppWindow {
    id: window
    objectName: "projectBbsWindow"
    required property var projectBridge
    required property var projectNavigation
    required property string projectId
    required property string sessionGeneration
    property bool projectCloseAuthorized: false
    property bool allowClose: false
    property bool initialized: false
    titleBase: "工程 BBS"
    projectNameForTitle: projectNavigation ? projectNavigation.projectName : ""
    minimumWidth: 680
    minimumHeight: 500
    readonly property var bbsSnapshot: page.snapshotState
    statusText: page.busy ? "正在处理工程记录…" : page.dirty ? "有未发布或未保存的内容" : "工程记录与报告"
    statusDetail: page.errorMessage || page.statusMessage
    statusSeverity: page.errorMessage ? "error" : "info"

    function commitPendingEdits() { page.retain(); return true }
    function dismissDialogs() { page.dismissDialogs(); keepCloseDialog.close() }
    function keepAndClose() {
        page.retain()
        allowClose = true
        keepCloseDialog.close()
        window.close()
        allowClose = false
    }
    Component.onCompleted: page.openFor(projectId, sessionGeneration)
    onVisibleChanged: {
        if (visible && !initialized) {
            initialized = true
            Qt.callLater(function() { if (!page.mode) page.refresh(0) })
        }
    }
    onClosing: function(close) {
        page.retain()
        if (page.busy) {
            close.accepted = false
            page.errorMessage = "工程请求仍在处理，请稍后关闭窗口。"
        } else if (!projectCloseAuthorized && !allowClose && page.dirty) {
            close.accepted = false
            keepCloseDialog.open()
        } else {
            window.dismissDialogs()
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.topMargin: window.contentTopMargin + window.themeColors.spaceSm
        anchors.leftMargin: window.themeColors.spaceMd
        anchors.rightMargin: window.themeColors.spaceMd
        anchors.bottomMargin: window.themeColors.spaceMd
        spacing: window.themeColors.spaceSm
        RowLayout {
            Layout.fillWidth: true
            LocalComponents.ProjectModuleSelector {
                navigation: window.projectNavigation
                currentModuleKey: "project_bbs"
                enabled: !page.busy
            }
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                text: "工程记录与报告 · 持久保存，按需刷新"
                muted: true
                wrapMode: Text.Wrap
            }
        }
        LocalComponents.ProjectBbs {
            id: page
            Layout.fillWidth: true
            Layout.fillHeight: true
            projectBridge: window.projectBridge
            themeTokens: window.themeColors
        }
    }

    ThemeControls.ThemeDialogBase {
        id: keepCloseDialog
        objectName: "projectBbsKeepCloseDialog"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(460, parent ? parent.width - 32 : 460)
        title: "内容尚未发布或保存"
        modal: true
        closePolicy: Popup.CloseOnEscape
        contentItem: ColumnLayout {
            spacing: window.themeColors.spaceMd
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                text: "保留并关闭后，可从工程菜单重新打开 BBS 继续编辑。未发布内容仅保留在本次工程会话中。"
                wrapMode: Text.Wrap
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                ThemeControls.ThemeButton {
                    objectName: "projectBbsContinueEditing"
                    text: "继续编辑"
                    onClicked: keepCloseDialog.close()
                }
                ThemeControls.ThemeButton {
                    objectName: "projectBbsKeepAndClose"
                    text: "保留并关闭"
                    onClicked: window.keepAndClose()
                }
            }
        }
    }
}
