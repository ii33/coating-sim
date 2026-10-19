pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "." as LocalComponents
import "theme_controls" as ThemeControls

RowLayout {
    id: controls
    objectName: "projectResourceControls"

    property var projectResources: null
    property var informationProvider: projectResources
    property string saveButtonObjectName: "projectResourceSaveButton"
    property var extraTransferActions: []
    property var beforeDocumentAction: function(action, documentId) { return true }
    property string activeTransferMode: ""
    property string transferSource: "global"
    property string transferTarget: "global"
    property string transferFileTarget: ""
    property string transferFileKind: ""
    property string categoryFilter: ""
    readonly property var targetKinds: projectResources ? listValues(projectResources.transferKinds) : []
    property var selectedTransferIds: []
    property var pendingDecisions: ({})
    property string activeReplacementRequestId: ""
    property var operationBarOverflowActions: []
    property var operationBarOverflowOwner: null
    readonly property var operationBarOverflowButton: moreButton
    readonly property bool hasAdditionalResourceActions: projectResources !== null
        && projectResources.moduleKey === "process"
    readonly property bool externalOverflowPlacement: operationBarOverflowOwner !== null
    readonly property real moreButtonWidth: Math.max(38, moreButton.implicitWidth)
    readonly property real operationBarOverflowWidth: moreButtonWidth
    readonly property real operationBarInlineWidth: documentPicker.Layout.minimumWidth + informationButton.implicitWidth
        + newButton.implicitWidth + saveButton.implicitWidth + copyButton.implicitWidth
        + transferButton.implicitWidth + spacing * 5
    readonly property real operationBarMinimumWidth: projectResources !== null
        ? documentPicker.Layout.minimumWidth + informationButton.implicitWidth + spacing
          + (externalOverflowPlacement ? 0 : moreButtonWidth + spacing)
        : informationButton.implicitWidth
    readonly property real operationBarPreferredWidth: projectResources !== null
        ? documentPicker.Layout.preferredWidth + informationButton.implicitWidth
          + newButton.implicitWidth + saveButton.implicitWidth + copyButton.implicitWidth
          + transferButton.implicitWidth + spacing * 5
          + (!externalOverflowPlacement && (hasAdditionalResourceActions || operationBarOverflowActions.length > 0)
             ? moreButtonWidth + spacing : 0)
        : informationButton.implicitWidth
    readonly property var resourceActionLayout: planResourceActions()
    readonly property bool showNewAction: resourceActionLayout.inline[0]
    readonly property bool showSaveAction: resourceActionLayout.inline[1]
    readonly property bool showCopyAction: resourceActionLayout.inline[2]
    readonly property bool showTransferAction: resourceActionLayout.inline[3]
    readonly property bool showMoreActions: resourceActionLayout.more
    signal extraTransferActionRequested(string key)
    readonly property bool transferBusy: projectResources !== null
                                        && !!projectResources.transferBusy
    readonly property bool transferCancellable: projectResources !== null
                                               && !!projectResources.transferCancellable
    readonly property bool resourceBusy: projectResources !== null
                                        && !!projectResources.resourceBusy
    readonly property bool resourceCancellable: projectResources !== null
                                               && !!projectResources.resourceCancellable
    readonly property bool operationBusy: controls.transferBusy || controls.resourceBusy
    readonly property bool transferPlanCurrent: projectResources !== null
                                                && !controls.transferBusy
                                                && projectResources.transferPlan
                                                && !!projectResources.transferPlan.plan_id

    visible: projectResources !== null || informationProvider !== null
    spacing: 4
    width: visible ? operationBarPreferredWidth : 0
    implicitWidth: operationBarPreferredWidth
    Layout.preferredWidth: visible ? operationBarPreferredWidth : 0
    Layout.minimumWidth: visible ? 0 : 0

    function planResourceActions() {
        if (projectResources === null)
            return { inline: [false, false, false, false], more: false }
        const widths = [newButton.implicitWidth, saveButton.implicitWidth,
                        copyButton.implicitWidth, transferButton.implicitWidth]
        const base = documentPicker.Layout.minimumWidth + informationButton.implicitWidth + spacing
        const persistentMore = hasAdditionalResourceActions || operationBarOverflowActions.length > 0
        const fullWidth = base + widths.reduce((sum, value) => sum + value + spacing, 0)
                          + (persistentMore && !externalOverflowPlacement ? moreButtonWidth + spacing : 0)
        if (width >= fullWidth)
            return { inline: [true, true, true, true], more: persistentMore }
        // Reserve the one shared overflow entry only when an action cannot fit.
        // Widths come from the themed controls, including font/density changes.
        let remaining = Math.max(0, width - base
            - (externalOverflowPlacement ? 0 : moreButtonWidth + spacing))
        const inline = []
        for (const value of widths) {
            const fits = value + spacing <= remaining
            inline.push(fits)
            if (fits)
                remaining -= value + spacing
        }
        return { inline: inline, more: true }
    }

    function restoreOverflowFocus(button) {
        if (!button.visible && button.activeFocus)
            moreButton.forceActiveFocus()
    }

    function refreshDocuments() {
        if (projectResources === null)
            return
        const rows = listValues(projectResources.documents)
        documentPicker.model = rows
        documentPicker.currentIndex = rows.findIndex(function(row) {
            return String(row.id) === String(projectResources.currentDocumentId || "")
        })
    }

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

    function rowsForCategory(rows, kind) {
        const values = listValues(rows)
        const result = []
        for (const row of values) {
            if (targetKinds.length && targetKinds.indexOf(String(row.kind || "")) < 0)
                continue
            if (String(row.kind || "") === "binary_asset")
                continue
            if (!kind) {
                result.push(row)
                continue
            }
            if (String(row.kind || "") === kind)
                result.push(row)
        }
        return result
    }

    function visibleTransferCategories(values) {
        const result = []
        for (const value of listValues(values)) {
            if (String(value) !== "binary_asset" && (!targetKinds.length || targetKinds.indexOf(String(value)) >= 0))
                result.push(String(value))
        }
        return result
    }

    function categoryName(kind) {
        const names = {
            source: "源",
            substrate: "基片",
            mask: "修正板",
            machine: "设备",
            process: "工艺",
            film: "膜系",
            film_design: "膜系设计",
            expression_template: "表达式模板",
            material_profile: "材料",
            optical_material: "光学材料",
            deposition_configuration: "沉积配置",
            raytrace_request: "光追请求",
            appearance_configuration: "外观配置",
            binary_asset: "输入资产"
        }
        return names[String(kind || "")] || String(kind || "其他")
    }

    function decisionLabel(action) {
        const labels = {
            copy: "复制为副本",
            overwrite: "覆盖现有资源",
            skip: "跳过此资源",
            reuse: "使用现有资源",
            import: "导入资源"
        }
        return labels[String(action || "")] || String(action || "")
    }

    function decisionOptions(actions) {
        return listValues(actions).map(function(action) {
            return { key: String(action), label: controls.decisionLabel(action) }
        })
    }

    function allowDocumentAction(action, documentId) {
        if (typeof beforeDocumentAction !== "function")
            return true
        return Boolean(beforeDocumentAction(String(action), String(documentId || "")))
    }

    function containsId(values, identity) {
        for (const value of listValues(values)) {
            if (String(value) === String(identity))
                return true
        }
        return false
    }

    function invalidatePreview() {
        pendingDecisions = ({})
        if (projectResources !== null)
            projectResources.clearTransferPreview()
    }

    function beginTransfer(mode) {
        if (projectResources === null || operationBusy)
            return
        activeTransferMode = mode
        transferSource = mode === "import" ? "global" : "project"
        transferTarget = mode === "import" ? "project" : "global"
        transferFileTarget = ""
        transferFileKind = ""
        categoryFilter = ""
        selectedTransferIds = []
        invalidatePreview()
        // Open even if the initial catalog read fails. The status remains
        // visible and the user can still try a package file as another source.
        projectResources.beginTransfer(mode)
        transferDialog.open()
    }

    function beginFileImport(source) {
        if (projectResources === null || operationBusy)
            return false
        activeTransferMode = "import"
        transferSource = "file"
        transferTarget = "project"
        transferFileTarget = ""
        transferFileKind = ""
        categoryFilter = ""
        selectedTransferIds = []
        invalidatePreview()
        const started = Boolean(projectResources.beginFileImport(String(source)))
        // Keep the transfer status visible even if the selected file is rejected.
        transferDialog.open()
        return started
    }

    function inspectSource(source) {
        if (operationBusy)
            return
        selectedTransferIds = []
        categoryFilter = ""
        invalidatePreview()
        transferSource = source === "global" ? "global" : "file"
        if (projectResources !== null)
            projectResources.inspectTransferSource(source)
    }

    function selectedIdsForPreview() {
        return selectedTransferIds.map(function(identity) { return String(identity) })
    }

    function selectedKindsForPreview() {
        const rows = listValues(projectResources && projectResources.transferCatalog
                    ? projectResources.transferCatalog.rows : [])
        const kinds = []
        for (const row of rows) {
            if (containsId(selectedTransferIds, row.id) && kinds.indexOf(String(row.kind)) < 0)
                kinds.push(String(row.kind))
        }
        return kinds
    }

    function canPreviewTarget() {
        if (activeTransferMode === "import")
            return true
        if (transferTarget === "global")
            return selectedKindsForPreview().length === 1
        return transferFileTarget.length > 0
    }

    function runPreview() {
        if (projectResources === null || operationBusy || selectedTransferIds.length === 0)
            return
        const target = activeTransferMode === "import"
            ? "project"
            : transferTarget === "file" ? transferFileTarget : "global"
        invalidatePreview()
        projectResources.previewTransfer(selectedIdsForPreview(), target)
    }

    function decisionFor(identity) {
        return String(pendingDecisions[String(identity)] || "")
    }

    function setDecision(identity, decision) {
        const next = Object.assign({}, pendingDecisions)
        next[String(identity)] = String(decision)
        pendingDecisions = next
    }

    function allConflictsDecided() {
        if (!transferPlanCurrent || projectResources === null)
            return false
        const rows = listValues(projectResources.transferPlan && projectResources.transferPlan.rows
                    ? projectResources.transferPlan.rows : [])
        for (const row of rows) {
            if (row.conflict && !decisionFor(row.id))
                return false
        }
        return true
    }

    function decisionPayload() {
        const result = {}
        for (const key in pendingDecisions)
            result[key] = pendingDecisions[key]
        return result
    }

    function finishTransfer() {
        if (projectResources !== null && transferPlanCurrent && !operationBusy)
            projectResources.applyTransfer(decisionPayload())
    }

    function cancelTransferAndClose() {
        if (transferBusy && !transferCancellable)
            return
        if (projectResources !== null)
            projectResources.cancelTransfer()
        transferDialog.close()
    }

    function resolveReplacement(decision) {
        if (projectResources === null)
            return
        const result = Boolean(projectResources.confirmReplace(decision))
        if (result) {
            activeReplacementRequestId = ""
            replaceDialog.close()
            return
        }
        const requestId = String(projectResources.replacementRequestId || "")
        if (requestId.length > 0)
            activeReplacementRequestId = requestId
    }

    ThemeControls.ThemeComboBox {
        id: documentPicker
        objectName: "projectManagedDocumentPicker"
        visible: controls.projectResources !== null
        Layout.fillWidth: true
        Layout.minimumWidth: 60
        Layout.preferredWidth: 145
        Layout.maximumWidth: 210
        enabled: !controls.operationBusy
        textRole: "name"
        valueRole: "id"
        displayText: {
            const information = controls.informationProvider
                                ? controls.informationProvider.resourceInformation || ({}) : ({})
            const draftName = String(information.name || "").trim()
            const dirty = controls.projectResources && controls.projectResources.dirty
            if (draftName.length > 0)
                return draftName + (dirty ? " *" : "")
            if (documentPicker.currentIndex < 0)
                return dirty ? "未保存资源" : "未命名资源"
            return documentPicker.currentText
        }
        ToolTip.visible: hovered && controls.projectResources !== null
                         && String(controls.projectResources.currentDocumentId || "").length > 0
        ToolTip.text: controls.projectResources !== null
                      ? String(controls.projectResources.currentDocumentId || "") : ""
        accessibleLabel: "打开当前模块工程资源"
        onActivated: index => {
            if (controls.operationBusy) {
                controls.refreshDocuments()
                Qt.callLater(controls.refreshDocuments)
                return
            }
            if (controls.projectResources === null || index < 0)
                return
            const rows = documentPicker.model
            const row = rows && rows[index] ? rows[index] : null
            if (row && controls.allowDocumentAction("open", row.id))
                controls.projectResources.openDocument(String(row.id))
            // Opening can return after an asynchronous dirty-draft decision.
            // Re-read the authoritative current ID so Cancel restores the old row.
            controls.refreshDocuments()
            Qt.callLater(controls.refreshDocuments)
        }
    }

    ThemeControls.ThemeButton {
        id: informationButton
        objectName: "projectResourceInformationButton"
        text: "信息"
        Accessible.name: "查看或编辑当前资源信息"
        enabled: controls.informationProvider !== null && !controls.operationBusy
                 && controls.informationProvider.resourceInformation !== undefined
        onClicked: {
            resourceInformationDialog.resetForOpen()
            resourceInformationDialog.open()
        }
    }

    ThemeControls.ThemeButton {
        id: newButton
        objectName: "projectResourceNewButton"
        visible: controls.projectResources !== null && controls.showNewAction
        text: "新建"
        Accessible.name: "新建项目资源"
        Accessible.ignored: !visible
        focusPolicy: visible ? Qt.StrongFocus : Qt.NoFocus
        onVisibleChanged: controls.restoreOverflowFocus(newButton)
        enabled: !controls.operationBusy
        onClicked: {
            if (controls.allowDocumentAction("new", ""))
                controls.projectResources.newDocument()
        }
    }

    ThemeControls.ThemeButton {
        id: saveButton
        objectName: controls.saveButtonObjectName
        visible: controls.projectResources !== null && controls.showSaveAction
        text: "保存"
        Accessible.name: "保存到项目"
        Accessible.ignored: !visible
        focusPolicy: visible ? Qt.StrongFocus : Qt.NoFocus
        onVisibleChanged: controls.restoreOverflowFocus(saveButton)
        enabled: controls.projectResources !== null && controls.projectResources.canSave
                 && !controls.operationBusy
        onClicked: {
            if (controls.allowDocumentAction("save", ""))
                controls.projectResources.saveCurrent()
        }
    }

    ThemeControls.ThemeButton {
        id: copyButton
        objectName: "projectResourceCreateCopyButton"
        visible: controls.projectResources !== null && controls.showCopyAction
        text: "创建副本"
        Accessible.name: "创建当前项目资源的副本"
        Accessible.ignored: !visible
        focusPolicy: visible ? Qt.StrongFocus : Qt.NoFocus
        enabled: controls.projectResources !== null && controls.projectResources.canSave
                 && !controls.operationBusy
        onVisibleChanged: controls.restoreOverflowFocus(copyButton)
        onClicked: {
            if (controls.allowDocumentAction("copy", ""))
                controls.projectResources.saveCopyCurrent()
        }
    }

    LocalComponents.ProjectResourceInformationDialog {
        id: resourceInformationDialog
        projectResources: controls.informationProvider
    }

    ThemeControls.ThemeButton {
        id: moreButton
        parent: controls.operationBarOverflowOwner || controls
        readonly property bool operationBarExternalAnchor: controls.externalOverflowPlacement
        objectName: "projectResourceMoreActionsButton"
        visible: controls.projectResources !== null && controls.showMoreActions
        text: "..."
        Accessible.name: "更多项目资源操作"
        Accessible.ignored: !visible
        focusPolicy: visible ? Qt.StrongFocus : Qt.NoFocus
        onVisibleChanged: {
            if (!visible) {
                const restoreFocus = activeFocus || resourceMenu.visible
                resourceMenu.close()
                if (restoreFocus) {
                    const target = copyButton.visible && copyButton.enabled ? copyButton : informationButton
                    if (target.enabled)
                        target.forceActiveFocus()
                    else
                        controls.forceActiveFocus()
                }
            }
        }
        width: controls.moreButtonWidth
        Layout.preferredWidth: controls.moreButtonWidth
        enabled: !controls.operationBusy || controls.operationBarOverflowActions.length > 0
        onClicked: resourceMenu.open()
        Menu {
            id: resourceMenu
            objectName: "projectResourceActionsMenu"
            y: moreButton.height
            MenuItem {
                objectName: "projectResourceOverflowNewAction"
                text: newButton.text
                visible: !controls.showNewAction
                height: visible ? implicitHeight : 0
                enabled: visible && newButton.enabled
                Accessible.ignored: !visible
                onTriggered: newButton.click()
            }
            MenuItem {
                objectName: "projectResourceOverflowSaveAction"
                text: saveButton.text
                visible: !controls.showSaveAction
                height: visible ? implicitHeight : 0
                enabled: visible && saveButton.enabled
                Accessible.ignored: !visible
                onTriggered: saveButton.click()
            }
            MenuItem {
                objectName: "projectResourceCreateCopyAction"
                text: copyButton.text
                visible: !controls.showCopyAction
                height: visible ? implicitHeight : 0
                enabled: visible && copyButton.enabled
                Accessible.ignored: !visible
                onTriggered: copyButton.click()
            }
            MenuItem {
                objectName: "projectResourceOverflowTransferAction"
                text: transferButton.text
                visible: !controls.showTransferAction
                height: visible ? implicitHeight : 0
                enabled: visible && transferButton.enabled
                Accessible.ignored: !visible
                onTriggered: transferButton.click()
            }
            MenuSeparator {
                visible: controls.hasAdditionalResourceActions
                         && (!controls.showNewAction || !controls.showSaveAction
                             || !controls.showCopyAction || !controls.showTransferAction)
                height: visible ? implicitHeight : 0
            }
            MenuItem {
                objectName: "projectResourceSaveConfigurationAction"
                text: "保存沉积配置…"
                height: visible ? implicitHeight : 0
                enabled: visible && !controls.operationBusy
                Accessible.ignored: !visible
                visible: controls.projectResources !== null
                         && controls.projectResources.moduleKey === "process"
                onTriggered: {
                    configurationName.text = "沉积配置"
                    parameters.text = controls.projectResources.defaultParameters
                    configurationDialog.open()
                }
            }
            MenuSeparator {
                visible: controls.operationBarOverflowActions.length > 0
                    && (controls.hasAdditionalResourceActions || !controls.showNewAction
                        || !controls.showSaveAction || !controls.showCopyAction || !controls.showTransferAction)
                height: visible ? implicitHeight : 0
            }
            Instantiator {
                model: controls.operationBarOverflowActions
                delegate: MenuItem {
                    id: operationItem
                    required property var modelData
                    readonly property var source: controls.operationBarOverflowOwner
                        ? controls.operationBarOverflowOwner.actionSource(modelData) : null
                    objectName: "operationBarOverflow_" + String(modelData.objectName || "action")
                    text: source ? String(source.text || source.Accessible.name || "操作") : ""
                    visible: !!source && modelData.visible
                    height: visible ? implicitHeight : 0
                    enabled: visible && !!source && source.enabled
                    Accessible.ignored: !visible
                    checkable: !!source && source.checkable
                    checked: !!source && source.checked
                    onTriggered: {
                        if (controls.operationBarOverflowOwner)
                            controls.operationBarOverflowOwner.triggerItem(modelData)
                        checked = Qt.binding(function() {
                            return !!operationItem.source && operationItem.source.checked
                        })
                    }
                }
                onObjectAdded: (index, object) => resourceMenu.insertItem(resourceMenu.count, object)
                onObjectRemoved: (index, object) => resourceMenu.removeItem(object)
            }
        }
    }

    ThemeControls.ThemeButton {
        id: transferButton
        objectName: "projectResourceTransferMenuButton"
        visible: controls.projectResources !== null && controls.showTransferAction
        text: "导入/导出"
        Accessible.name: "导入或导出项目资源"
        Accessible.ignored: !visible
        focusPolicy: visible ? Qt.StrongFocus : Qt.NoFocus
        onVisibleChanged: controls.restoreOverflowFocus(transferButton)
        enabled: !controls.operationBusy
        onClicked: transferMenu.open()
        Menu {
            id: transferMenu
            parent: transferButton.visible ? transferButton : moreButton
            MenuItem {
                objectName: "projectResourceImportMenuItem"
                text: "导入资源…"
                onTriggered: importPackageDialog.open()
            }
            MenuItem {
                objectName: "projectResourceExportMenuItem"
                text: "导出资源…"
                onTriggered: controls.beginTransfer("export")
            }
            MenuItem {
                objectName: "projectResourceExportBundleMenuItem"
                text: "资源文件及依赖…"
                enabled: controls.projectResources !== null
                         && typeof controls.projectResources.exportDocument === "function"
                         && controls.projectResources.canExportDocument !== false
                onTriggered: resourceBundleFolderDialog.open()
            }
            MenuSeparator {
                visible: controls.listValues(controls.extraTransferActions).length > 0
                height: visible ? implicitHeight : 0
            }
            Instantiator {
                model: controls.listValues(controls.extraTransferActions)
                delegate: MenuItem {
                    id: extraTransferMenuItem
                    required property var modelData
                    objectName: "projectResourceExtraAction_" + String(extraTransferMenuItem.modelData.key || "")
                    text: String(extraTransferMenuItem.modelData.text || "")
                    enabled: extraTransferMenuItem.modelData.enabled !== false
                    onTriggered: controls.extraTransferActionRequested(
                                     String(extraTransferMenuItem.modelData.key || ""))
                }
                onObjectAdded: (index, object) => transferMenu.insertItem(index + 4, object)
                onObjectRemoved: (index, object) => transferMenu.removeItem(object)
            }
        }
    }

    FolderDialog {
        id: resourceBundleFolderDialog
        objectName: "projectResourceBundleFolderDialog"
        title: "选择完整资源文件及依赖的导出目录"
        onAccepted: {
            if (controls.projectResources !== null
                    && controls.projectResources.canExportDocument !== false)
                controls.projectResources.exportDocument(selectedFolder)
        }
    }

    FileDialog {
        id: importPackageDialog
        objectName: "projectResourceImportFileDialog"
        title: "选择资源文件或工程包"
        fileMode: FileDialog.OpenFile
        nameFilters: controls.projectResources ? controls.projectResources.transferFileFilters : []
        onAccepted: controls.beginFileImport(selectedFile.toString())
    }

    FileDialog {
        id: existingProjectTargetDialog
        title: "选择已存在的目标 .coatproj 工程"
        fileMode: FileDialog.OpenFile
        nameFilters: ["Coat 工程描述文件 (*.coatproj)"]
        onAccepted: {
            controls.transferFileTarget = selectedFile.toString()
            controls.transferFileKind = "existing_project"
            controls.transferTarget = "file"
            controls.invalidatePreview()
        }
    }

    FileDialog {
        id: newPackageTargetDialog
        title: "新建 .cproject 工程包"
        fileMode: FileDialog.SaveFile
        defaultSuffix: "cproject"
        nameFilters: ["Coat 工程包 (*.cproject)"]
        onAccepted: {
            controls.transferFileTarget = selectedFile.toString()
            controls.transferFileKind = "new_package"
            controls.transferTarget = "file"
            controls.invalidatePreview()
        }
    }

    ThemeControls.ThemeDialogBase {
        id: transferDialog
        objectName: "projectResourceTransferDialog"
        title: controls.activeTransferMode === "import" ? "导入资源" : "导出资源"
        parent: Overlay.overlay
        anchors.centerIn: parent
        modal: true
        width: parent ? Math.max(1, Math.min(760, parent.width - 32)) : 760
        height: parent ? Math.max(1, Math.min(650, parent.height - 32)) : 650
        standardButtons: Dialog.NoButton
        closePolicy: controls.transferBusy && !controls.transferCancellable
                     ? Popup.NoAutoClose : Popup.CloseOnEscape

        ColumnLayout {
            anchors.fill: parent
            spacing: 8

            ThemeControls.ThemeLabel {
                text: controls.activeTransferMode === "import"
                      ? "选择来源、类别和资源；依赖会以只读方式自动包含。"
                      : "选择项目资源和目标；依赖会以只读方式自动包含。"
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }

            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeLabel {
                    text: controls.activeTransferMode === "import" ? "来源" : "来源"
                    muted: true
                }
                ThemeControls.ThemeLabel {
                    objectName: "projectResourceTransferSource"
                    text: controls.activeTransferMode === "import"
                          ? (controls.transferSource === "file" ? "工程资源包" : "全局资源库")
                          : "当前项目资源库"
                }
                Item { Layout.fillWidth: true }
                ThemeControls.ThemeButton {
                    objectName: "projectResourceTransferBrowseSourceButton"
                    text: "浏览文件…"
                    visible: controls.activeTransferMode === "import"
                    enabled: !controls.transferBusy
                    onClicked: importPackageDialog.open()
                }
                ThemeControls.ThemeButton {
                    objectName: "projectResourceTransferGlobalSourceButton"
                    text: "全局资源库"
                    visible: controls.activeTransferMode === "import"
                             && controls.transferSource !== "global"
                    enabled: !controls.transferBusy
                    onClicked: controls.inspectSource("global")
                }
            }

            RowLayout {
                Layout.fillWidth: true
                visible: controls.activeTransferMode === "export"
                ThemeControls.ThemeLabel { text: "目标"; muted: true }
                RadioButton {
                    objectName: "projectResourceExportGlobalTarget"
                    text: "全局资源库"
                    checked: controls.transferTarget === "global"
                    enabled: !controls.transferBusy
                    onClicked: {
                        controls.transferTarget = "global"
                        controls.transferFileTarget = ""
                        controls.transferFileKind = ""
                        controls.invalidatePreview()
                    }
                }
                RadioButton {
                    objectName: "projectResourceExportFileTarget"
                    text: "工程文件"
                    checked: controls.transferTarget === "file"
                    enabled: !controls.transferBusy
                    onClicked: {
                        controls.transferTarget = "file"
                        controls.transferFileKind = "existing_project"
                        controls.transferFileTarget = ""
                        controls.invalidatePreview()
                        existingProjectTargetDialog.open()
                    }
                }
                ThemeControls.ThemeButton {
                    objectName: "projectResourceChooseCoatprojTargetButton"
                    text: "选择已有 .coatproj…"
                    visible: controls.transferTarget === "file"
                             && controls.transferFileKind === "existing_project"
                    enabled: !controls.transferBusy
                    onClicked: existingProjectTargetDialog.open()
                }
                ThemeControls.ThemeButton {
                    objectName: "projectResourceChooseCprojectTargetButton"
                    text: "新建 .cproject…"
                    enabled: !controls.transferBusy
                    onClicked: newPackageTargetDialog.open()
                }
                ThemeControls.ThemeButton {
                    objectName: "projectResourceChooseExportFileButton"
                    text: "更改目标文件…"
                    visible: controls.transferTarget === "file"
                             && controls.transferFileTarget.length > 0
                    enabled: !controls.transferBusy
                    onClicked: controls.transferFileKind === "existing_project"
                               ? existingProjectTargetDialog.open()
                               : newPackageTargetDialog.open()
                }
                ThemeControls.ThemeLabel {
                    visible: controls.transferTarget === "file" && controls.transferFileTarget.length > 0
                    text: controls.transferFileTarget
                    muted: true
                    elide: Text.ElideMiddle
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeLabel {
                    visible: controls.transferTarget === "global"
                    text: "全局库每次只传输一个类别；请按类别筛选。"
                    muted: true
                    Layout.fillWidth: true
                }
            }

            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeLabel { text: "类别"; muted: true }
                ThemeControls.ThemeComboBox {
                    id: categoryPicker
                    objectName: "projectResourceTransferCategoryPicker"
                    Layout.preferredWidth: 190
                    model: {
                        const values = controls.projectResources && controls.projectResources.transferCatalog
                                    ? controls.projectResources.transferCatalog.categories || [] : []
                        return controls.targetKinds.length ? controls.visibleTransferCategories(values) : ["", ...controls.visibleTransferCategories(values)]
                    }
                    displayText: !controls.targetKinds.length && categoryPicker.currentIndex <= 0
                                 ? "全部类别" : controls.categoryName(categoryPicker.currentText)
                    Accessible.name: "工程资源类别筛选"
                    enabled: !controls.transferBusy
                    onActivated: index => {
                        controls.categoryFilter = !controls.targetKinds.length && index <= 0
                                ? "" : String(categoryPicker.model[index])
                        controls.selectedTransferIds = []
                        controls.invalidatePreview()
                    }
                }
                Item { Layout.fillWidth: true }
                ThemeControls.ThemeLabel {
                    text: controls.selectedTransferIds.length + " 项已选"
                    muted: true
                }
            }

            ThemeControls.ThemeSurface {
                objectName: "projectResourceTransferCatalogSurface"
                Layout.fillWidth: true
                Layout.fillHeight: true
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 0
                    ThemeControls.ThemeLabel {
                        text: "资源"
                        heading: true
                        Layout.fillWidth: true
                    }
                    ScrollView {
                        id: transferCatalogScroll
                        objectName: "projectResourceTransferCatalogScroll"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        contentWidth: availableWidth
                        clip: true
                        ScrollBar.vertical.policy: ScrollBar.AsNeeded
                        ColumnLayout {
                            width: transferCatalogScroll.availableWidth
                            spacing: 2
                            Repeater {
                                id: transferCatalogRepeater
                                objectName: "projectResourceTransferCatalogRepeater"
                                model: controls.rowsForCategory(
                                           controls.projectResources && controls.projectResources.transferCatalog
                                             ? controls.projectResources.transferCatalog.rows : [],
                                           controls.categoryFilter)
                                delegate: RowLayout {
                                    id: transferCatalogRow
                                    objectName: "projectResourceTransferRow_" + String(transferCatalogRow.modelData.id)
                                    required property var modelData
                                    Layout.fillWidth: true
                                    CheckBox {
                                        id: transferResourceCheck
                                        objectName: "projectResourceTransferSelect_" + String(transferCatalogRow.modelData.id)
                                        text: String(transferCatalogRow.modelData.name || transferCatalogRow.modelData.id)
                                        contentItem: Text {
                                            text: transferResourceCheck.text
                                            textFormat: Text.PlainText
                                            font: transferResourceCheck.font
                                            color: transferResourceCheck.palette.text
                                            elide: Text.ElideRight
                                            verticalAlignment: Text.AlignVCenter
                                            leftPadding: transferResourceCheck.indicator.width
                                                        + transferResourceCheck.spacing
                                        }
                                        checked: controls.containsId(controls.selectedTransferIds, transferCatalogRow.modelData.id)
                                        enabled: !controls.transferBusy
                                        Accessible.name: controls.categoryName(transferCatalogRow.modelData.kind)
                                                         + "：" + transferResourceCheck.text
                                        onToggled: {
                                            const next = controls.selectedTransferIds.slice()
                                            const index = next.findIndex(function(value) {
                                                return String(value) === String(transferCatalogRow.modelData.id)
                                            })
                                            if (checked && index < 0)
                                                next.push(String(transferCatalogRow.modelData.id))
                                            else if (!checked && index >= 0)
                                                next.splice(index, 1)
                                            controls.selectedTransferIds = next
                                            controls.invalidatePreview()
                                        }
                                    }
                                    ThemeControls.ThemeLabel {
                                        text: controls.categoryName(transferCatalogRow.modelData.kind)
                                        muted: true
                                    }
                                    ThemeControls.ThemeLabel {
                                        text: transferCatalogRow.modelData.read_only ? "来源不改写" : ""
                                        muted: true
                                    }
                                    ThemeControls.ThemeLabel {
                                        text: controls.listValues(transferCatalogRow.modelData.dependencies).length > 0
                                              ? "含 " + controls.listValues(transferCatalogRow.modelData.dependencies).length + " 项依赖"
                                              : ""
                                        muted: true
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel {
                                visible: !controls.projectResources
                                         || !controls.projectResources.transferCatalog
                                         || !controls.projectResources.transferCatalog.rows
                                         || controls.listValues(controls.projectResources.transferCatalog.rows).length === 0
                                text: "没有可传输的资源。"
                                muted: true
                                Layout.fillWidth: true
                            }
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeLabel {
                    text: controls.activeTransferMode === "import"
                          ? "导入目标：当前项目"
                          : "导出目标：" + (controls.transferTarget === "file"
                                          ? (controls.transferFileTarget.length
                                             ? (controls.transferFileKind === "new_package"
                                                ? "新建 .cproject 包" : "已有 .coatproj 工程")
                                             : "请选择目标文件")
                                          : "全局资源库")
                    muted: true
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeButton {
                    objectName: "projectResourceTransferPreviewButton"
                    text: "预览依赖与冲突"
                    enabled: controls.selectedTransferIds.length > 0
                             && controls.canPreviewTarget()
                             && !controls.transferBusy
                    onClicked: controls.runPreview()
                }
            }

            ThemeControls.ThemeSurface {
                objectName: "projectResourceTransferPlanSurface"
                visible: controls.transferPlanCurrent
                Layout.fillWidth: true
                Layout.preferredHeight: 115
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    ThemeControls.ThemeLabel { text: "传输预览"; heading: true }
                    ScrollView {
                        id: transferPlanScroll
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        contentWidth: availableWidth
                        clip: true
                        ColumnLayout {
                            width: transferPlanScroll.availableWidth
                            Repeater {
                                model: controls.listValues(controls.projectResources && controls.projectResources.transferPlan
                                       ? controls.projectResources.transferPlan.rows : [])
                                delegate: RowLayout {
                                    id: transferPlanRow
                                    required property var modelData
                                    Layout.fillWidth: true
                                    ThemeControls.ThemeLabel {
                                        objectName: "projectResourceTransferPreview_"
                                                    + String(transferPlanRow.modelData.id)
                                        Layout.fillWidth: true
                                        text: controls.containsId(
                                                  controls.projectResources.transferPlan.dependency_ids,
                                                  transferPlanRow.modelData.id)
                                              ? "依赖 · 自动包含 · " + controls.categoryName(transferPlanRow.modelData.kind) + " · " + transferPlanRow.modelData.name
                                              : controls.categoryName(transferPlanRow.modelData.kind) + " · " + transferPlanRow.modelData.name
                                        elide: Text.ElideRight
                                        textFormat: Text.PlainText
                                    }
                                    ThemeControls.ThemeLabel {
                                        text: transferPlanRow.modelData.conflict ? "存在冲突" : "可传输"
                                        muted: !transferPlanRow.modelData.conflict
                                    }
                                    ThemeControls.ThemeComboBox {
                                        id: conflictDecisionPicker
                                        visible: !!transferPlanRow.modelData.conflict
                                        Layout.preferredWidth: 150
                                        model: controls.decisionOptions(transferPlanRow.modelData.choices)
                                        textRole: "label"
                                        valueRole: "key"
                                        enabled: !controls.transferBusy
                                        displayText: conflictDecisionPicker.currentIndex < 0
                                                     ? "选择处理方式…" : conflictDecisionPicker.currentText
                                        Accessible.name: "冲突处理方式：" + String(transferPlanRow.modelData.name || transferPlanRow.modelData.id)
                                        currentIndex: {
                                            const chosen = controls.decisionFor(transferPlanRow.modelData.id)
                                            return chosen.length
                                                ? conflictDecisionPicker.model.findIndex(option => String(option.key) === chosen)
                                                : -1
                                        }
                                        onActivated: index => {
                                            const option = conflictDecisionPicker.model[index]
                                            if (option)
                                                controls.setDecision(transferPlanRow.modelData.id, String(option.key))
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }

            ThemeControls.ThemeLabel {
                objectName: "projectResourceTransferStatus"
                text: controls.projectResources ? controls.projectResources.transferStatus : ""
                wrapMode: Text.Wrap
                Layout.fillWidth: true
                visible: text.length > 0
            }

            Repeater {
                model: controls.listValues(controls.projectResources && controls.projectResources.transferCatalog
                       ? controls.projectResources.transferCatalog.warnings : [])
                delegate: ThemeControls.ThemeLabel {
                    id: transferWarningRow
                    required property var modelData
                    text: String(transferWarningRow.modelData)
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ThemeControls.ThemeButton {
                    objectName: "projectResourceTransferCancelButton"
                    enabled: !controls.transferBusy || controls.transferCancellable
                    text: controls.transferBusy && !controls.transferCancellable
                          ? "正在提交，无法取消"
                          : controls.transferBusy ? "取消当前操作" : "取消"
                    onClicked: {
                        controls.cancelTransferAndClose()
                    }
                }
                ThemeControls.ThemeButton {
                    objectName: "projectResourceTransferApplyButton"
                    text: controls.activeTransferMode === "import" ? "导入所选资源" : "导出所选资源"
                    primary: true
                    enabled: controls.allConflictsDecided() && !controls.transferBusy
                    onClicked: controls.finishTransfer()
                }
            }
        }

        onRejected: {
            if (controls.transferBusy && !controls.transferCancellable) {
                transferDialog.open()
                return
            }
            if (controls.projectResources !== null)
                controls.projectResources.cancelTransfer()
        }
    }

    Dialog {
        id: replaceDialog
        objectName: "projectResourceReplaceDialog"
        title: "替换当前资源？"
        modal: true
        width: 420
        standardButtons: Dialog.NoButton
        ColumnLayout {
            anchors.fill: parent
            ThemeControls.ThemeLabel {
                text: "当前资源有未保存修改。"
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ThemeControls.ThemeButton {
                    objectName: "projectResourceReplaceSaveButton"
                    text: "保存后继续"
                    enabled: controls.projectResources !== null
                             && !controls.operationBusy
                    onClicked: {
                        controls.resolveReplacement("save")
                    }
                }
                ThemeControls.ThemeButton {
                    objectName: "projectResourceReplaceDiscardButton"
                    text: "放弃修改并继续"
                    enabled: controls.projectResources !== null
                             && !controls.operationBusy
                    onClicked: {
                        controls.resolveReplacement("discard")
                    }
                }
                ThemeControls.ThemeButton {
                    text: "取消"
                    enabled: !controls.resourceBusy || controls.resourceCancellable
                    onClicked: {
                        controls.resolveReplacement("cancel")
                    }
                }
            }
            ThemeControls.ThemeLabel {
                text: controls.projectResources !== null ? controls.projectResources.status : ""
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
        }
        closePolicy: controls.resourceBusy && !controls.resourceCancellable
                     ? Popup.NoAutoClose : Popup.CloseOnEscape
        onRejected: {
            if (controls.resourceBusy && !controls.resourceCancellable) {
                replaceDialog.open()
                return
            }
            controls.resolveReplacement("cancel")
        }
    }

    Dialog {
        id: configurationDialog
        objectName: "projectDefaultConfigurationDialog"
        title: "保存沉积配置"
        modal: true
        width: 480
        standardButtons: Dialog.Save | Dialog.Cancel
        ColumnLayout {
            anchors.fill: parent
            ThemeControls.ThemeLabel { text: "配置名" }
            ThemeControls.ThemeField { id: configurationName; Layout.fillWidth: true; accessibleLabel: "配置名" }
            ThemeControls.ThemeLabel {
                text: "计算参数 JSON（空对象使用明确的计算默认值）"
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.preferredHeight: 150
                TextArea { id: parameters; wrapMode: TextEdit.Wrap; selectByMouse: true }
            }
            ThemeControls.ThemeLabel {
                text: controls.projectResources !== null ? controls.projectResources.status : ""
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
        }
        onAccepted: {
            if (!controls.projectResources.saveDefaultConfiguration(configurationName.text, parameters.text))
                configurationDialog.open()
        }
    }

    Dialog {
        id: closeDialog
        objectName: "projectResourceCloseDialog"
        onClosed: if (controls.projectResources !== null) controls.projectResources.cancelCloseReview()
        title: "项目资源尚未保存"
        modal: true
        width: 390
        ColumnLayout {
            anchors.fill: parent
            ThemeControls.ThemeLabel {
                text: "当前设计有未保存修改。可以保存后关闭，或放弃修改并关闭；取消会保留全部输入。"
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            RowLayout {
                ThemeControls.ThemeButton {
                    objectName: "projectResourceSaveCloseButton"
                    text: "保存并关闭"
                    onClicked: { if (controls.projectResources.confirmClose("save")) closeDialog.close() }
                }
                ThemeControls.ThemeButton {
                    objectName: "projectResourceDiscardCloseButton"
                    text: "放弃修改并关闭"
                    onClicked: { if (controls.projectResources.confirmClose("discard")) closeDialog.close() }
                }
                ThemeControls.ThemeButton { objectName: "projectResourceCancelCloseButton"; text: "取消"; onClicked: closeDialog.close() }
            }
            ThemeControls.ThemeLabel {
                text: controls.projectResources !== null ? controls.projectResources.status : ""
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
        }
    }

    Connections {
        target: controls.projectResources
        ignoreUnknownSignals: true
        function onChanged() { controls.refreshDocuments() }
        function onTransferChanged() {
            // Transfer state has its own notifier. Do not read document rows
            // here: a background catalog task may hold the service read lock.
        }
        function onTransferFinished(success) {
            if (success)
                transferDialog.close()
        }
        function onReplaceDecisionRequested() {
            controls.activeReplacementRequestId = controls.projectResources !== null
                ? String(controls.projectResources.replacementRequestId || "") : ""
            replaceDialog.open()
        }
        function onReplacementFinished(replacementId, success, error) {
            if (String(replacementId) !== controls.activeReplacementRequestId)
                return
            if (success) {
                controls.activeReplacementRequestId = ""
                replaceDialog.close()
            }
        }
        function onCloseDecisionRequested() { closeDialog.open() }
    }
    Component.onCompleted: refreshDocuments()
}
