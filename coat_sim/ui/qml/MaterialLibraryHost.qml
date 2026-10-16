pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import "." as LocalComponents

LocalComponents.AppWindow {
    id: host
    objectName: "materialLibraryHost"
    width: 1100
    height: 820
    minimumWidth: 900
    minimumHeight: 620
    initialMaximized: false
    title: "材料库"
    statusText: editorInstance === null
        ? ""
        : bridge.valid
          ? ((host.editorInstance.library.dirty ? "● 有未保存修改 · " : "") + host.editorInstance.library.status)
          : bridge.error
    statusSeverity: editorInstance === null ? "info"
        : !bridge.valid ? "error"
        : bridge.loading ? "busy" : "info"
    statusDetail: statusText

    signal closed()
    property var projectNavigation: null
    property var projectResources: null
    property bool projectPendingTextEdited: false
    property bool projectCloseAuthorized: false
    LocalComponents.ProjectPendingEdits { editorWindow: host }
    property bool allowClose: false
    property bool reopenEditorAfterCancel: false
    property var editorInstance: null
    property var editorIncubator: null
    property bool editorLoading: false
    property bool editorLoadFailed: false
    property bool editorOpenRequested: false

    function commitPendingEdits() { contentItem.forceActiveFocus(); return true }

    onClosing: close => {
        if (projectCloseAuthorized) {
            projectCloseAuthorized = false
            close.accepted = true
        } else if (projectResources !== null) {
            close.accepted = projectResources.requestClose()
        } else if (allowClose) {
            bridge.cancelPendingLoad()
            cancelEditorIncubation()
        } else if (bridge.dirty) {
            close.accepted = false
            closeConfirmation.open()
        } else {
            bridge.cancelPendingLoad()
            cancelEditorIncubation()
        }
    }

    function finishEditorIncubation(incubator) {
        if (editorIncubator !== incubator) return
        if (incubator.status === Component.Ready) {
            editorInstance = incubator.object
            editorIncubator = null
            editorLoading = false
            editorLoadFailed = false
            if (editorOpenRequested && editorInstance) {
                editorOpenRequested = false
                editorInstance.open()
            }
        } else if (incubator.status === Component.Error) {
            editorIncubator = null
            editorLoading = false
            editorLoadFailed = true
            editorOpenRequested = false
            console.error("Unable to create material library editor")
        }
    }

    function openEditor() {
        if (editorInstance) {
            editorInstance.open()
            return
        }
        editorOpenRequested = true
        if (editorIncubator) return
        editorLoading = true
        editorLoadFailed = false
        const incubator = editorComponent.incubateObject(host, {}, Qt.Asynchronous)
        editorIncubator = incubator
        if (!incubator) {
            editorLoading = false
            editorLoadFailed = true
            editorOpenRequested = false
            console.error("Unable to start material library editor creation")
            return
        }
        incubator.onStatusChanged = function() {
            host.finishEditorIncubation(incubator)
        }
        finishEditorIncubation(incubator)
    }

    function cancelEditorIncubation() {
        if (!editorIncubator) return
        const incubator = editorIncubator
        editorIncubator = null
        editorLoading = false
        editorOpenRequested = false
        incubator.clear()
    }

    function finishClose() {
        allowClose = true
        bridge.cancelPendingLoad()
        closeConfirmation.close()
        host.close()
    }

    function cancelCloseConfirmation() {
        closeConfirmation.close()
        if (reopenEditorAfterCancel) {
            reopenEditorAfterCancel = false
            host.openEditor()
        }
    }

    Component {
        id: editorComponent
        LocalComponents.MaterialLibraryDialog {
            objectName: "materialLibraryDialog"
            library: bridge
            projectNavigation: host.projectNavigation
            projectResources: host.projectResources
            modal: false
            closePolicy: Popup.NoAutoClose
            showUseAction: false
            hostedInWindowChrome: true
            y: Math.max(0, (parent.height - height) / 2 + 16)
            height: Math.max(0, parent.height - 68)
            onClosed: {
                if (bridge.dirty) {
                    host.reopenEditorAfterCancel = true
                    closeConfirmation.open()
                } else {
                    host.close()
                }
            }
        }
    }

    Label {
        objectName: "materialLibraryHostLoadingLabel"
        anchors.centerIn: parent
        visible: editorInstance === null && !editorLoadFailed
        text: "正在加载材料库…"
    }
    Column {
        anchors.centerIn: parent
        spacing: 8
        visible: editorLoadFailed
        Label {
            objectName: "materialLibraryHostLoadError"
            text: "材料库加载失败。"
        }
        Button {
            text: "重试"
            onClicked: host.openEditor()
        }
    }

    Dialog {
        id: closeConfirmation
        objectName: "materialLibraryCloseConfirmation"
        title: "未保存的材料修改"
        modal: true
        anchors.centerIn: parent
        width: Math.min(480, parent.width - 40)
        closePolicy: Popup.NoAutoClose
        contentItem: Label {
            text: "材料修改尚未保存。保存后关闭、放弃修改并关闭，或取消返回材料库。"
            wrapMode: Text.Wrap
        }
        footer: DialogButtonBox {
            Button {
                objectName: "saveAndCloseMaterialButton"
                text: "保存并关闭"
                enabled: bridge.dirty && bridge.valid && !bridge.loading
                onClicked: {
                    if (bridge.save()) host.finishClose()
                    else {
                        closeConfirmation.close()
                        host.reopenEditorAfterCancel = true
                        host.openEditor()
                    }
                }
            }
            Button { objectName: "discardAndCloseMaterialButton"; text: "丢弃并关闭"; onClicked: host.finishClose() }
            Button { objectName: "cancelMaterialCloseButton"; text: "取消"; onClicked: host.cancelCloseConfirmation() }
        }
    }

    Component.onCompleted: host.openEditor()
}
