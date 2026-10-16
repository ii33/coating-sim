import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "."
import "theme_controls" as ThemeControls

AppWindow {
    id: window
    property bool projectCloseAuthorized: false
    property var projectNavigation: null
    required property var controller
    property bool allowClose: false
    property alias parametersExpanded: appearanceWorkspace.parametersExpanded
    property alias previewMode: appearanceWorkspace.previewMode
    property alias spectrumTarget: appearanceWorkspace.spectrumTarget
    property bool compactActions: width < 1000
    property int themeRevision: Theme.revision

    titleBase: "薄膜外观"
    captionBase: "本地光谱渲染 · 仅几何预览"
    projectNameForTitle: projectNavigation ? projectNavigation.projectName : ""
    statusText: controller.errorText.length > 0 ? "渲染失败" : controller.status
    statusSeverity: controller.errorText.length > 0 || controller.phase === "failed" ? "error"
        : controller.phase === "cancelling" ? "warning"
        : controller.phase === "starting" || controller.phase === "running" ? "busy"
        : controller.phase === "complete" ? "success" : "info"
    statusDetail: controller.phase + " · " + controller.status
        + (controller.errorText.length > 0 ? " · " + controller.errorText : "")
    width: 1160
    height: 820
    minimumWidth: 720
    minimumHeight: 520
    initialMaximized: false
    color: Theme.snapshot.colorWindow

    onClosing: function(close) {
        if (!allowClose && !projectCloseAuthorized) {
            close.accepted = false
            controller.requestClose()
        } else {
            controller.detach_observers()
        }
    }

    Connections {
        target: window.controller
        function onCloseConfirmationRequested() { closeDialog.open() }
        function onCloseReady() {
            window.allowClose = true
            Qt.callLater(window.close)
        }
    }

    ThemeControls.ThemeDialogBase {
        id: closeDialog
        title: "渲染仍在运行"
        modal: true
        anchors.centerIn: parent
        width: Math.min(460, window.width - Theme.snapshot.spaceXl * 2)
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Label {
            text: "取消本窗口的本地渲染，并在当前波长帧结束后关闭？选择“否”返回窗口。"
            color: Theme.snapshot.colorText
            wrapMode: Text.WordWrap
            padding: Theme.snapshot.spaceMd
        }
        onAccepted: controller.resolveClose(true)
        onRejected: controller.resolveClose(false)
    }

    ProjectModuleSelector {
        id: projectSelector
        x: 0
        y: window.contentTopMargin
        navigation: window.projectNavigation
        currentModuleKey: "film_appearance"
    }

    AppearanceWorkspace {
        id: appearanceWorkspace
        objectName: "appearanceWorkspace"
        anchors.fill: parent
        anchors.topMargin: window.contentTopMargin + (projectSelector.visible ? projectSelector.height : 0)
        controller: window.controller
        showAssetSelector: false
        onCategorySelectionRequested: appearanceWorkspace.selectedCategory = category
    }
}
