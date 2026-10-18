pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "theme_controls" as ThemeControls

Item {
    id: root
    objectName: "projectRuntimeManagement"
    signal dismissed()

    required property var projectBridge
    required property var themeTokens
    property string projectId: ""
    property string projectName: ""
    property string localError: ""
    property string statusMessage: ""
    property var information: ({})
    property bool informationLoaded: false
    property bool restoringInformationFields: false
    property string pendingManagementProjectId: ""
    readonly property string currentProjectName: informationLoaded
                                                 ? String(information.name || "") : root.projectName
    readonly property bool informationDirty: informationLoaded
        && (informationNameField.text !== String(information.name || "")
            || informationSummaryField.text !== String(information.summary || "")
            || informationNotesField.text !== String(information.notes || ""))
    property string pendingRecoveryProjectId: ""
    property string pendingRecoveryGeneration: ""
    property string pendingRecoverySection: ""
    property string pendingRecoveryRecordId: ""
    property int pendingRecoveryRevision: -1
    property bool recoveryReviewActive: false

    function stageInformationEdit() {
        if (!root.informationLoaded || root.restoringInformationFields)
            return
        const changed = informationNameField.text !== String(root.information.name || "")
                        || informationSummaryField.text !== String(root.information.summary || "")
                        || informationNotesField.text !== String(root.information.notes || "")
        if (changed)
            root.projectBridge.stageProjectInformationEdit(
                root.projectId, String(root.information.sessionGeneration), Number(root.information.revision),
                informationNameField.text, informationSummaryField.text, informationNotesField.text)
        else
            root.projectBridge.clearProjectInformationEdit(root.projectId, String(root.information.sessionGeneration))
    }

    function restoreInformationFields() {
        root.restoringInformationFields = true
        informationNameField.text = String(root.information.name || "")
        informationSummaryField.text = String(root.information.summary || "")
        informationNotesField.text = String(root.information.notes || "")
        root.restoringInformationFields = false
        if (root.informationLoaded)
            root.projectBridge.clearProjectInformationEdit(root.projectId, String(root.information.sessionGeneration))
    }

    function loadInformation() {
        const value = root.projectBridge.projectInformation(root.projectId)
        if (!value || String(value.projectId || "") !== root.projectId
                || !String(value.sessionGeneration || "")
                || !Number.isInteger(Number(value.revision)) || Number(value.revision) < 0) {
            root.localError = String(root.projectBridge.projectError || "无法读取当前工程信息。")
            return false
        }
        root.information = value
        root.informationLoaded = true
        root.restoreInformationFields()
        root.localError = ""
        root.statusMessage = ""
        return true
    }

    function saveInformation() {
        if (!root.informationLoaded)
            return false
        root.localError = ""
        root.statusMessage = ""
        if (!informationNameField.text.trim()
                || informationNameField.text.length > 100
                || informationSummaryField.text.length > 20000
                || informationNotesField.text.length > 20000) {
            root.localError = "工程名称须为 1–100 字，摘要和备注各须不超过 20000 字。"
            return false
        }
        const accepted = root.projectBridge.saveProjectInformation(
            root.projectId, String(root.information.sessionGeneration),
            Number(root.information.revision), informationNameField.text,
            informationSummaryField.text, informationNotesField.text)
        if (!accepted) {
            root.localError = String(root.projectBridge.projectError || "工程信息未能保存，修改仍保留。")
            return false
        }
        if (!root.loadInformation())
            return false
        root.statusMessage = "工程信息已保存。"
        return true
    }

    function requestDismiss() {
        if (root.informationDirty) {
            discardInformationDialog.open()
            return false
        }
        managementDialog.close()
        return true
    }

    function openFor(projectId) {
        const requestedId = String(projectId || "")
        if (root.projectBridge.projectBusy) {
            if (requestedId === root.projectId) {
                managementDialog.open()
                return true
            }
            root.localError = "工程请求仍在处理，请稍后切换。"
            return false
        }
        if (requestedId && requestedId !== root.projectId && root.informationDirty) {
            root.pendingManagementProjectId = requestedId
            discardInformationDialog.open()
            return false
        }
        let keepDraft = requestedId === root.projectId && root.informationDirty
        root.projectId = requestedId
        if (keepDraft) {
            const current = root.projectBridge.projectInformation(root.projectId)
            if (current && current.sessionGeneration
                    && String(current.sessionGeneration) !== String(root.information.sessionGeneration))
                keepDraft = false
        }
        if (!root.projectId)
            return false
        if (!keepDraft) {
            root.informationLoaded = false
            root.information = ({})
            root.restoreInformationFields()
            root.localError = ""
            root.statusMessage = ""
            root.loadInformation()
        }
        managementDialog.open()
        const flickable = managementScroll.contentItem as Flickable
        if (flickable)
            flickable.contentY = 0
        return true
    }

    function sectionName(section) {
        if (section === "film") return "膜系"
        if (section === "resources") return "资源"
        if (section === "compute") return "计算配置"
        return String(section || "未知类别")
    }

    function openRecoveryCandidates() {
        recoveryDialog.openFor(root.projectId)
    }

    function dismissForReview() {
        root.pendingManagementProjectId = ""
        root.cancelRecoveryReview()
        recoveryConfirmDialog.close()
        recoveryDialog.dismiss()
        exportPackageDialog.close()
        importPackageDialog.close()
        importDestinationParentDialog.close()
        discardInformationDialog.close()
        managementDialog.close()
    }

    function errorText() {
        return root.localError || (root.projectBridge
                                  ? String(root.projectBridge.projectError || "") : "")
    }

    function submitImport() {
        const source = importSourceField.text.trim()
        const destination = importDestinationField.text.trim()
        if (!source || !destination) {
            root.localError = "请选择工程包来源，并填写一个尚不存在的新工程目录。"
            root.statusMessage = ""
            return false
        }
        root.localError = ""
        root.statusMessage = ""
        const accepted = root.projectBridge.importProjectPackage(source, destination)
        if (accepted)
            root.statusMessage = "工程包已提交导入；验证完成后会打开新工程。"
        else
            root.localError = String(root.projectBridge.projectError || "工程包导入未能开始。")
        return accepted
    }

    function submitExport(destination) {
        const target = String(destination || "").trim()
        if (!target) {
            root.localError = "请选择工程包的保存位置。"
            root.statusMessage = ""
            return false
        }
        root.localError = ""
        root.statusMessage = ""
        const accepted = root.projectBridge.exportProjectPackage(root.projectId, target)
        if (accepted)
            root.statusMessage = "工程包已提交导出。"
        else
            root.localError = String(root.projectBridge.projectError || "工程包导出未能开始。")
        return accepted
    }

    Connections {
        target: root.projectBridge
        function onRecoveryConfirmationRequested(projectId, generation, section, revision, recordId) {
            const requestedProjectId = String(projectId || "")
            if (requestedProjectId !== root.projectId) {
                root.projectBridge.cancelRecovery(requestedProjectId)
                return
            }
            root.pendingRecoveryProjectId = requestedProjectId
            root.pendingRecoveryGeneration = String(generation || "")
            root.pendingRecoverySection = String(section || "")
            root.pendingRecoveryRevision = Number(revision)
            root.pendingRecoveryRecordId = String(recordId || "")
            root.recoveryReviewActive = true
            root.localError = ""
            recoveryConfirmDialog.open()
        }
    }

    ThemeControls.ThemeDialogBase {
        id: managementDialog
        objectName: "projectRuntimeManagementDialog"
        parent: Overlay.overlay
        modal: true
        anchors.centerIn: parent
        width: Math.min(720, parent ? Math.max(1, parent.width - 32) : 720)
        height: Math.min(600, parent ? Math.max(1, parent.height - 32) : 600)
        title: "工程管理"
        standardButtons: Dialog.NoButton
        closePolicy: root.informationDirty ? Popup.NoAutoClose
                                          : Popup.CloseOnEscape | Popup.CloseOnPressOutside
        onClosed: {
            if (root.recoveryReviewActive)
                root.cancelRecoveryReview()
            root.dismissed()
        }

        contentItem: ColumnLayout {
            spacing: root.themeTokens.spaceSm
            implicitWidth: Math.max(1, managementDialog.width - 2 * managementDialog.padding)
            implicitHeight: Math.max(1, managementDialog.height - 2 * managementDialog.padding)

            ThemeControls.ThemeLabel {
                objectName: "projectRuntimeManagementProjectId"
                Layout.fillWidth: true
                muted: true
                wrapMode: Text.Wrap
                textFormat: Text.PlainText
                text: "当前工程：" + (root.currentProjectName || "工程")
            }
            ThemeControls.ThemeLabel {
                objectName: "projectRuntimeManagementIdentity"
                Layout.fillWidth: true
                muted: true
                wrapMode: Text.Wrap
                font.pointSize: root.themeTokens.fontSizeSmall
                text: "工程标识：" + root.projectId
            }

            ThemeControls.ThemeLabel {
                objectName: "projectRuntimeManagementError"
                Layout.fillWidth: true
                visible: root.errorText().length > 0
                color: root.themeTokens.colorError
                wrapMode: Text.Wrap
                text: root.errorText()
            }

            ThemeControls.ThemeLabel {
                objectName: "projectRuntimeManagementStatus"
                Layout.fillWidth: true
                visible: root.statusMessage.length > 0
                muted: true
                wrapMode: Text.Wrap
                text: root.statusMessage
            }

            ScrollView {
                id: managementScroll
                objectName: "projectRuntimeManagementScroll"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                ScrollBar.vertical.policy: ScrollBar.AsNeeded

                ColumnLayout {
                    width: managementScroll.availableWidth
                    spacing: root.themeTokens.spaceMd

                    ThemeControls.ThemeSection {
                        objectName: "projectRuntimeInformationSection"
                        Layout.fillWidth: true
                        title: "工程信息"
                        expanded: true

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: root.themeTokens.spaceSm
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                text: "工程名称"
                            }
                            ThemeControls.ThemeField {
                                id: informationNameField
                                objectName: "projectRuntimeInformationNameField"
                                Layout.fillWidth: true
                                accessibleLabel: "工程名称"
                                placeholderText: "工程名称"
                                maximumLength: 100
                                onTextChanged: root.stageInformationEdit()
                                enabled: root.informationLoaded && !root.projectBridge.projectBusy
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                text: "工程摘要"
                            }
                            ScrollView {
                                Layout.fillWidth: true
                                Layout.preferredHeight: root.themeTokens.controlHeightMd * 2
                                clip: true
                                TextArea {
                                    id: informationSummaryField
                                    objectName: "projectRuntimeInformationSummaryField"
                                    Accessible.name: "工程摘要"
                                    placeholderText: "工程摘要（可选）"
                                    wrapMode: TextEdit.Wrap
                                    textFormat: TextEdit.PlainText
                                    onTextChanged: root.stageInformationEdit()
                                    selectByMouse: true
                                    enabled: root.informationLoaded && !root.projectBridge.projectBusy
                                    color: root.themeTokens.colorText
                                    selectionColor: root.themeTokens.colorSelection
                                    background: Rectangle {
                                        color: root.themeTokens.colorSurface
                                        border.color: informationSummaryField.activeFocus
                                            ? root.themeTokens.colorFocusRing : root.themeTokens.colorBorder
                                        border.width: root.themeTokens.borderWidth
                                        radius: root.themeTokens.radiusSm
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                text: "工程备注"
                            }
                            ScrollView {
                                Layout.fillWidth: true
                                Layout.preferredHeight: root.themeTokens.controlHeightMd * 2
                                clip: true
                                TextArea {
                                    id: informationNotesField
                                    objectName: "projectRuntimeInformationNotesField"
                                    Accessible.name: "工程备注"
                                    placeholderText: "工程备注（可选）"
                                    wrapMode: TextEdit.Wrap
                                    textFormat: TextEdit.PlainText
                                    onTextChanged: root.stageInformationEdit()
                                    selectByMouse: true
                                    enabled: root.informationLoaded && !root.projectBridge.projectBusy
                                    color: root.themeTokens.colorText
                                    selectionColor: root.themeTokens.colorSelection
                                    background: Rectangle {
                                        color: root.themeTokens.colorSurface
                                        border.color: informationNotesField.activeFocus
                                            ? root.themeTokens.colorFocusRing : root.themeTokens.colorBorder
                                        border.width: root.themeTokens.borderWidth
                                        radius: root.themeTokens.radiusSm
                                    }
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                Item { Layout.fillWidth: true }
                                ThemeControls.ThemeButton {
                                    objectName: "projectRuntimeInformationCancelButton"
                                    text: "取消修改"
                                    enabled: root.informationDirty && !root.projectBridge.projectBusy
                                    onClicked: root.loadInformation()
                                }
                                ThemeControls.ThemeButton {
                                    objectName: "projectRuntimeInformationSaveButton"
                                    text: "保存工程信息"
                                    primary: true
                                    enabled: root.informationDirty && !root.projectBridge.projectBusy
                                    onClicked: root.saveInformation()
                                }
                            }
                        }
                    }

                    ThemeControls.ThemeSection {
                        objectName: "projectRuntimeRecoverySection"
                        Layout.fillWidth: true
                        title: "异常恢复与已审阅归档"
                        expanded: true

                        ColumnLayout {
                            spacing: root.themeTokens.spaceSm
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                muted: true
                                text: "查看此工程保留的异常退出恢复内容与已审阅归档。恢复操作会先显示确认，并只恢复所选类别。"
                            }
                            ThemeControls.ThemeButton {
                                objectName: "projectRuntimeOpenRecoveryButton"
                                text: "查看异常恢复与已审阅归档…"
                                enabled: !!root.projectBridge && !root.projectBridge.projectBusy
                                onClicked: root.openRecoveryCandidates()
                            }
                        }
                    }

                    ThemeControls.ThemeSection {
                        objectName: "projectRuntimePackagesSection"
                        Layout.fillWidth: true
                        title: "已保存工程包"
                        expanded: true

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: root.themeTokens.spaceSm
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                muted: true
                                text: "导出仅包含已保存的受管工程输入。导入会创建新工程身份；目标目录必须尚不存在。"
                            }
                            ThemeControls.ThemeButton {
                                objectName: "projectRuntimeExportPackageButton"
                                text: "导出已保存工程包…"
                                enabled: !!root.projectBridge && !root.projectBridge.projectBusy
                                onClicked: exportPackageDialog.open()
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                heading: true
                                text: "导入工程包"
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeField {
                                    id: importSourceField
                                    objectName: "projectRuntimeImportSourceField"
                                    accessibleLabel: "工程包来源"
                                    placeholderText: "选择 .cproject 工程包"
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                }
                                ThemeControls.ThemeButton {
                                    objectName: "projectRuntimeBrowseImportSourceButton"
                                    text: "浏览…"
                                    enabled: !!root.projectBridge && !root.projectBridge.projectBusy
                                    onClicked: importPackageDialog.open()
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeField {
                                    id: importDestinationField
                                    objectName: "projectRuntimeImportDestinationField"
                                    accessibleLabel: "新工程目标目录"
                                    accessibleDescription: "填写一个尚不存在的新工程完整目录。"
                                    placeholderText: "尚不存在的新工程完整目录"
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                }
                                ThemeControls.ThemeButton {
                                    objectName: "projectRuntimeBrowseImportDestinationButton"
                                    text: "选父目录…"
                                    enabled: !!root.projectBridge && !root.projectBridge.projectBusy
                                    onClicked: importDestinationParentDialog.open()
                                }
                            }
                            ThemeControls.ThemeButton {
                                objectName: "projectRuntimeImportPackageButton"
                                text: "导入并打开新工程"
                                primary: true
                                enabled: !!root.projectBridge && !root.projectBridge.projectBusy
                                Layout.alignment: Qt.AlignRight
                                onClicked: root.submitImport()
                            }
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ThemeControls.ThemeButton {
                    objectName: "projectRuntimeManagementCloseButton"
                    text: "关闭"
                    onClicked: root.requestDismiss()
                }
            }
        }
    }

    Shortcut {
        sequence: "Esc"
        enabled: managementDialog.visible && root.informationDirty
                 && !discardInformationDialog.visible && !recoveryDialog.visible
                 && !recoveryConfirmDialog.visible && !exportPackageDialog.visible
                 && !importPackageDialog.visible && !importDestinationParentDialog.visible
        onActivated: root.requestDismiss()
    }

    ThemeControls.ThemeDialogBase {
        id: discardInformationDialog
        objectName: "projectRuntimeInformationDiscardDialog"
        parent: Overlay.overlay
        modal: true
        anchors.centerIn: parent
        width: Math.min(480, parent ? Math.max(1, parent.width - 32) : 480)
        title: "放弃工程信息修改？"
        onClosed: root.pendingManagementProjectId = ""
        standardButtons: Dialog.NoButton
        contentItem: ColumnLayout {
            spacing: root.themeTokens.spaceSm
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: "工程信息尚未保存。放弃后将保留原有名称、摘要和备注。"
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ThemeControls.ThemeButton {
                    objectName: "projectRuntimeInformationReturnButton"
                    text: "返回修改"
                    onClicked: {
                        root.pendingManagementProjectId = ""
                        discardInformationDialog.close()
                        informationNameField.forceActiveFocus()
                    }
                }
                ThemeControls.ThemeButton {
                    objectName: "projectRuntimeInformationDiscardButton"
                    text: root.pendingManagementProjectId ? "放弃并切换" : "放弃并关闭"
                    onClicked: {
                        const nextProject = root.pendingManagementProjectId
                        root.pendingManagementProjectId = ""
                        root.restoreInformationFields()
                        root.localError = ""
                        root.statusMessage = ""
                        discardInformationDialog.close()
                        if (nextProject)
                            root.openFor(nextProject)
                        else
                            managementDialog.close()
                    }
                }
            }
        }
    }

    ProjectRecoveryCandidatesDialog {
        id: recoveryDialog
        objectName: "projectRuntimeRecoveryCandidates"
        projectBridge: root.projectBridge
        themeTokens: root.themeTokens
    }

    ThemeControls.ThemeDialogBase {
        id: recoveryConfirmDialog
        objectName: "projectRuntimeRecoveryConfirmDialog"
        parent: Overlay.overlay
        modal: true
        anchors.centerIn: parent
        width: Math.min(520, parent ? Math.max(1, parent.width - 32) : 520)
        title: "确认恢复异常退出内容"
        standardButtons: Dialog.NoButton
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        onClosed: {
            if (root.recoveryReviewActive)
                root.cancelRecoveryReview()
        }

        contentItem: ColumnLayout {
            spacing: root.themeTokens.spaceMd
            implicitWidth: Math.max(1, recoveryConfirmDialog.width - 2 * recoveryConfirmDialog.padding)
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: "将恢复会话 " + root.pendingRecoveryGeneration + " 中的"
                      + root.sectionName(root.pendingRecoverySection)
                      + "内容。恢复后尚未保存；其他类别不会改变，计算任务不会自动重启。"
            }
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                visible: root.errorText().length > 0
                color: root.themeTokens.colorError
                wrapMode: Text.WordWrap
                text: root.errorText()
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ThemeControls.ThemeButton {
                    objectName: "projectRuntimeRecoveryCancelButton"
                    text: "取消"
                    onClicked: {
                        root.cancelRecoveryReview()
                        recoveryConfirmDialog.close()
                    }
                }
                ThemeControls.ThemeButton {
                    objectName: "projectRuntimeRecoveryConfirmButton"
                    text: "确认恢复"
                    primary: true
                    onClicked: {
                        const requestedProjectId = root.pendingRecoveryProjectId
                        root.recoveryReviewActive = false
                        recoveryConfirmDialog.close()
                        const result = root.projectBridge.restoreRecovery(requestedProjectId)
                        root.localError = result ? ""
                            : String(root.projectBridge.projectError || "恢复尚未完成；请查看工程状态后重试。")
                        root.statusMessage = result ? "所选类别的异常退出内容已恢复，尚未保存。" : ""
                    }
                }
            }
        }
    }

    FileDialog {
        id: exportPackageDialog
        objectName: "projectRuntimeExportPackageDialog"
        title: "导出已保存工程包"
        fileMode: FileDialog.SaveFile
        defaultSuffix: "cproject"
        nameFilters: ["Coat 工程包 (*.cproject)"]
        onAccepted: root.submitExport(selectedFile.toString())
    }

    FileDialog {
        id: importPackageDialog
        objectName: "projectRuntimeImportPackageDialog"
        title: "选择 .cproject 工程包"
        fileMode: FileDialog.OpenFile
        nameFilters: ["Coat 工程包 (*.cproject)", "所有文件 (*)"]
        onAccepted: importSourceField.text = selectedFile.toString()
    }

    FolderDialog {
        id: importDestinationParentDialog
        objectName: "projectRuntimeImportDestinationParentDialog"
        title: "选择新工程目录的父目录"
        onAccepted: {
            const parentPath = selectedFolder.toString().replace(/[\\/]+$/, "")
            importDestinationField.text = parentPath + "/Imported project"
        }
    }

    function cancelRecoveryReview() {
        if (!root.recoveryReviewActive)
            return
        const requestedProjectId = root.pendingRecoveryProjectId
        root.recoveryReviewActive = false
        root.projectBridge.cancelRecovery(requestedProjectId)
    }
}
