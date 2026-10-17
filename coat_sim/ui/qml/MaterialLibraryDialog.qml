import QtQuick
import QtQuick.Controls
import QtQuick.Window
import QtQuick.Layouts
import QtQuick.Dialogs as FileDialogs
import "." as LocalComponents
import "theme_controls" as ThemeControls
import "theme_controls/NumericFormat.js" as NumericFormat

LocalComponents.ThemePageDialog {
    id: dialog
    required property var library
    property var projectNavigation: null
    property var projectResources: null
    property bool showUseAction: true
    property bool hostedInWindowChrome: false
    property bool embeddedInAssetLibrary: false
    readonly property real standaloneChromeTopInset: {
        const hostWindow = parent ? parent.Window.window : null
        return hostWindow && typeof hostWindow.contentTopMargin !== "undefined"
            ? hostWindow.contentTopMargin : 0
    }
    property var expandedOpticalSetIds: []
    property var opticalCurveStates: ({})
    property Item editorFocusReturnTarget: null
    property int editorFocusGeneration: 0
    readonly property string observedMaterialId: library.currentId
    readonly property int observedOpticalSetDataRevision: library.opticalSetDataRevision
    property string scrollMaterialId: ""
    property string exportMaterialId: ""
    property var pendingExportIds: []
    property var setEditorInstance: null
    property var referenceDialogInstance: null
    title: hostedInWindowChrome || embeddedInAssetLibrary ? "" : "材料库"
    padding: embeddedInAssetLibrary ? 0 : themeTokens.spaceLg
    modal: true
    x: embeddedInAssetLibrary ? 0 : parent ? Math.max(0, (parent.width - width) / 2) : 0
    y: embeddedInAssetLibrary ? 0 : parent ? (hostedInWindowChrome
        ? Math.max(0, (parent.height - height) / 2 + 16)
        : Math.max(standaloneChromeTopInset, (parent.height - height) / 2)) : 0
    width: Math.max(0, parent.width - (embeddedInAssetLibrary ? 0 : 32))
    height: Math.max(0, parent.height - (embeddedInAssetLibrary ? 0 : hostedInWindowChrome
        ? 68 : Math.max(40, standaloneChromeTopInset + 4)))
    header: ThemeControls.ThemePopupHeader {
        objectName: "materialLibraryPopupHeader"
        visible: !dialog.embeddedInAssetLibrary
        title: dialog.hostedInWindowChrome ? "" : dialog.title
        statusText: dialog.hostedInWindowChrome ? ""
            : dialog.library.valid
              ? ((dialog.library.dirty ? "● 有未保存修改 · " : "") + dialog.library.status)
              : dialog.library.error
        statusSeverity: dialog.hostedInWindowChrome ? "info"
            : !dialog.library.valid ? "error"
            : dialog.library.loading ? "busy" : "info"
        statusDetail: statusText
        tooltipBoundsItem: Overlay.overlay
    }
    onOpened: {
        library.prepareOpen()
        for (const identity of expandedOpticalSetIds) {
            if (hasOpticalSet(identity)) startOpticalCurveRequest(identity)
        }
    }
    onClosed: {
        editorFocusReturnTarget = null
        editorFocusGeneration += 1
        cancelAllOpticalCurveRequests()
        library.cancelPendingLoad()
        library.cancelImportPreview()
        library.cancelOpticalInversion()
        if (referenceDialogInstance) referenceDialogInstance.close()
        if (setEditorInstance) setEditorInstance.close()
    }
    onObservedMaterialIdChanged: {
        cancelAllOpticalCurveRequests(true)
        expandedOpticalSetIds = []
        opticalCurveStates = ({})
    }
    onObservedOpticalSetDataRevisionChanged: {
        refreshExpandedOpticalCurves()
    }
    property string modelPrefix: contentTabs.currentIndex === 1 ? "resistance" : "electron_beam"
    readonly property var processColumnWidths: [176, 146, 72]
    readonly property var firstOpticalSetCard: opticalSetsRepeater.count > 0 ? opticalSetsRepeater.itemAt(0) : null
    function field(key) { return String(library.fields[key] === undefined ? "" : library.fields[key]) }
    function modelField(key) { return field(modelPrefix + "_" + key) }
    function processValueText(rowData) {
        const value = String(rowData.value || "").trim()
        const unit = String(rowData.unit || "").trim()
        return value.length > 0 ? (unit.length > 0 ? value + " " + unit : value) : unit
    }
    function editModel(key, value) { if (!library.opticalOnly) library.setField(modelPrefix + "_" + key, value) }
    function ensureSetEditor() {
        if (setEditorInstance) return setEditorInstance
        setEditorInstance = setEditorComponent.createObject(
            dialog, { parent: dialog.parent })
        return setEditorInstance
    }
    function openSetEditor(identity) {
        openSetEditorFromControl(identity, null)
    }
    function openSetEditorFromControl(identity, focusTarget) {
        editorFocusGeneration += 1
        editorFocusReturnTarget = focusTarget || null
        const editor = ensureSetEditor()
        if (editor) editor.beginEdit(identity)
    }
    function restoreEditorFocus() {
        const target = editorFocusReturnTarget
        const generation = editorFocusGeneration
        editorFocusReturnTarget = null
        if (target) Qt.callLater(function() {
            if (dialog.visible && generation === dialog.editorFocusGeneration
                    && target.visible) target.forceActiveFocus()
        })
    }
    function newOpticalSet() {
        editorFocusReturnTarget = null
        editorFocusGeneration += 1
        const editor = ensureSetEditor()
        if (editor) editor.beginNew()
    }
    function openReferenceDetails() {
        if (!referenceDialogInstance)
            referenceDialogInstance = referenceDialogComponent.createObject(
                dialog, { parent: dialog.parent })
        if (referenceDialogInstance) referenceDialogInstance.open()
    }
    function isOpticalSetExpanded(identity) {
        return expandedOpticalSetIds.indexOf(identity) >= 0
    }
    function hasOpticalSet(identity) {
        return library.opticalSets.some(row => row.id === identity)
    }
    function opticalCurveState(identity) {
        if (Object.prototype.hasOwnProperty.call(opticalCurveStates, identity))
            return opticalCurveStates[identity]
        return {
            generation: -1, points: [], loading: false, error: ""
        }
    }
    function setOpticalCurveState(identity, state) {
        const next = Object.assign({}, opticalCurveStates)
        next[identity] = state
        opticalCurveStates = next
    }
    function cancelOpticalCurveRequest(identity, clearPoints) {
        const state = opticalCurveState(identity)
        if (state.generation >= 0 && state.loading)
            library.cancelOpticalSetCurve(identity, state.generation)
        const next = Object.assign({}, opticalCurveStates)
        if (clearPoints) {
            delete next[identity]
        } else {
            next[identity] = {
                generation: -1, points: state.points || [], loading: false,
                error: state.loading ? "" : (state.error || "")
            }
        }
        opticalCurveStates = next
    }
    function cancelAllOpticalCurveRequests(clearPoints) {
        for (const identity of Object.keys(opticalCurveStates))
            cancelOpticalCurveRequest(identity, clearPoints === true)
    }
    function startOpticalCurveRequest(identity) {
        cancelOpticalCurveRequest(identity, false)
        const current = opticalCurveState(identity)
        const generation = library.requestOpticalSetCurve(identity)
        setOpticalCurveState(identity, {
            generation: generation, points: current.points || [],
            loading: true, error: ""
        })
    }
    function refreshExpandedOpticalCurves() {
        const validIds = library.opticalSets.map(row => row.id)
        const retained = expandedOpticalSetIds.filter(identity => validIds.indexOf(identity) >= 0)
        cancelAllOpticalCurveRequests(true)
        expandedOpticalSetIds = retained
        opticalCurveStates = ({})
        if (visible) {
            for (const identity of retained) startOpticalCurveRequest(identity)
        }
    }
    function toggleOpticalSet(identity) {
        if (isOpticalSetExpanded(identity)) {
            expandedOpticalSetIds = expandedOpticalSetIds.filter(item => item !== identity)
            cancelOpticalCurveRequest(identity, true)
            return
        }
        expandedOpticalSetIds = expandedOpticalSetIds.concat([identity])
        startOpticalCurveRequest(identity)
    }
    function themeColor(name, fallback) {
        return typeof Theme !== "undefined" ? Theme.snapshot[name] : fallback
    }
    function localPath(url) { return dialog.library.localFilePath(url) }
    function openSingleExport() {
        if (!library.currentId || library.dirty || library.loading) return
        exportMaterialId = library.currentId
        singleExportPicker.open()
    }

    Component {
        id: referenceDialogComponent
        LocalComponents.MaterialReferenceDialog {
            objectName: "materialReferenceDetailsDialog"
            sections: visible ? dialog.library.referenceSections : []
        }
    }
    Component {
        id: setEditorComponent
        LocalComponents.MaterialEditor {
            objectName: "materialEditor"
            library: dialog.library
            onClosed: dialog.restoreEditorFocus()
            onReferenceRequested: dialog.openReferenceDetails()
        }
    }
    FileDialogs.FileDialog {
        id: materialImportPicker
        objectName: "materialImportFilePicker"
        fileMode: FileDialogs.FileDialog.OpenFile
        nameFilters: ["CoatSim material packages (*.cmat *.cmatl)", "JSON files (*.json)"]
        onAccepted: {
            const path = dialog.localPath(selectedFile)
            if (path.length > 0) dialog.library.previewImport(path)
        }
    }
    FileDialogs.FileDialog {
        id: singleExportPicker
        objectName: "singleMaterialExportPicker"
        fileMode: FileDialogs.FileDialog.SaveFile
        defaultSuffix: "cmat"
        nameFilters: ["CoatSim material (*.cmat)"]
        onAccepted: {
            const path = dialog.localPath(selectedFile)
            if (path.length > 0) dialog.library.exportMaterial(path, dialog.exportMaterialId)
        }
    }
    FileDialogs.FileDialog {
        id: libraryExportPicker
        objectName: "materialLibraryExportPicker"
        fileMode: FileDialogs.FileDialog.SaveFile
        defaultSuffix: "cmatl"
        nameFilters: ["CoatSim material library (*.cmatl)"]
        onAccepted: {
            const path = dialog.localPath(selectedFile)
            if (path.length > 0) dialog.library.exportLibrary(path, dialog.pendingExportIds)
        }
    }
    LocalComponents.ThemePageDialog {
        id: exportSelectionDialog
        objectName: "materialLibraryExportSelectionDialog"
        property var included: ({})
        function resetSelection() {
            const next = {}
            for (const row of dialog.library.entries) next[row.id] = true
            included = next
        }
        function setAll(value) {
            const next = {}
            for (const row of dialog.library.entries) next[row.id] = value
            included = next
        }
        function isIncluded(identity) { return included[identity] !== false }
        function selectedIds() {
            const ids = []
            for (const row of dialog.library.entries)
                if (isIncluded(row.id)) ids.push(row.id)
            return ids
        }
        title: "Export material library"
        anchors.centerIn: parent
        width: Math.min(620, dialog.width - 36)
        height: Math.min(580, dialog.height - 36)
        modal: true
        closePolicy: Popup.NoAutoClose
        onOpened: resetSelection()
        contentItem: ColumnLayout {
            spacing: 8
            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeButton { objectName: "materialLibraryExportSelectAll"; text: "Select all"; onClicked: exportSelectionDialog.setAll(true) }
                ThemeControls.ThemeButton { objectName: "materialLibraryExportSelectNone"; text: "Select none"; onClicked: exportSelectionDialog.setAll(false) }
                Item { Layout.fillWidth: true }
                ThemeControls.ThemeLabel {
                    objectName: "materialLibraryExportCount"
                    text: exportSelectionDialog.selectedIds().length + " of " + dialog.library.entries.length + " selected"
                }
            }
            ListView {
                objectName: "materialLibraryExportItems"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: dialog.library.entries
                ScrollBar.vertical: ScrollBar { }
                delegate: CheckBox {
                    required property var modelData
                    width: ListView.view.width
                    text: modelData.name + " · " + (modelData.builtin ? "built-in" : "custom")
                    checked: exportSelectionDialog.isIncluded(modelData.id)
                    onToggled: {
                        const next = Object.assign({}, exportSelectionDialog.included)
                        next[modelData.id] = checked
                        exportSelectionDialog.included = next
                    }
                }
            }
        }
        footer: DialogButtonBox {
            ThemeControls.ThemeButton {
                objectName: "confirmMaterialLibraryExportSelection"
                text: "Choose export file…"
                enabled: exportSelectionDialog.selectedIds().length > 0 && !dialog.library.loading
                onClicked: {
                    dialog.pendingExportIds = exportSelectionDialog.selectedIds()
                    exportSelectionDialog.close()
                    libraryExportPicker.open()
                }
            }
            ThemeControls.ThemeButton { objectName: "cancelMaterialLibraryExportSelectionButton"; text: "Cancel"; onClicked: exportSelectionDialog.close() }
        }
    }
    LocalComponents.ThemePageDialog {
        id: importPreviewDialog
        objectName: "materialLibraryImportPreviewDialog"
        property var decisions: ({})
        function decisionFor(identity) { return decisions[identity] || "skip" }
        function setDecision(identity, value) {
            const next = Object.assign({}, decisions)
            next[identity] = value
            decisions = next
        }
        title: "Review material import"
        anchors.centerIn: parent
        width: Math.min(720, dialog.width - 36)
        height: Math.min(610, dialog.height - 36)
        modal: true
        closePolicy: Popup.NoAutoClose
        onOpened: {
            const next = {}
            for (const row of dialog.library.importPreview)
                if (row.conflict.length > 0) next[row.id] = "skip"
            decisions = next
        }
        onClosed: dialog.library.cancelImportPreview()
        contentItem: ColumnLayout {
            spacing: 8
            ThemeControls.ThemeLabel {
                objectName: "materialImportPreviewSummary"
                Layout.fillWidth: true
                text: dialog.library.importError.length > 0
                      ? dialog.library.importError
                      : dialog.library.importPreview.length + " material record(s) validated. Review each conflict before writing."
                color: dialog.library.importError.length > 0 ? dialog.themeColor("colorError", "#d55b52") : Theme.snapshot.colorText
                wrapMode: Text.Wrap
            }
            ListView {
                objectName: "materialLibraryImportPreviewItems"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: dialog.library.importPreview
                ScrollBar.vertical: ScrollBar { }
                delegate: RowLayout {
                    required property var modelData
                    width: ListView.view.width
                    spacing: 8
                    ColumnLayout {
                        Layout.fillWidth: true
                        ThemeControls.ThemeLabel { text: modelData.name; font.bold: true; elide: Text.ElideRight }
                        ThemeControls.ThemeLabel {
                            text: modelData.id + (modelData.conflict.length > 0
                                  ? " · conflicts with " + modelData.target_id + " (" + modelData.conflict + ")"
                                  : " · new material")
                            muted: true
                            elide: Text.ElideRight
                        }
                    }
                    ThemeControls.ThemeComboBox {
                        visible: modelData.conflict.length > 0
                        Layout.preferredWidth: 160
                        model: [{text: "Skip", value: "skip"},
                                {text: "Save as copy", value: "copy"},
                                {text: "Overwrite…", value: "overwrite"}]
                        textRole: "text"
                        valueRole: "value"
                        currentIndex: Math.max(0, ["skip", "copy", "overwrite"].indexOf(
                            importPreviewDialog.decisionFor(modelData.id)))
                        onActivated: importPreviewDialog.setDecision(modelData.id, currentValue)
                    }
                }
            }
        }
        footer: DialogButtonBox {
            ThemeControls.ThemeButton {
                objectName: "applyMaterialImportButton"
                text: dialog.library.loading ? "Importing…" : "Apply import"
                enabled: dialog.library.importPreview.length > 0 && !dialog.library.loading
                         && dialog.library.importError.length === 0
                onClicked: dialog.library.applyImport(importPreviewDialog.decisions)
            }
            ThemeControls.ThemeButton {
                objectName: "cancelMaterialImportPreviewButton"
                text: "Cancel"
                enabled: !dialog.library.loading || dialog.library.importPreviewBusy
                onClicked: importPreviewDialog.close()
            }
        }
    }

    Connections {
        target: dialog.library
        function onDiscardRequested() { discard.open() }
        function onDeleteRequested() { deleteDialog.open() }
        function onDraftChanged() {
            const materialId = String(dialog.library.currentId || "")
            if (materialId !== dialog.scrollMaterialId) {
                dialog.scrollMaterialId = materialId
                editorScroll.contentItem.contentY = 0
            }
        }
        function onImportPreviewReady() { importPreviewDialog.open() }
        function onImportApplyFinished(success) { if (success) importPreviewDialog.close() }
        function onOpticalCurveReady(identity, generation, points, error) {
            const state = dialog.opticalCurveState(identity)
            if (!dialog.isOpticalSetExpanded(identity) || generation !== state.generation
                    || !dialog.hasOpticalSet(identity)) return
            // Python object signals arrive as PyObjectWrapper in QML; read the
            // now-warm QVariantList cache after the generation check.
            dialog.setOpticalCurveState(identity, {
                generation: generation,
                points: dialog.library.opticalSetCurve(identity),
                loading: false, error: error || ""
            })
        }
    }
    LocalComponents.ThemePageDialog {
        id: restoreBuiltinDialog
        objectName: "restoreBuiltinDialog"
        title: "Restore built-in original?"
        anchors.centerIn: parent
        width: Math.min(470, dialog.width - 36)
        height: 220
        modal: true
        closePolicy: Popup.NoAutoClose
        contentItem: ThemeControls.ThemeLabel {
            text: "This removes the current override for this built-in material and restores its packaged original in the library. Saved source and film snapshots keep their existing material values."
            wrapMode: Text.Wrap
        }
        footer: DialogButtonBox {
            ThemeControls.ThemeButton { objectName: "confirmRestoreBuiltinButton"; text: "Restore original"; enabled: !dialog.library.loading && dialog.library.canRestoreBuiltin; onClicked: { restoreBuiltinDialog.close(); dialog.library.restoreBuiltinInBackground() } }
            ThemeControls.ThemeButton { text: "Cancel"; onClicked: restoreBuiltinDialog.close() }
        }
    }
    LocalComponents.ThemePageDialog {
        id: discard
        title: "材料修改尚未保存"
        anchors.centerIn: parent
        width: Math.min(450, dialog.width - 36)
        height: 205
        modal: true
        closePolicy: Popup.NoAutoClose
        contentItem: ThemeControls.ThemeLabel { text: "切换或新建会替换当前材料内容。取消将保留全部输入。"; wrapMode: Text.Wrap }
        footer: DialogButtonBox {
            ThemeControls.ThemeButton { text: "保存并继续"; enabled: dialog.library.valid; onClicked: { if (dialog.library.saveAndDiscardInBackground()) discard.close() } }
            ThemeControls.ThemeButton { text: "放弃修改"; onClicked: { discard.close(); dialog.library.confirmDiscard(true) } }
            ThemeControls.ThemeButton { text: "取消"; onClicked: { discard.close(); dialog.library.confirmDiscard(false) } }
        }
    }
    LocalComponents.ThemePageDialog {
        id: deleteDialog
        objectName: "materialLibraryDeleteDialog"
        title: "从本地库删除此材料？"
        anchors.centerIn: parent
        width: Math.min(450, dialog.width - 36)
        height: 205
        modal: true
        standardButtons: Dialog.Yes | Dialog.Cancel
        contentItem: ThemeControls.ThemeLabel { text: "将删除当前材料的 JSON 文件。当前未保存的编辑内容会保留，已有源中的完整快照不会受影响。"; wrapMode: Text.Wrap }
        onAccepted: dialog.library.deleteInBackground()
        onRejected: dialog.library.confirmDelete(false)
    }
    contentItem: ColumnLayout {
        spacing: 10
        LocalComponents.ResponsiveOperationBar {
            id: materialLibraryActionToolbar
            objectName: "materialLibraryActionToolbar"
            Accessible.name: "操作栏"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            width: parent.width
            height: childrenRect.height
            spacing: 6
            LocalComponents.ProjectModuleSelector {
                visible: dialog.projectResources !== null && !dialog.embeddedInAssetLibrary
                navigation: dialog.projectNavigation
                currentModuleKey: "materials"
                width: visible ? implicitWidth : 0
            }
            LocalComponents.ProjectResourceControls {
                projectResources: dialog.projectResources
                informationProvider: dialog.projectResources !== null ? dialog.projectResources : dialog.library.informationController
                width: visible ? implicitWidth : 0
                extraTransferActions: [
                    {key: "material-file-import", text: "导入材料文件…", enabled: !dialog.library.loading},
                    {key: "material-library-export", text: "导出材料库文件…", enabled: dialog.library.entries.length > 0 && !dialog.library.loading},
                    {key: "material-file-export", text: "导出当前材料文件…", enabled: !!dialog.library.currentId && !dialog.library.dirty && !dialog.library.loading}
                ]
                onExtraTransferActionRequested: key => {
                    if (key === "material-file-import") materialImportPicker.open()
                    else if (key === "material-library-export") exportSelectionDialog.open()
                    else if (key === "material-file-export") dialog.openSingleExport()
                }
            }
            ThemeControls.ThemeButton {
                objectName: "newMaterialButton"
                visible: dialog.projectResources === null
                text: "＋ 新建材料"
                width: Math.max(140, implicitWidth)
                enabled: !dialog.library.loading
                onClicked: dialog.library.newMaterial()
            }
            ThemeControls.ThemeButton {
                objectName: "importMaterialButton"
                visible: dialog.projectResources === null
                text: "导入"
                width: Math.max(140, implicitWidth)
                enabled: !dialog.library.loading
                onClicked: materialImportPicker.open()
            }
            ThemeControls.ThemeButton {
                objectName: "saveMaterialButton"
                visible: dialog.projectResources === null
                text: "保存材料"
                width: Math.max(140, implicitWidth)
                enabled: dialog.library.valid && !dialog.library.readOnly && !dialog.library.loading
                onClicked: dialog.library.saveInBackground()
            }
            ThemeControls.ThemeButton {
                objectName: "exportLibraryButton"
                visible: dialog.projectResources === null
                text: "导出材料库"
                width: Math.max(140, implicitWidth)
                enabled: dialog.library.entries.length > 0 && !dialog.library.loading
                onClicked: exportSelectionDialog.open()
            }
            ThemeControls.ThemeButton {
                objectName: "exportSingleMaterialButton"
                visible: dialog.projectResources === null
                text: "导出所选"
                width: Math.max(140, implicitWidth)
                enabled: !!dialog.library.currentId && !dialog.library.dirty && !dialog.library.loading
                onClicked: dialog.openSingleExport()
            }
        }
        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 14
            ColumnLayout {
                visible: !dialog.embeddedInAssetLibrary
                Layout.preferredWidth: dialog.width >= 900 ? 260 : 215
                Layout.minimumWidth: 190
                Layout.maximumWidth: dialog.width >= 900 ? 260 : 215
                Layout.fillHeight: true
                ThemeControls.ThemeField {
                    objectName: "materialSearchField"
                    Layout.fillWidth: true
                    placeholderText: "搜索名称 / 化学式 / 别名"
                    text: dialog.library.searchText
                    selectByMouse: true
                    onTextEdited: dialog.library.setSearchText(text)
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 6
                    ThemeControls.ThemeComboBox {
                        objectName: "materialRoleFilter"
                        Layout.fillWidth: true
                        model: [{text: "膜料", value: "film"}, {text: "基片 / 光学介质", value: "substrate"}, {text: "其他", value: "other"}]
                        textRole: "text"; valueRole: "value"
                        currentIndex: Math.max(0, ["film", "substrate", "other"].indexOf(dialog.library.roleFilter))
                        onActivated: dialog.library.setRoleFilter(currentValue)
                    }
                    ThemeControls.ThemeButton {
                        objectName: "refreshMaterialLibraryButton"
                        text: "刷新"
                        enabled: !dialog.library.loading
                        onClicked: dialog.library.reload()
                    }
                }
                ThemeControls.ThemeLabel { Layout.fillWidth: true; text: "显示 " + dialog.library.filteredEntries.length + " / " + dialog.library.entries.length + " 条"; opacity: 0.7 }
                RowLayout {
                    Layout.fillWidth: true
                    Item { Layout.fillWidth: true }
                    ThemeControls.ThemeButton { objectName: "restoreBuiltinButton"; text: "Restore built-in original"; Layout.fillWidth: true; visible: dialog.library.builtin && dialog.library.builtinOverridden; enabled: dialog.library.canRestoreBuiltin; onClicked: restoreBuiltinDialog.open() }
                }
                ListView {
                    id: materialList
                    objectName: "materialList"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    spacing: 4
                    model: dialog.library.filteredEntries
                    ScrollBar.vertical: ScrollBar { }
                    delegate: ItemDelegate {
                        id: materialEntryDelegate
                        objectName: "materialEntryDelegate"
                        required property var modelData
                        width: ListView.view.width
                        contentItem: Column {
                            spacing: 3
                            ThemeControls.ThemeLabel { width: parent.width; text: modelData.name; wrapMode: Text.Wrap; font.bold: modelData.key === dialog.library.currentKey }
                ThemeControls.ThemeLabel { objectName: "materialEntryRoleLabel"; width: parent.width; text: materialEntryDelegate.modelData.globalReadOnly ? "全局材料 · 只读来源" : (materialEntryDelegate.modelData.roles && materialEntryDelegate.modelData.roles.indexOf("medium") >= 0 ? "内置 · 光学介质" : (materialEntryDelegate.modelData.opticalOnly ? "\u7528\u6237 \u00b7 \u4ec5\u5149\u5b66\u8d44\u6599\uff08\u5de5\u827a\u6a21\u578b\u672a\u63d0\u4f9b\uff09" : (materialEntryDelegate.modelData.builtin ? "内置 · " : "用户 · ") + ({film:"膜料", substrate:"基片", other:"其他"}[materialEntryDelegate.modelData.category]))); muted: true; wrapMode: Text.Wrap; opacity: 0.8 }

                        }
                        highlighted: modelData.key === dialog.library.currentKey
                        onClicked: dialog.library.selectMaterial(modelData.key)
                    }
                    ThemeControls.ThemeLabel { anchors.fill: parent; visible: materialList.count === 0; text: dialog.library.entries.length === 0 ? "材料库为空\n新建并保存第一种材料" : "没有匹配材料\n请调整搜索或用途角色"; wrapMode: Text.Wrap; verticalAlignment: Text.AlignVCenter }
                }
                ThemeControls.ThemeButton { text: "删除所选材料"; enabled: dialog.library.canDelete; Layout.fillWidth: true; onClicked: dialog.library.requestDelete() }
            }
            ScrollView {
                id: editorScroll
                objectName: "materialResearchScroll"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: availableWidth
                ScrollBar.vertical.policy: ScrollBar.AlwaysOn
                Column {
                    width: editorScroll.availableWidth - 12
                    spacing: 10
                    ThemeControls.ThemeLabel {
                        objectName: "globalMaterialCopyOnWriteNotice"
                        visible: dialog.library.globalSourceSelected === true
                        width: parent.width
                        text: "当前来自全局材料库。修改和保存只写入当前工程，不会修改全局原件。"
                        wrapMode: Text.Wrap
                    }
                    ThemeControls.ThemeLabel { objectName: "materialOpticalOnlyNotice"; visible: dialog.library.opticalOnly; width: parent.width; text: "\u4ec5\u6709\u5149\u5b66\u6570\u636e\uff1b\u5de5\u827a\u6a21\u578b\u672a\u63d0\u4f9b\u3002\u53ef\u7f16\u8f91\u548c\u4f7f\u7528\u5149\u5b66\u5e38\u6570\uff0c\u4e0d\u80fd\u7ed1\u5b9a\u4e3a\u84b8\u53d1\u6e90\uff1b\u4fdd\u5b58\u65f6\u963b\u503c\u6a21\u578b\u4ecd\u4e3a null\u3002"; color: dialog.themeColor("colorWarning", "#b07730"); wrapMode: Text.Wrap }
                    ThemeControls.ThemeLabel { visible: false; objectName: "materialReadinessLabel"; width: parent.width; text: dialog.library.readiness; wrapMode: Text.Wrap; color: dialog.library.sourceBindable ? Theme.snapshot.colorText : dialog.themeColor("colorWarning", "#b07730") }
                    ThemeControls.ThemeLabel { objectName: "materialCoverageLabel"; visible: false; width: parent.width; text: "资料覆盖：" + dialog.library.propertyCoverage; wrapMode: Text.Wrap; opacity: 0.8 }
                    RowLayout {
                        width: parent.width
                        spacing: 8
                        LocalComponents.SourceValueField {
                            enabled: !dialog.library.readOnly && !dialog.library.loading
                            Layout.fillWidth: true
                            Layout.minimumWidth: 160
                            visible: false
                            label: "材料名称"
                            fieldName: "materialNameField"
                            value: dialog.field("name")
                            numeric: false
                            onEdited: value => dialog.library.setField("name", value)
                        }
                        ThemeControls.ThemeComboBox {
                            objectName: "materialCategoryEditor"
                            visible: !dialog.library.readOnly && dialog.library.roles.length === 0
                            enabled: !dialog.library.loading
                            Layout.preferredWidth: 118
                            Layout.minimumWidth: 94
                            model: ["膜料", "基片", "其他"]
                            currentIndex: ["film", "substrate", "other"].indexOf(dialog.library.category)
                            onActivated: dialog.library.setCategory(["film", "substrate", "other"][currentIndex])
                        }
                        ThemeControls.ThemeButton {
                            objectName: "materialReferenceDetailsButton"
                            text: "材料参考详情"
                            visible: dialog.library.hasReferenceData
                            enabled: !dialog.library.loading
                            Layout.preferredWidth: 128
                            Layout.minimumWidth: 110
                            onClicked: dialog.openReferenceDetails()
                        }
                    }
                    LocalComponents.SourceValueField { visible: false; enabled: !dialog.library.readOnly && !dialog.library.loading; width: parent.width; label: "说明"; value: dialog.field("description"); numeric: false; onEdited: value => dialog.library.setField("description", value) }
                    Column {
                        visible: false
                        width: parent.width
                        spacing: 4
                        ThemeControls.ThemeLabel { text: "材料备注" }
                        TextArea {
                            objectName: "materialNotesEditor"
                            visible: !dialog.library.readOnly
                            width: parent.width
                            implicitHeight: 86
                            text: dialog.field("notes")
                            wrapMode: TextEdit.Wrap
                            selectByMouse: true
                            onTextChanged: if (activeFocus && text !== dialog.field("notes")) dialog.library.setField("notes", text)
                        }
                        ThemeControls.ThemeLabel {
                            visible: dialog.library.readOnly
                            width: parent.width
                            text: dialog.field("notes")
                            textFormat: Text.PlainText
                            wrapMode: Text.Wrap
                        }
                    }
                    ThemeControls.ThemeLabel { visible: dialog.library.readOnly; width: parent.width; text: "\u53ea\u8bfb\u7814\u7a76\u8bb0\u5f55\u3002\u590d\u5236\u4e3a\u7528\u6237\u6750\u6599\u540e\u53ef\u7f16\u8f91\u6a21\u578b\u53c2\u6570\uff1b\u6765\u6e90\u8d44\u6599\u4fdd\u6301\u53ea\u8bfb\u3002"; wrapMode: Text.Wrap; color: dialog.themeColor("colorWarning", "#b07730") }
                    ThemeControls.ThemeLabel { visible: dialog.library.loading; text: "正在后台载入材料…" }
                        TabBar {
                            id: contentTabs
                            objectName: "materialContentTabs"
                            currentIndex: 0
                            width: parent.width
                        TabButton { objectName: "opticalConstantsTab"; text: "\u5149\u5b66\u5e38\u6570" }
                        TabButton { objectName: "resistanceModelTab"; text: "\u963b\u84b8\u6a21\u578b"; enabled: !dialog.library.opticalOnly }
                        TabButton { objectName: "electronBeamModelTab"; text: "\u7535\u5b50\u675f\u6a21\u578b"; enabled: !dialog.library.opticalOnly }
                    }
                    Column {
                        objectName: "materialOpticalDatasetsPage"
                        width: parent.width
                        spacing: 10
                        visible: contentTabs.currentIndex === 0
                        ThemeControls.ThemeLabel { width: parent.width; wrapMode: Text.Wrap; text: "多组独立数据可描述不同样品、工艺或温度。光学数据仅作材料记录，不自动用于沉积物理计算。" }
                        ThemeControls.ThemeButton { objectName: "addOpticalSetButton"; text: "＋ 光学常数集"; enabled: !dialog.library.readOnly && !dialog.library.loading; onClicked: dialog.newOpticalSet() }
                        ThemeControls.ThemeLabel { width: parent.width; visible: dialog.library.opticalSets.length === 0; text: "尚无光学常数集。可导入离散 n/k 文件或输入 Cauchy、Sellmeier 模型；未知 k 不会填零。"; wrapMode: Text.Wrap }
                        Repeater {
                            id: opticalSetsRepeater
                            objectName: "opticalSetsRepeater"
                            model: dialog.library.opticalSets
                            ThemeControls.ThemeSurface {
                                id: opticalCard
                                objectName: "opticalDatasetCard_" + modelData.id
                                required property var modelData
                                 property bool expanded: dialog.isOpticalSetExpanded(modelData.id)
                                 property var curveData: dialog.opticalCurveState(modelData.id)
                                 property var curvePoints: expanded ? curveData.points : []
                                property var processRows: modelData.process_parameters || []
                                width: parent.width
                                Layout.fillWidth: true
                                contentSpacing: 0
                                padding: Theme.snapshot.spaceSm
                                Column {
                                    Layout.fillWidth: true
                                    width: parent.width
                                    spacing: 5
                                    ThemeControls.ThemeButton {
                                        id: disclosureHeader
                                        objectName: "opticalSetDisclosureHeader"
                                        width: parent.width
                                        implicitHeight: disclosureContent.implicitHeight + 10
                                        onClicked: dialog.toggleOpticalSet(modelData.id)
                                        HoverHandler { cursorShape: Qt.PointingHandCursor }
                                        Accessible.name: modelData.name + (opticalCard.expanded ? "，收起" : "，展开")
                                        Accessible.description: modelData.summary
                                        background: Rectangle {
                                            color: disclosureHeader.down
                                                   ? Qt.rgba(Theme.snapshot.colorAccentPressed.r,
                                                             Theme.snapshot.colorAccentPressed.g,
                                                             Theme.snapshot.colorAccentPressed.b,
                                                             Theme.snapshot.opacityPressed)
                                                   : disclosureHeader.hovered
                                                     ? Qt.rgba(Theme.snapshot.colorAccentHover.r,
                                                               Theme.snapshot.colorAccentHover.g,
                                                               Theme.snapshot.colorAccentHover.b,
                                                               Theme.snapshot.opacityHover)
                                                     : "transparent"
                                            border.color: disclosureHeader.activeFocus
                                                          ? Theme.snapshot.colorFocusRing
                                                          : disclosureHeader.hovered
                                                            ? Theme.snapshot.colorBorderStrong
                                                            : "transparent"
                                            border.width: disclosureHeader.activeFocus
                                                          ? Theme.snapshot.focusRingWidth
                                                          : disclosureHeader.hovered ? Theme.snapshot.borderWidth : 0
                                            radius: Theme.snapshot.radiusSm
                                        }
                    Connections {
                        target: dialog.library
                        function onChanged() {
                            if (dialog.library.opticalOnly && contentTabs.currentIndex > 0)
                                contentTabs.currentIndex = 0
                        }
                    }
                                        contentItem: RowLayout {
                                            id: disclosureContent
                                            spacing: 10
                                            ColumnLayout {
                                                Layout.fillWidth: true
                                                spacing: 3
                                                ThemeControls.ThemeLabel { Layout.fillWidth: true; text: modelData.name; font.bold: true; textFormat: Text.PlainText; elide: Text.ElideRight }
                                                ThemeControls.ThemeLabel { Layout.fillWidth: true; text: modelData.summary; textFormat: Text.PlainText; wrapMode: Text.Wrap }
                                            }
                                            Canvas {
                                                id: opticalSetDisclosureArrow
                                                objectName: "opticalSetDisclosureArrow"
                                                width: 22
                                                height: 22
                                                property bool expanded: opticalCard.expanded
                                                onExpandedChanged: requestPaint()
                                                Connections {
                                                    target: typeof Theme !== "undefined" ? Theme : null
                                                    function onSnapshotChanged() { opticalSetDisclosureArrow.requestPaint() }
                                                }
                                                onPaint: {
                                                    let ctx = getContext("2d")
                                                    ctx.clearRect(0, 0, width, height)
                                                    ctx.strokeStyle = Theme.snapshot.colorText.toString()
                                                    ctx.lineWidth = 2
                                                    ctx.beginPath()
                                                    if (expanded) { ctx.moveTo(5, 8); ctx.lineTo(11, 14); ctx.lineTo(17, 8) }
                                                    else { ctx.moveTo(8, 5); ctx.lineTo(14, 11); ctx.lineTo(8, 17) }
                                                    ctx.stroke()
                                                }
                                            }
                                        }
                                    }
                                    TapHandler {
                                        objectName: "opticalSetCardTap"
                                        enabled: false
                                        gesturePolicy: TapHandler.WithinBounds
                                        onTapped: dialog.toggleOpticalSet(modelData.id)
                                    }
                                    RowLayout {
                                        width: parent.width
                                        visible: false
                                        ToolButton {
                                            objectName: "opticalSetDisclosure"
                                            visible: false
                                            onClicked: dialog.toggleOpticalSet(modelData.id)
                                        }
                                        ThemeControls.ThemeLabel { Layout.fillWidth: true; text: modelData.name; font.bold: true; textFormat: Text.PlainText; elide: Text.ElideRight }
                                        ThemeControls.ThemeLabel { objectName: "legacyOpticalSetDisclosureArrow"; text: opticalCard.expanded ? "▾" : "▸"; heading: true }
                                    }
                                    ThemeControls.ThemeLabel {
                                        objectName: "opticalSetSummary"
                                        visible: false
                                        width: parent.width
                                        text: modelData.summary
                                        textFormat: Text.PlainText
                                        wrapMode: Text.Wrap
                                    }
                                    ThemeControls.ThemeLabel {
                                        objectName: "opticalSetCollapsedNotes"
                                        visible: false
                                        width: parent.width
                                        text: modelData.notes || "（无备注）"
                                        textFormat: Text.PlainText
                                        wrapMode: Text.Wrap
                                        maximumLineCount: 3
                                        elide: Text.ElideRight
                                    }
                                    Loader {
                                        objectName: "opticalSetExpandedDetailsLoader"
                                        width: parent.width
                                        // The details component is now lazy, so make its
                                        // natural height explicit in this Column. This
                                        // keeps the loaded content visible and lets the
                                        // card grow when a dataset is expanded.
                                        height: implicitHeight
                                        active: opticalCard.expanded
                                        sourceComponent: Component {
                                            Column {
                                                objectName: "opticalSetExpandedDetails"
                                                spacing: 7
                                                ThemeControls.ThemeLabel { text: modelData.kindLabel; font.bold: true; wrapMode: Text.Wrap }
                                                ThemeControls.ThemeLabel { objectName: "opticalSetNotes"; text: modelData.notes || "\u65e0\u7b80\u77ed\u4ecb\u7ecd"; maximumLineCount: 3; elide: Text.ElideRight; textFormat: Text.PlainText; wrapMode: Text.Wrap; width: parent.width }
                                                ThemeControls.ThemeLabel { objectName: "opticalSetProcessHeading"; text: "\u6837\u54c1\u4e0e\u5de5\u827a\u6761\u4ef6"; font.bold: true }
                                                ThemeControls.ThemeLabel {
                                                    objectName: "opticalSetNoProcessParameters"
                                                    visible: !modelData.process_parameters || modelData.process_parameters.length === 0
                                                    text: "\u6765\u6e90\u672a\u63d0\u4f9b\u53ef\u8bc6\u522b\u7684\u7ed3\u6784\u5316\u5de5\u827a\u6570\u636e\uff1b\u539f\u6587\u53ef\u5728\u53c2\u8003\u8be6\u60c5\u67e5\u770b\u3002"
                                                    wrapMode: Text.Wrap
                                                    width: parent.width
                                                }
                                                GridLayout {
                                                    id: opticalSetProcessGrid
                                                    objectName: "opticalSetProcessGrid"
                                                    property bool splitColumns: width >= 760 && opticalCard.processRows.length > 1
                                                    visible: opticalCard.processRows.length > 0
                                                    width: parent.width
                                                    columns: splitColumns ? 2 : 1
                                                    columnSpacing: 16
                                                    rowSpacing: 2
                                                    ColumnLayout {
                                                        objectName: "opticalSetProcessGroup_0"
                                                        Layout.fillWidth: true
                                                        spacing: 2
                                                        RowLayout {
                                                            objectName: "opticalSetProcessTableHeader"
                                                            Layout.fillWidth: true
                                                            spacing: 4
                                                            ThemeControls.ThemeLabel { objectName: "opticalSetProcessHeaderName"; Layout.fillWidth: true; Layout.minimumWidth: 90; Layout.preferredWidth: parent.width * 0.54; text: "\u53c2\u6570"; font.bold: true; elide: Text.ElideRight }
                                                            ThemeControls.ThemeLabel { objectName: "opticalSetProcessHeaderValue"; Layout.fillWidth: true; Layout.minimumWidth: 90; Layout.preferredWidth: parent.width * 0.46; text: "\u503c / \u5355\u4f4d"; font.bold: true; elide: Text.ElideRight }
                                                        }
                                                        Repeater {
                                                            objectName: "opticalSetProcessParameterRows"
                                                            model: opticalSetProcessGrid.splitColumns ? Math.ceil(opticalCard.processRows.length / 2) : opticalCard.processRows.length
                                                            delegate: RowLayout {
                                                                required property int index
                                                                readonly property int sourceIndex: opticalSetProcessGrid.splitColumns ? index * 2 : index
                                                                readonly property var rowData: opticalCard.processRows[sourceIndex]
                                                                objectName: "opticalSetProcessRow_" + sourceIndex
                                                                Layout.fillWidth: true
                                                                spacing: 4
                                                                ThemeControls.ThemeLabel { objectName: "opticalSetProcessCellName_" + parent.sourceIndex; Layout.fillWidth: true; Layout.minimumWidth: 90; Layout.preferredWidth: parent.width * 0.54; text: parent.rowData.name; textFormat: Text.PlainText; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter }
                                                                ThemeControls.ThemeLabel { objectName: "opticalSetProcessCellValue_" + parent.sourceIndex; Layout.fillWidth: true; Layout.minimumWidth: 90; Layout.preferredWidth: parent.width * 0.46; text: dialog.processValueText(parent.rowData); textFormat: Text.PlainText; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter }
                                                            }
                                                        }
                                                    }
                                                    ColumnLayout {
                                                        objectName: "opticalSetProcessGroup_1"
                                                        visible: opticalSetProcessGrid.splitColumns
                                                        Layout.fillWidth: true
                                                        spacing: 2
                                                        RowLayout {
                                                            objectName: "opticalSetProcessTableHeader_2"
                                                            Layout.fillWidth: true
                                                            spacing: 4
                                                            ThemeControls.ThemeLabel { objectName: "opticalSetProcessHeaderName_2"; Layout.fillWidth: true; Layout.minimumWidth: 90; Layout.preferredWidth: parent.width * 0.54; text: "\u53c2\u6570"; font.bold: true; elide: Text.ElideRight }
                                                            ThemeControls.ThemeLabel { objectName: "opticalSetProcessHeaderValue_2"; Layout.fillWidth: true; Layout.minimumWidth: 90; Layout.preferredWidth: parent.width * 0.46; text: "\u503c / \u5355\u4f4d"; font.bold: true; elide: Text.ElideRight }
                                                        }
                                                        Repeater {
                                                            objectName: "opticalSetProcessParameterRows_2"
                                                            model: Math.floor(opticalCard.processRows.length / 2)
                                                            delegate: RowLayout {
                                                                required property int index
                                                                readonly property int sourceIndex: index * 2 + 1
                                                                readonly property var rowData: opticalCard.processRows[sourceIndex]
                                                                objectName: "opticalSetProcessRow_" + sourceIndex
                                                                Layout.fillWidth: true
                                                                spacing: 4
                                                                ThemeControls.ThemeLabel { objectName: "opticalSetProcessCellName_" + parent.sourceIndex; Layout.fillWidth: true; Layout.minimumWidth: 90; Layout.preferredWidth: parent.width * 0.54; text: parent.rowData.name; textFormat: Text.PlainText; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter }
                                                                ThemeControls.ThemeLabel { objectName: "opticalSetProcessCellValue_" + parent.sourceIndex; Layout.fillWidth: true; Layout.minimumWidth: 90; Layout.preferredWidth: parent.width * 0.46; text: dialog.processValueText(parent.rowData); textFormat: Text.PlainText; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter }
                                                            }
                                                        }
                                                    }
                                                }
                                                ThemeControls.ThemeLabel { text: "n / k \u66f2\u7ebf"; font.bold: true }
                                                ThemeControls.ThemeLabel {
                                                    objectName: "opticalSetCurveStatus"
                                                     visible: opticalCard.expanded &&
                                                              (opticalCard.curveData.loading || opticalCard.curveData.error.length > 0)
                                                     text: opticalCard.curveData.loading ? "\u6b63\u5728\u751f\u6210 n/k \u66f2\u7ebf" : "\u66f2\u7ebf\u8bfb\u53d6\u5931\u8d25\uff1a" + opticalCard.curveData.error
                                                     color: opticalCard.curveData.loading ? Theme.snapshot.colorTextMuted : Theme.snapshot.colorError
                                                    wrapMode: Text.Wrap
                                                    width: parent.width
                                                }
                                                Canvas {
                                                    id: opticalSetCurve
                                                    objectName: "opticalSetCurve"
                                                    width: parent.width
                                                    height: 180
                                                    property var points: opticalCard.curvePoints || []
                                                    onPointsChanged: requestPaint()
                                                    Connections {
                                                        target: typeof Theme !== "undefined" ? Theme : null
                                                        function onSnapshotChanged() { opticalSetCurve.requestPaint() }
                                                    }
                                                    onPaint: {
                                                        let ctx = getContext("2d")
                                                        ctx.clearRect(0, 0, width, height)
                                                        let data = []
                                                        let rawPoints = points || []
                                                        for (let i = 0; i < rawPoints.length; ++i) {
                                                            let point = rawPoints[i]
                                                            if (point && Number.isFinite(point.wavelength)) data.push(point)
                                                        }
                                                        if (data.length < 2) {
                                                             const waiting = opticalCard.expanded &&
                                                                             (opticalCard.curveData.loading || opticalCard.curveData.error.length > 0)
                                                            if (!waiting) {
                                                                ctx.fillStyle = dialog.themeColor("colorTextMuted", "#7b8490"); ctx.font = "12px sans-serif"
                                                                ctx.fillText("\u5f53\u524d\u6ce2\u6bb5\u65e0\u53ef\u7ed8\u5236\u66f2\u7ebf", 12, 24)
                                                            }
                                                            return
                                                        }
                                                        let left = 40, right = width - 42, top = 12, bottom = height - 27
                                                        ctx.strokeStyle = dialog.themeColor("colorBorder", "#8b929a"); ctx.lineWidth = 1
                                                        ctx.beginPath(); ctx.moveTo(left, top); ctx.lineTo(left, bottom); ctx.lineTo(right, bottom); ctx.stroke()
                                                        let x0 = data[0].wavelength, x1 = data[data.length - 1].wavelength
                                                        let nValues = data.map(p => p.n).filter(v => v !== null && Number.isFinite(v))
                                                        let kValues = data.map(p => p.k).filter(v => v !== null && Number.isFinite(v))
                                                        function draw(key, values, color) {
                                                            if (values.length < 2) return
                                                            let lo = Math.min.apply(null, values), hi = Math.max.apply(null, values)
                                                            if (Math.abs(hi - lo) < 1e-15) { lo -= 0.5; hi += 0.5 }
                                                            ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath()
                                                            let started = false
                                                            for (let p of data) {
                                                                let v = p[key]
                                                                if (v === null || !Number.isFinite(v)) { started = false; continue }
                                                                let x = left + (p.wavelength - x0) / Math.max(1e-15, x1 - x0) * (right - left)
                                                                let y = top + (hi - v) / (hi - lo) * (bottom - top)
                                                                if (!started) { ctx.moveTo(x, y); started = true } else ctx.lineTo(x, y)
                                                            }
                                                            ctx.stroke()
                                                        }
                                                        draw("n", nValues, "#3979b9"); draw("k", kValues, "#d36b4d")
                                                        ctx.font = "12px sans-serif"
                                                        ctx.fillStyle = "#3979b9"; ctx.fillText("n", left + 5, top + 12)
                                                        ctx.fillStyle = "#d36b4d"; ctx.fillText(kValues.length ? "k" : "k \u672a\u77e5", left + 28, top + 12)
                                                        ctx.fillStyle = dialog.themeColor("colorTextMuted", "#7b8490")
                                                        ctx.fillText(NumericFormat.format(Number(x0), Qt.locale(), 3) + " " + modelData.range.split(" ").pop(), left, height - 5)
                                                        ctx.fillText(NumericFormat.format(Number(x1), Qt.locale(), 3), Math.max(left, right - 44), height - 5)
                                                    }
                                                }
                                                ThemeControls.ThemeLabel { width: parent.width; text: "n\u3001k \u5404\u6309\u81ea\u8eab\u6570\u503c\u8303\u56f4\u7f29\u653e\uff1b\u4ec5\u5728\u5b58\u5728 k \u6570\u636e\u65f6\u7ed8\u5236 k\u3002"; wrapMode: Text.Wrap; opacity: 0.75 }
                                                RowLayout {
                                                    visible: !dialog.library.readOnly
                                                ThemeControls.ThemeButton { id: editOpticalSetButton; objectName: "editOpticalSetButton"; text: "\u7f16\u8f91\u5b8c\u6574\u6570\u636e\u96c6"; enabled: !dialog.library.loading; onClicked: dialog.openSetEditorFromControl(modelData.id, editOpticalSetButton) }
                                                    ThemeControls.ThemeButton { text: "\u79fb\u51fa\u8349\u7a3f"; enabled: !dialog.library.loading; onClicked: dialog.library.removeOpticalSet(modelData.id) }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    Column {
                        objectName: "materialModelParametersPage"
                        width: parent.width
                        spacing: 10
                        visible: contentTabs.currentIndex > 0
                        ThemeControls.ThemeLabel { visible: !dialog.library.sourceBindable && !dialog.library.opticalOnly; width: parent.width; text: "此材料角色不能绑定到蒸发源。以下通用字段只是兼容记录格式，不代表该材料可用的蒸发模型。"; wrapMode: Text.Wrap; color: dialog.themeColor("colorWarning", "#b07730") }
                        ThemeControls.ThemeLabel { visible: !dialog.library.opticalOnly; width: parent.width; text: "默认系数是可编辑的项目约定，不能仅凭材料名称推算物理常数。标定资料不足时请保留“未验证”。"; wrapMode: Text.Wrap }
                        GridLayout {
                            objectName: "materialModelParameterGrid"
                            width: parent.width
                            columns: Math.max(1, Math.min(3, Math.floor((width + 12) / 330)))
                            columnSpacing: 12
                            rowSpacing: 6
                            Repeater {
                                model: [
                                    { key: "source_exponent", label: "\u53c2\u8003\u901f\u7387\u4e0b\u6e90\u89d2\u6307\u6570 n" },
                                    { key: "reference_rate_nm_s", label: "\u53c2\u8003\u6c89\u79ef\u901f\u7387 / \u00c5/s" },
                                    { key: "rate_exponent_per_decade", label: "\u89d2\u6307\u6570\u968f\u901f\u7387\u7684\u53d8\u5316 / \u6bcf\u5341\u500d\u901f\u7387" },
                                    { key: "valid_rate_min_nm_s", label: "\u6807\u5b9a\u6709\u6548\u901f\u7387\u4e0b\u9650 / \u00c5/s\uff08\u53ef\u7559\u7a7a\uff09" },
                                    { key: "valid_rate_max_nm_s", label: "\u6807\u5b9a\u6709\u6548\u901f\u7387\u4e0a\u9650 / \u00c5/s\uff08\u53ef\u7559\u7a7a\uff09" }
                                ]
                                LocalComponents.SourceValueField {
                                    visible: !dialog.library.opticalOnly
                                    enabled: !dialog.library.readOnly && !dialog.library.loading
                                    required property var modelData
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 240
                                    label: modelData.label
                                    fieldName: "material_" + modelData.key
                                    value: dialog.modelField(modelData.key)
                                    onEdited: value => dialog.editModel(modelData.key, value)
                                }
                            }
                            Repeater {
                                model: contentTabs.currentIndex === 1 ? [
                                    {key: "active_fraction", label: "\u6709\u6548\u53d1\u5c04\u5c3a\u5bf8\u6bd4\u4f8b\uff080\u20131\uff09"},
                                    {key: "rate_active_fraction_per_decade", label: "\u6709\u6548\u5c3a\u5bf8\u6bd4\u4f8b\u53d8\u5316 / \u6bcf\u5341\u500d\u901f\u7387"},
                                    {key: "center_weight", label: "\u4e2d\u5fc3\u589e\u5f3a\u6743\u91cd"}
                                ] : [
                                    {key: "spot_sigma_mm", label: "\u6709\u6548\u53d1\u5c04\u9ad8\u65af\u5bbd\u5ea6 \u03c3 / mm"},
                                    {key: "rate_spot_sigma_per_decade", label: "\u9ad8\u65af\u5bbd\u5ea6\u53d8\u5316 / \u6bcf\u5341\u500d\u901f\u7387"}
                                ]
                                LocalComponents.SourceValueField {
                                    visible: !dialog.library.opticalOnly
                                    enabled: !dialog.library.readOnly && !dialog.library.loading
                                    required property var modelData
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 240
                                    label: modelData.label
                                    value: dialog.modelField(modelData.key)
                                    onEdited: value => dialog.editModel(modelData.key, value)
                                }
                            }
                        }
                        ThemeControls.ThemeLabel { visible: !dialog.library.opticalOnly; text: "模型依据与完整性"; font.bold: true }
                        ThemeControls.ThemeComboBox {
                        visible: !dialog.library.opticalOnly
                            enabled: !dialog.library.readOnly && !dialog.library.loading
                            width: parent.width
                            model: [{text: "未验证 / 项目约定", value: "unvalidated"}, {text: "理论或文献依据", value: "theoretical"}, {text: "实验标定（须填写依据）", value: "calibrated"}]
                            textRole: "text"; valueRole: "value"
                            currentIndex: ["unvalidated", "theoretical", "calibrated"].indexOf(dialog.modelField("provenance_status"))
                            onActivated: dialog.editModel("provenance_status", currentValue)
                        }
                        LocalComponents.SourceValueField { enabled: !dialog.library.readOnly && !dialog.library.loading; width: parent.width; label: "依据 / 实验记录 / 文献链接"; numeric: false; value: dialog.modelField("provenance_reference"); onEdited: value => dialog.editModel("provenance_reference", value) }
                        LocalComponents.SourceValueField { enabled: !dialog.library.readOnly && !dialog.library.loading; width: parent.width; label: "条件、限制与待补充数据"; numeric: false; value: dialog.modelField("provenance_notes"); onEdited: value => dialog.editModel("provenance_notes", value) }
                        ThemeControls.ThemeLabel { width: parent.width; wrapMode: Text.Wrap; text: "“已标定”是用户声明，程序不会验证文献或测量真实性。电子束 σ 是有效蒸汽发射分布代理，不等于束斑热物理预测。速率以 Å/s 显示、以 nm/s 保存。"; opacity: 0.75 }
                    }
                }
            }
        }

    }
    footer: DialogButtonBox {
        visible: dialog.showUseAction
        ThemeControls.ThemeButton { objectName: "useMaterialButton"; visible: dialog.showUseAction; text: dialog.library.purpose === "substrate" ? "选用到当前基片" : "选用到当前源"; enabled: dialog.library.canUseMaterial; onClicked: { if (dialog.library.useMaterial()) dialog.close() } }
    }
}
