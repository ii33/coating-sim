import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ThemeControls.ThemeDialogBase {
    id: dialog
    objectName: "projectResourceInformationDialog"

    property var projectResources: null
    property var information: projectResources !== null
                              ? projectResources.resourceInformation || ({}) : ({})
    property bool editing: false
    property bool restoringFields: false
    property string expectedToken: ""
    property string recoveryStatus: ""
    readonly property var pendingEdit: projectResources !== null
                                       ? projectResources.pendingInformationEdit || ({}) : ({})
    readonly property bool hasOtherPendingEdit: Object.keys(pendingEdit).length > 0
                                                && String(pendingEdit.token || pendingEdit.expected_token
                                                          || pendingEdit.expectedToken || "")
                                                   !== String(information.token || "")
    readonly property bool operationBusy: projectResources !== null
                                          && (!!projectResources.transferBusy
                                              || !!projectResources.resourceBusy)

    title: "资源信息"
    modal: true
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: parent ? Math.max(1, Math.min(540, parent.width - 32)) : 540
    height: parent
            ? Math.max(1, Math.min(parent.height - 32,
                                   informationContent.implicitHeight
                                   + (informationStatus.visible
                                      ? informationStatus.implicitHeight : 0)
                                   + informationButtons.implicitHeight
                                   + informationBody.spacing * 2
                                   + (dialog.header ? dialog.header.implicitHeight : 0)
                                   + dialog.padding * 2))
            : 560
    standardButtons: Dialog.NoButton

    function listValues(values) {
        if (values === null || values === undefined || values.length === undefined)
            return []
        const result = []
        const count = Number(values.length)
        if (!Number.isFinite(count) || count < 0)
            return result
        for (let index = 0; index < count; ++index)
            result.push(values[index])
        return result
    }

    function fieldEditable(fieldName) {
        const fields = dialog.listValues(dialog.information ? dialog.information.editable_fields : [])
        return fields.indexOf(fieldName) >= 0
    }

    function resetForOpen() {
        const pending = dialog.pendingEdit
        const pendingToken = String(pending.expected_token || pending.expectedToken || pending.token || "")
        const currentToken = String(dialog.information.token || "")
        const hasPending = Object.keys(pending).length > 0 && pendingToken === currentToken
        dialog.recoveryStatus = Object.keys(pending).length > 0 && !hasPending
                ? "未确认的编辑属于其他资源，已保留且未应用；切回原资源可恢复。" : ""
        dialog.expectedToken = hasPending ? pendingToken : currentToken
        dialog.restoringFields = true
        nameField.text = String(hasPending ? pending.name || "" : dialog.information.name || "")
        summaryField.text = String(hasPending ? pending.summary || "" : dialog.information.summary || "")
        notesField.text = String(hasPending ? pending.notes || "" : dialog.information.notes || "")
        dialog.restoringFields = false
        dialog.editing = hasPending
    }

    function beginEditing() {
        dialog.expectedToken = String(dialog.information.token || "")
        nameField.text = String(dialog.information.name || "")
        summaryField.text = String(dialog.information.summary || "")
        notesField.text = String(dialog.information.notes || "")
        dialog.editing = true
    }

    function stageEdit() {
        if (dialog.restoringFields || !dialog.editing || dialog.projectResources === null)
            return
        dialog.projectResources.stageInformationEdit(
            nameField.text, summaryField.text, notesField.text, dialog.expectedToken)
    }

    function clearEditBuffer() {
        if (dialog.projectResources !== null)
            dialog.projectResources.clearInformationEdit()
        dialog.recoveryStatus = ""
    }

    function restorePendingEditIfMatching() {
        if (dialog.visible || dialog.projectResources === null)
            return
        const pending = dialog.pendingEdit
        const pendingToken = String(pending.expected_token || pending.expectedToken || pending.token || "")
        if (Object.keys(pending).length > 0
                && pendingToken.length > 0
                && pendingToken === String(dialog.information.token || "")) {
            dialog.resetForOpen()
            dialog.open()
        }
    }

    function applyEdit() {
        if (dialog.projectResources !== null
                && dialog.projectResources.updateResourceInformation(
                    nameField.text, summaryField.text, notesField.text, dialog.expectedToken)) {
            dialog.editing = false
            dialog.close()
        }
    }

    ColumnLayout {
        id: informationBody
        anchors.fill: parent
        spacing: 8

        ScrollView {
            id: informationScrollView
            objectName: "projectResourceInformationScrollView"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 0
            Layout.preferredHeight: informationContent.implicitHeight
            contentWidth: availableWidth
            contentHeight: informationContent.implicitHeight
            clip: true
            ScrollBar.vertical: ScrollBar {
                objectName: "projectResourceInformationVerticalScrollBar"
                policy: ScrollBar.AsNeeded
            }

            ColumnLayout {
                id: informationContent
                width: informationScrollView.availableWidth
                spacing: 8

                ThemeControls.ThemeLabel {
                    objectName: "projectResourceInformationNameHeading"
                    text: "名称"
                    heading: true
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeLabel {
                    id: nameViewer
                    objectName: "projectResourceInformationNameViewer"
                    visible: !dialog.editing
                    text: String(dialog.information.name || "未命名")
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                    Layout.preferredHeight: nameViewer.contentHeight
                }
                ThemeControls.ThemeField {
                    id: nameField
                    objectName: "projectResourceInformationName"
                    visible: dialog.editing
                    enabled: dialog.fieldEditable("name")
                    maximumLength: Number(dialog.information.name_limit || 120)
                    accessibleLabel: "资源名称"
                    placeholderText: "资源名称"
                    Layout.fillWidth: true
                    onTextEdited: dialog.stageEdit()
                }

                ThemeControls.ThemeLabel {
                    objectName: "projectResourceInformationSummaryHeading"
                    text: "概述"
                    heading: true
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeLabel {
                    id: summaryViewer
                    objectName: "projectResourceInformationSummaryViewer"
                    visible: !dialog.editing
                    text: String(dialog.information.summary || "无")
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                    Layout.preferredHeight: summaryViewer.contentHeight
                }
                TextArea {
                    id: summaryField
                    objectName: "projectResourceInformationSummary"
                    visible: dialog.editing
                    enabled: dialog.fieldEditable("summary")
                    property string accessibleLabel: "资源概述"
                    Accessible.name: summaryField.accessibleLabel
                    placeholderText: "概述"
                    wrapMode: TextEdit.Wrap
                    selectByMouse: true
                    Layout.fillWidth: true
                    Layout.preferredHeight: 72
                    onTextChanged: dialog.stageEdit()
                    color: dialog.themeTokens.colorText
                    selectionColor: dialog.themeTokens.colorSelection
                    selectedTextColor: dialog.themeTokens.colorText
                    background: Rectangle {
                        color: dialog.themeTokens.colorSurface
                        border.color: summaryField.activeFocus
                                     ? dialog.themeTokens.colorFocusRing
                                     : dialog.themeTokens.colorBorder
                        border.width: summaryField.activeFocus
                                      ? dialog.themeTokens.focusRingWidth
                                      : dialog.themeTokens.borderWidth
                        radius: dialog.themeTokens.radiusSm
                    }
                }

                ThemeControls.ThemeLabel {
                    objectName: "projectResourceInformationNotesHeading"
                    text: "备注"
                    heading: true
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeLabel {
                    id: notesViewer
                    objectName: "projectResourceInformationNotesViewer"
                    visible: !dialog.editing
                    text: String(dialog.information.notes || "无")
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                    Layout.preferredHeight: notesViewer.contentHeight
                }
                TextArea {
                    id: notesField
                    objectName: "projectResourceInformationNotes"
                    visible: dialog.editing
                    enabled: dialog.fieldEditable("notes")
                    Accessible.name: "资源备注"
                    placeholderText: "备注"
                    wrapMode: TextEdit.Wrap
                    selectByMouse: true
                    Layout.fillWidth: true
                    Layout.preferredHeight: 128
                    onTextChanged: dialog.stageEdit()
                    color: dialog.themeTokens.colorText
                    selectionColor: dialog.themeTokens.colorSelection
                    selectedTextColor: dialog.themeTokens.colorText
                    background: Rectangle {
                        color: dialog.themeTokens.colorSurface
                        border.color: notesField.activeFocus
                                     ? dialog.themeTokens.colorFocusRing
                                     : dialog.themeTokens.colorBorder
                        border.width: notesField.activeFocus
                                      ? dialog.themeTokens.focusRingWidth
                                      : dialog.themeTokens.borderWidth
                        radius: dialog.themeTokens.radiusSm
                    }
                }
            }
        }

        ThemeControls.ThemeLabel {
            id: informationStatus
            objectName: "projectResourceInformationStatus"
            text: dialog.recoveryStatus.length > 0
                  ? dialog.recoveryStatus
                  : dialog.projectResources !== null ? dialog.projectResources.status : ""
            visible: text.length > 0
            wrapMode: Text.Wrap
            Layout.fillWidth: true
        }

        RowLayout {
            id: informationButtons
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            ThemeControls.ThemeButton {
                objectName: "projectResourceInformationEditButton"
                text: "编辑"
                visible: !dialog.editing
                         && dialog.listValues(dialog.information.editable_fields).length > 0
                         && !dialog.hasOtherPendingEdit
                onClicked: dialog.beginEditing()
            }
            ThemeControls.ThemeButton {
                objectName: "projectResourceInformationCancelButton"
                text: "取消"
                visible: dialog.editing
                onClicked: {
                    dialog.clearEditBuffer()
                    dialog.editing = false
                    dialog.close()
                }
            }
            ThemeControls.ThemeButton {
                objectName: "projectResourceInformationSaveButton"
                text: "确认更新"
                primary: true
                visible: dialog.editing
                enabled: dialog.projectResources !== null && !dialog.operationBusy
                onClicked: dialog.applyEdit()
            }
            ThemeControls.ThemeButton {
                text: "关闭"
                visible: !dialog.editing
                onClicked: dialog.close()
            }
        }
    }

    onRejected: {
        dialog.clearEditBuffer()
        dialog.editing = false
    }

    Component.onCompleted: Qt.callLater(dialog.restorePendingEditIfMatching)

    Connections {
        target: dialog.projectResources
        ignoreUnknownSignals: true
        function onChanged() { Qt.callLater(dialog.restorePendingEditIfMatching) }
    }
}
