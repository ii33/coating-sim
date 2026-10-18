pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "." as LocalComponents
import "theme_controls" as ThemeControls

LocalComponents.AppWindow {
    id: window
    titleBase: window.projectScoped ? "工程资产库" : "全局资产库"
    minimumWidth: 680
    minimumHeight: 500
    readonly property var tokens: window.themeColors
    // qmllint disable unqualified
    readonly property var resourceBridge: bridge
    readonly property var launcherBridge: typeof launcher === "undefined" ? null : launcher
    // qmllint enable unqualified
    property var projectNavigation: null
    projectNameForTitle: projectNavigation ? projectNavigation.projectName : ""
    readonly property var projectResources: resourceBridge ? resourceBridge.materialResources : null
    readonly property var editorInstance: materialEditorLoader.item
    property bool projectPendingTextEdited: false
    property bool projectCloseAuthorized: false
    property bool allowClose: false
    LocalComponents.ProjectPendingEdits { editorWindow: window }
    readonly property bool materialCategory: categoryKey === "material_profile" || categoryKey === "optical_material"
    readonly property bool materialPaneVisible: librarySource && materialCategory && !!resourceBridge && resourceBridge.materialEditorActive
    function commitPendingEdits() { contentItem.forceActiveFocus(); return true }
    function canLeaveMaterialEditor() {
        const editor = editorInstance
        return !editor || ((!editor.setEditorInstance || !editor.setEditorInstance.visible)
            && (!editor.referenceDialogInstance || !editor.referenceDialogInstance.visible))
    }
    function inspectAsset(identity) {
        const row = allRows.find(function(item) { return item.id === identity })
        if (row && librarySource && (row.kind === "material_profile" || row.kind === "optical_material"))
            resourceBridge.openMaterialAsset(identity)
        else
            informationId = identity
    }
    readonly property bool projectScoped: !!resourceBridge && resourceBridge.libraryScope === "project"
    readonly property string libraryLabel: projectScoped ? "工程资产库" : "全局资产库"
    readonly property bool librarySource: String(catalog.source_path || "")
        === String(resourceBridge ? resourceBridge.librarySourcePath : "global")
    readonly property var catalog: resourceBridge ? resourceBridge.transferCatalog || ({}) : ({})
    readonly property var plan: resourceBridge ? resourceBridge.transferPlan || ({}) : ({})
    readonly property bool operationBusy: !!resourceBridge && (resourceBridge.transferBusy || resourceBridge.deletionBusy || resourceBridge.assetApplying)
    readonly property bool operationFailed: !!resourceBridge && resourceBridge.libraryFailed
    readonly property bool cancellable: !resourceBridge || resourceBridge.assetCancellable
    readonly property string operationStatus: resourceBridge && resourceBridge.catalogRefreshPending
        ? "资产目录正在更新，请等待…" : resourceBridge ? resourceBridge.transferStatus : ""
    readonly property string resultMessage: resourceBridge ? resourceBridge.libraryMessage : ""
    readonly property var allRows: catalog.rows || []
    readonly property bool globalSource: catalog.source_type === "global"
    property var selectedIds: []
    property string informationId: ""
    property string categoryKey: ""
    property bool materialSectionRequested: false
    property string materialUse: ""
    property string lastCatalogId: ""
    property var decisions: ({})
    property var restoreRecord: ({})
    readonly property var deletionPlan: resourceBridge ? resourceBridge.deletionPlan || ({}) : ({})
    readonly property bool planChoicesResolved: (plan.rows || []).every(function(row) {
        return row.conflict !== "different" || !!decisions[row.id]
    })
    property var focusReturnItem: null
    readonly property var categories: {
        const result = []
        const order = ["material_profile", "optical_material", "film", "process", "machine",
            "source", "substrate", "mask", "deposition_configuration", "raytrace_request",
            "appearance_configuration", "expression_template", "binary_asset"]
        const actualKinds = (catalog.categories || []).slice()
        if (librarySource && materialSectionRequested && actualKinds.indexOf("material_profile") < 0)
            actualKinds.unshift("material_profile")
        const kinds = actualKinds.sort(function(left, right) {
            const a = order.indexOf(String(left))
            const b = order.indexOf(String(right))
            return (a < 0 ? order.length : a) - (b < 0 ? order.length : b)
                || String(left).localeCompare(String(right))
        })
        for (let i = 0; i < kinds.length; ++i)
            result.push({key: String(kinds[i]), label: kindLabel(String(kinds[i]))})
        return result
    }
    readonly property var materialUses: {
        const existing = []
        for (const row of allRows) {
            if (row.kind === "material_profile") {
                for (const use of row.material_uses || ["other"]) {
                    if (existing.indexOf(use) < 0)
                        existing.push(use)
                }
            }
        }
        const labels = {film: "膜料", substrate: "基片材料", medium: "介质", other: "其他材料", unspecified: "用途未标注"}
        return ["film", "substrate", "medium", "other", "unspecified"].filter(function(use) {
            return existing.indexOf(use) >= 0
        }).map(function(use) { return {key: use, label: labels[use]} })
    }
    readonly property var visibleRows: {
        const kind = categoryKey
        const query = searchField.text.trim().toLowerCase()
        return allRows.filter(function(row) {
            return row.kind === kind
                && (kind !== "material_profile" || (row.material_uses || ["other"]).indexOf(materialUse) >= 0)
                && (!query || String(row.name || "").toLowerCase().indexOf(query) >= 0
                    || String(row.summary || "").toLowerCase().indexOf(query) >= 0)
        })
    }
    readonly property var information: {
        for (let i = 0; i < allRows.length; ++i) {
            if (allRows[i].id === informationId)
                return allRows[i]
        }
        return null
    }
    statusText: operationStatus
    statusDetail: resultMessage

    function kindLabel(kind) {
        const labels = {source: "蒸发源", substrate: "基片", mask: "修正板", machine: "机器",
            process: "工艺", film: "膜系设计", expression_template: "表达式模板",
            material_profile: "材料", optical_material: "光学材料数据",
            deposition_configuration: "膜厚计算配置", raytrace_request: "光追配置",
            appearance_configuration: "外观配置", binary_asset: "依赖输入"}
        return labels[kind] || kind
    }
    function recycleStateLabel(state) {
        const labels = {active: "已回收", restored: "已恢复", rolled_back: "已回滚",
            prepared: "准备中，需审查", uncertain: "未确认完成，需审查", restoring: "恢复中，需审查"}
        return labels[state] || "需审查"
    }
    function selectMaterialUse(use) {
        if (!canLeaveMaterialEditor()) return
        if (materialUse === use)
            return
        materialUse = use
        selectedIds = []
        informationId = ""
        resourceBridge.clearTransferPreview()
    }
    function selectCategory(kind) {
        if (!canLeaveMaterialEditor()) return
        if (categoryKey === kind)
            return
        categoryKey = kind
        selectedIds = []
        informationId = ""
        resourceBridge.clearTransferPreview()
    }
    function choose(identity, checked) {
        const values = selectedIds.filter(function(value) { return value !== identity })
        if (checked)
            values.push(identity)
        selectedIds = values
        informationId = identity
        resourceBridge.clearTransferPreview()
    }
    function restoreFocus() {
        Qt.callLater(function() {
            if (window.focusReturnItem)
                window.focusReturnItem.forceActiveFocus(Qt.OtherFocusReason)
        })
    }
    function choiceRows(values) {
        const labels = {import: "导入", reuse: "复用", copy: "创建副本", overwrite: "覆盖", skip: "跳过"}
        const rows = [{key: "", label: "请选择处理方式"}]
        for (let i = 0; i < values.length; ++i)
            rows.push({key: String(values[i]), label: labels[values[i]] || String(values[i])})
        return rows
    }
    onClosing: function(event) {
        if (!window.cancellable || !window.canLeaveMaterialEditor()) {
            event.accepted = false
            return
        }
        if (window.projectScoped && window.projectResources && !window.projectCloseAuthorized
                && !window.projectResources.requestClose()) {
            event.accepted = false
            return
        }
        if (!window.projectScoped && resourceBridge && resourceBridge.dirty && !window.allowClose) {
            event.accepted = false
            materialCloseDialog.open()
            return
        }
        if (resourceBridge)
            resourceBridge.dispose()
    }

    Connections {
        target: window.resourceBridge
        function onMaterialSelectionChanged(identity) { window.informationId = String(identity) }
        function onTransferChanged() {
            const identity = String(window.catalog.catalog_id || "")
            if (identity && identity !== window.lastCatalogId) {
                window.lastCatalogId = identity
                if (!window.categories.some(function(row) { return row.key === window.categoryKey }))
                    window.categoryKey = window.categories.length ? window.categories[0].key : ""
                if (!window.materialUses.some(function(row) { return row.key === window.materialUse }))
                    window.materialUse = window.materialUses.length ? window.materialUses[0].key : ""
                window.selectedIds = window.selectedIds.filter(function(value) {
                    return window.allRows.some(function(row) {
                        return row.id === value && row.kind === window.categoryKey
                            && (row.kind !== "material_profile"
                                || (row.material_uses || ["other"]).indexOf(window.materialUse) >= 0)
                    })
                })
            }
        }
        function onDeletionPreviewReady() { deletionDialog.open() }
        function onDeletionHistoryReady() { recycleDialog.open() }
        function onDeletionFinished(succeeded) {
            if (succeeded) { deletionDialog.close(); restoreDialog.close(); recycleDialog.close() }
        }
        function onTransferOperationFinished(operationId, kind, succeeded, error) {
            if (kind === "preview" && succeeded) {
                window.decisions = ({})
                previewDialog.open()
            }
            if (kind === "apply" && succeeded)
                previewDialog.close()
        }
    }
    FileDialog {
        id: importDialog
        objectName: "globalResourcesImportDialog"
        title: "选择要导入" + window.libraryLabel + "的资源"
        fileMode: FileDialog.OpenFile
        nameFilters: ["工程与设计资源 (*.cproject *.coatproj *.csrc *.csub *.cmsk *.cmac *.cproc *.cfilm *.cdep *.cray *.cappearance *.casset *.json)", "所有文件 (*)"]
        onAccepted: {
            window.selectedIds = []
            window.informationId = ""
            window.resourceBridge.browseSource(String(selectedFile))
            window.restoreFocus()
        }
        onRejected: window.restoreFocus()
    }
    FileDialog {
        id: exportDialog
        objectName: "globalResourcesExportDialog"
        title: "导出所选资源与依赖"
        fileMode: FileDialog.SaveFile
        defaultSuffix: "cproject"
        nameFilters: ["工程资源包 (*.cproject)"]
        onAccepted: {
            window.resourceBridge.previewLibraryTransfer(window.selectedIds, String(selectedFile))
            window.restoreFocus()
        }
        onRejected: window.restoreFocus()
    }
    ThemeControls.ThemeDialogBase {
        id: previewDialog
        objectName: "globalResourcesPreviewDialog"
        title: "确认资源传输"
        modal: true
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(720, parent ? parent.width - 30 : 720)
        height: Math.min(540, parent ? parent.height - 30 : 540)
        closePolicy: window.cancellable
                   ? Popup.CloseOnEscape : Popup.NoAutoClose
        onClosed: window.restoreFocus()
        ColumnLayout {
            anchors.fill: parent
            ThemeControls.ThemeLabel {
                text: "目标：" + String(window.plan.target_path || "")
                    + "\n所选资源与依赖将一起传输。覆盖必须逐项选择。"
                wrapMode: Text.WrapAnywhere
                Layout.fillWidth: true
            }
            ListView {
                objectName: "globalResourcesPreviewList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: window.plan.rows || []
                spacing: window.tokens.spaceSm
                ScrollBar.vertical: ScrollBar {}
                delegate: ColumnLayout {
                    id: previewRow
                    required property var modelData
                    required property int index
                    width: ListView.view.width
                    ThemeControls.ThemeLabel {
                        text: String(previewRow.modelData.name) + " · "
                            + window.kindLabel(String(previewRow.modelData.kind))
                            + (previewRow.modelData.selected ? "" : "（依赖）")
                            + (previewRow.modelData.conflict === "different" ? " · 内容冲突" : "")
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                    ThemeControls.ThemeComboBox {
                        objectName: "globalResourceConflict_" + previewRow.index
                        accessibleLabel: String(previewRow.modelData.name) + "传输处理"
                        model: window.choiceRows(previewRow.modelData.choices)
                        textRole: "label"
                        currentIndex: {
                            const chosen = window.decisions[previewRow.modelData.id]
                                || (previewRow.modelData.conflict === "identical" ? "reuse"
                                    : !previewRow.modelData.conflict ? "import" : "")
                            return Math.max(0, model.findIndex(function(row) { return row.key === chosen }))
                        }
                        Layout.fillWidth: true
                        enabled: !window.operationBusy
                        onActivated: {
                            const values = Object.assign({}, window.decisions)
                            const key = String(model[currentIndex].key)
                            if (key)
                                values[previewRow.modelData.id] = key
                            else
                                delete values[previewRow.modelData.id]
                            window.decisions = values
                        }
                    }
                }
            }
            ThemeControls.ThemeLabel {
                text: window.operationStatus
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ThemeControls.ThemeButton {
                    text: "取消"
                    enabled: window.cancellable
                    onClicked: {
                        window.resourceBridge.clearTransferPreview()
                        previewDialog.close()
                    }
                }
                ThemeControls.ThemeButton {
                    objectName: "globalResourcesApplyButton"
                    text: "确认传输"
                    primary: true
                    enabled: !window.operationBusy
                        && !!window.plan.plan_id && window.planChoicesResolved
                    onClicked: window.resourceBridge.applyTransfer(window.decisions)
                }
            }
        }
    }

    // The full page becomes scrollable when large accessibility fonts make
    // the header taller than the viewport. Neither font size nor window
    // minimums change, and the resource panes retain usable content heights.
    FontMetrics {
        id: informationHeadingMetrics
        font.family: window.tokens.fontFamily
        font.pointSize: window.tokens.fontSizeHeading
    }
    ScrollView {
        id: pageScroll
        objectName: "globalResourcesPageScroll"
        anchors.fill: parent
        anchors.topMargin: window.contentTopMargin + window.tokens.spaceMd
        anchors.leftMargin: window.tokens.spaceMd
        anchors.rightMargin: window.tokens.spaceMd
        anchors.bottomMargin: window.tokens.spaceMd
        contentWidth: availableWidth
        contentHeight: pageColumn.height
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: ScrollBar.AsNeeded

        Column {
            id: pageColumn
            objectName: "globalResourcesPageContent"
            width: pageScroll.availableWidth
            height: implicitHeight
            spacing: window.tokens.spaceSm
            ColumnLayout {
                id: headerColumn
                width: pageColumn.width
                height: implicitHeight
                spacing: window.tokens.spaceSm
                LocalComponents.ResponsiveOperationBar {
                    objectName: "resourceLibraryOperationBar"
                    Layout.fillWidth: true
                    Layout.preferredHeight: implicitHeight
                    themeTokens: window.tokens
                    LocalComponents.ProjectModuleSelector {
                        navigation: window.projectNavigation
                        currentModuleKey: "project_resources"
                        themeTokens: window.tokens
                    }
                    ThemeControls.ThemeButton {
                        objectName: "globalResourcesRefreshButton"
                        text: window.librarySource ? "刷新" : "返回" + window.libraryLabel
                        enabled: !window.operationBusy
                        onClicked: window.resourceBridge.browseLibrary()
                    }
                    ThemeControls.ThemeButton {
                        objectName: "refractiveIndexDatabaseButton"
                        text: "折射率数据库…"
                        enabled: !window.operationBusy
                        onClicked: {
                            window.focusReturnItem = this
                            window.resourceBridge.openRefractiveIndexBrowser()
                        }
                    }
                    ThemeControls.ThemeButton {
                        objectName: "globalResourcesImportButton"
                        text: "导入…"
                        enabled: !window.operationBusy
                        onClicked: { window.focusReturnItem = this; importDialog.open() }
                    }
                    ThemeControls.ThemeButton {
                        objectName: "globalResourcesExportButton"
                        text: window.librarySource ? "导出所选…" : "预览导入" + window.libraryLabel
                        enabled: !window.operationBusy && window.selectedIds.length > 0
                        onClicked: {
                            window.focusReturnItem = this
                            if (window.librarySource)
                                exportDialog.open()
                            else
                                window.resourceBridge.previewLibraryTransfer(window.selectedIds, window.projectScoped ? "project" : "global")
                        }
                    }
                    ThemeControls.ThemeButton {
                        objectName: "projectResourcesImportGlobalButton"
                        text: "从全局库导入…"
                        visible: window.projectScoped
                        enabled: !window.operationBusy
                        onClicked: {
                            window.selectedIds = []
                            window.informationId = ""
                            window.resourceBridge.browseSource("global")
                        }
                    }
                    ThemeControls.ThemeButton {
                        objectName: "projectResourcesExportGlobalButton"
                        text: "导出至全局库…"
                        visible: window.projectScoped
                        enabled: !window.operationBusy && window.librarySource && window.selectedIds.length > 0
                        onClicked: window.resourceBridge.previewLibraryTransfer(window.selectedIds, "global")
                    }
                    ThemeControls.ThemeButton {
                        objectName: "assetLibraryDeleteButton"
                        text: "回收所选…"
                        enabled: !window.operationBusy && !window.operationFailed && window.librarySource && window.selectedIds.length === 1
                        onClicked: {
                            window.focusReturnItem = this
                            window.resourceBridge.previewAssetDeletion(window.selectedIds)
                        }
                    }
                    ThemeControls.ThemeButton {
                        objectName: "assetLibraryRecycleButton"
                        text: "回收记录…"
                        enabled: !window.operationBusy && window.librarySource
                        onClicked: { window.focusReturnItem = this; window.resourceBridge.loadAssetDeletions() }
                    }
                    ThemeControls.ThemeButton {
                        objectName: "globalResourcesMaterialsButton"
                        text: "材料"
                        enabled: !window.operationBusy && window.librarySource
                        onClicked: {
                            window.materialSectionRequested = true
                            window.selectCategory("material_profile")
                            window.resourceBridge.openMaterialSection()
                        }
                    }
                    ThemeControls.ThemeButton {
                        objectName: "assetLibraryNewMaterialButton"
                        text: "新建材料"
                        enabled: !window.operationBusy && window.librarySource
                        onClicked: {
                            window.materialSectionRequested = true
                            window.selectCategory("material_profile")
                            window.resourceBridge.newMaterialAsset()
                        }
                    }
                }
                ThemeControls.ThemeLabel {
                    text: window.librarySource ? window.libraryLabel : "导入来源：" + String(window.catalog.source_path || "正在读取…")
                    heading: true
                    wrapMode: Text.WrapAnywhere
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeLabel {
                    visible: !!window.catalog.catalog_id
                    text: window.librarySource
                        ? (window.projectScoped ? "浏览当前工程资产；材料可在右侧编辑，其他资产由对应设计窗口保存。" : "按分类浏览全局资源。")
                        : "导入" + window.libraryLabel + "时，每次选择同一分类；所需依赖会自动包含。"
                    muted: true
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeLabel {
                    objectName: "globalResourcesStatus"
                    text: window.operationStatus
                    color: window.operationFailed ? window.tokens.colorError : window.tokens.colorText
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeLabel {
                    objectName: "assetDeletionMessage"
                    visible: !!window.resourceBridge && window.resourceBridge.deletionMessage.length > 0
                    text: window.resourceBridge ? window.resourceBridge.deletionMessage : ""
                    wrapMode: Text.WrapAnywhere
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeLabel {
                    visible: window.resultMessage.length > 0
                    text: window.resultMessage
                    muted: true
                    wrapMode: Text.WrapAnywhere
                    Layout.fillWidth: true
                }
            }
            GridLayout {
                id: resourcePanes
                width: pageColumn.width
                height: Math.max(window.width >= 900 ? 350 : resourceBrowser.implicitHeight + 290,
                    resourceBrowser.implicitHeight, pageScroll.availableHeight - headerColumn.height - pageColumn.spacing)
                columns: window.width >= 900 ? 2 : 1
                rowSpacing: window.tokens.spaceSm
                columnSpacing: window.tokens.spaceSm
                ColumnLayout {
                    id: resourceBrowser
                    Layout.preferredWidth: 285
                    Layout.minimumWidth: 230
                    Layout.maximumWidth: resourcePanes.columns === 2 ? 360 : Infinity
                    Layout.fillWidth: resourcePanes.columns === 1
                    Layout.preferredHeight: Math.max(260, implicitHeight)
                    Layout.minimumHeight: implicitHeight
                    Layout.fillHeight: true
                    spacing: window.tokens.spaceSm
                    ThemeControls.ThemeField {
                        id: searchField
                        objectName: "globalResourcesSearch"
                        accessibleLabel: "搜索资产名称或摘要"
                        placeholderText: "搜索名称 / 摘要"
                        Layout.fillWidth: true
                    }
                    ThemeControls.ThemeComboBox {
                        id: category
                        objectName: "globalResourcesCategory"
                        accessibleLabel: "资产分类"
                        model: window.categories
                        textRole: "label"
                        currentIndex: Math.max(0, window.categories.findIndex(function(row) { return row.key === window.categoryKey }))
                        enabled: !window.operationBusy
                        onActivated: window.selectCategory(String(model[currentIndex].key))
                        Layout.fillWidth: true
                    }
                    ThemeControls.ThemeComboBox {
                        objectName: "resourceLibraryMaterialUse"
                        accessibleLabel: "材料用途"
                        visible: window.categoryKey === "material_profile" && window.materialUses.length > 0
                        model: window.materialUses
                        textRole: "label"
                        currentIndex: Math.max(0, window.materialUses.findIndex(function(row) { return row.key === window.materialUse }))
                        enabled: !window.operationBusy
                        onActivated: window.selectMaterialUse(String(model[currentIndex].key))
                        Layout.fillWidth: true
                    }
                    ThemeControls.ThemeLabel {
                        text: "显示 " + window.visibleRows.length + " / " + window.allRows.length + " 项"
                        muted: true
                        Layout.fillWidth: true
                    }
                    ListView {
                        id: resourceList
                        objectName: "globalResourcesList"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.minimumHeight: Math.max(100, informationHeadingMetrics.height * 4 + window.tokens.spaceSm * 2)
                        clip: true
                        spacing: window.tokens.spaceXs
                        model: window.visibleRows
                        ScrollBar.vertical: ScrollBar {}
                        delegate: RowLayout {
                            id: resourceRow
                            required property var modelData
                            required property int index
                            width: resourceList.width
                            CheckBox {
                                objectName: "globalResourceSelect_" + resourceRow.index
                                Accessible.name: "选择" + String(resourceRow.modelData.name)
                                checked: window.selectedIds.indexOf(resourceRow.modelData.id) >= 0
                                enabled: !window.operationBusy
                                onToggled: window.choose(resourceRow.modelData.id, checked)
                            }
                            ItemDelegate {
                                objectName: "globalResourceInformation_" + resourceRow.index
                                Layout.fillWidth: true
                                highlighted: window.informationId === resourceRow.modelData.id
                                font.family: window.tokens.fontFamily
                                font.pointSize: window.tokens.fontSizeBody
                                contentItem: Column {
                                    spacing: window.tokens.spaceXs
                                    ThemeControls.ThemeLabel {
                                        width: parent.width
                                        text: String(resourceRow.modelData.name)
                                        maximumLineCount: 2
                                        elide: Text.ElideRight
                                        wrapMode: Text.Wrap
                                        font.bold: window.informationId === resourceRow.modelData.id
                                    }
                                    ThemeControls.ThemeLabel {
                                        width: parent.width
                                        text: window.kindLabel(String(resourceRow.modelData.kind))
                                        muted: true
                                        wrapMode: Text.Wrap
                                    }
                                }
                                onClicked: window.inspectAsset(String(resourceRow.modelData.id))
                            }
                        }
                        ThemeControls.ThemeLabel {
                            anchors.centerIn: parent
                            width: parent.width
                            visible: resourceList.count === 0 && !window.operationBusy && !window.operationFailed
                            text: window.allRows.length ? "没有匹配资产，请调整分类或搜索。" : "资产库为空，可导入资产或新建材料。"
                            wrapMode: Text.Wrap
                            muted: true
                        }
                    }
                }
                Item {
                    id: assetDetailPane
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumWidth: 300
                    Layout.minimumHeight: 280
                    Loader {
                        id: materialEditorLoader
                        anchors.fill: parent
                        active: !!window.resourceBridge && !!window.resourceBridge.materialLibrary
                        sourceComponent: Component {
                            LocalComponents.MaterialLibraryDialog {
                                objectName: "assetMaterialEditor"
                                parent: assetDetailPane
                                library: window.resourceBridge.materialLibrary
                                projectResources: window.resourceBridge.materialResources
                                projectNavigation: window.projectNavigation
                                embeddedInAssetLibrary: true
                                hostedInWindowChrome: true
                                showUseAction: false
                                modal: false
                                dim: false
                                closePolicy: Popup.NoAutoClose
                                visible: window.materialPaneVisible
                            }
                        }
                    }
                    ThemeControls.ThemeSidebar {
                        objectName: "globalResourceInformation"
                        anchors.fill: parent
                        visible: !window.materialPaneVisible
                        title: "资产信息"
                        ThemeControls.ThemeLabel {
                            text: window.information ? "名称：" + String(window.information.name || "未命名") : "选择资产查看信息，或选择材料直接编辑。"
                            wrapMode: Text.Wrap
                            Layout.fillWidth: true
                        }
                        ThemeControls.ThemeLabel {
                            text: "摘要：" + String(window.information && window.information.summary || "未填写")
                            wrapMode: Text.Wrap
                            Layout.fillWidth: true
                        }
                        ThemeControls.ThemeLabel {
                            text: "备注：" + String(window.information && window.information.notes || "未填写")
                            wrapMode: Text.Wrap
                            Layout.fillWidth: true
                        }
                        ThemeControls.ThemeLabel {
                            text: "依赖：" + (window.information ? (window.information.dependencies || []).length : 0)
                            muted: true
                            Layout.fillWidth: true
                        }
                    }
                }
            }
        }
    }
    Loader {
        active: !!window.resourceBridge && !!window.resourceBridge.refractiveIndexBrowser
        sourceComponent: Component {
            LocalComponents.RefractiveIndexBrowserDialog {
                parent: Overlay.overlay
                browser: window.resourceBridge.refractiveIndexBrowser
                onClosed: window.restoreFocus()
            }
        }
    }
    ThemeControls.ThemeDialogBase {
        id: deletionDialog
        objectName: "assetDeletionPreviewDialog"
        title: "确认回收" + window.libraryLabel + "资产"
        modal: true
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(660, parent ? parent.width - 30 : 660)
        height: Math.min(520, parent ? parent.height - 30 : 520)
        closePolicy: window.cancellable ? Popup.CloseOnEscape : Popup.NoAutoClose
        onClosed: { window.resourceBridge.cancelAssetDeletion(); window.restoreFocus() }
        contentItem: ColumnLayout {
            ThemeControls.ThemeLabel {
                text: "范围：" + window.libraryLabel + "\n"
                    + (window.deletionPlan.action === "revert_builtin_override"
                       ? "归档自定义覆盖并恢复内置内容；覆盖内容可从回收记录恢复。"
                       : "资产将移入可恢复的回收记录。不会级联移除引用它的其他资产。")
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                contentWidth: availableWidth
                ColumnLayout {
                    width: parent.width
                    Repeater {
                        model: window.deletionPlan.rows || []
                        ThemeControls.ThemeLabel {
                            required property var modelData
                            text: String(modelData.name || modelData.id) + " · " + window.kindLabel(String(modelData.kind))
                                + (modelData.same_asset_component ? "（同一资产组成）" : "")
                            wrapMode: Text.WrapAnywhere
                            Layout.fillWidth: true
                        }
                    }
                    Repeater {
                        model: window.deletionPlan.blockers || []
                        ThemeControls.ThemeLabel {
                            required property var modelData
                            text: "无法回收：" + String(modelData.message || modelData.code || "引用检查未通过")
                                + (modelData.name ? " · " + modelData.name : "")
                            color: window.tokens.colorError
                            wrapMode: Text.WrapAnywhere
                            Layout.fillWidth: true
                        }
                    }
                }
            }
            ThemeControls.ThemeLabel {
                text: window.resourceBridge ? window.resourceBridge.deletionMessage : ""
                wrapMode: Text.WrapAnywhere
                Layout.fillWidth: true
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ThemeControls.ThemeButton {
                    objectName: "assetDeletionCancelButton"
                    text: "取消"
                    enabled: window.cancellable
                    onClicked: deletionDialog.close()
                }
                ThemeControls.ThemeButton {
                    objectName: "assetDeletionConfirmButton"
                    text: window.deletionPlan.action === "revert_builtin_override" ? "归档覆盖，恢复内置" : "确认回收"
                    enabled: !window.operationBusy && window.deletionPlan.can_apply === true
                    onClicked: window.resourceBridge.confirmAssetDeletion()
                }
            }
        }
    }
    ThemeControls.ThemeDialogBase {
        id: recycleDialog
        objectName: "assetDeletionHistoryDialog"
        title: window.libraryLabel + "回收记录"
        modal: true
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(660, parent ? parent.width - 30 : 660)
        height: Math.min(480, parent ? parent.height - 30 : 480)
        onClosed: window.restoreFocus()
        contentItem: ColumnLayout {
            ThemeControls.ThemeLabel { text: "恢复只写回当前资产库；目标冲突时保留现有内容并拒绝恢复。"; wrapMode: Text.Wrap; Layout.fillWidth: true }
            ListView {
                objectName: "assetDeletionHistoryList"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                model: window.resourceBridge ? window.resourceBridge.deletionRecords : []
                ScrollBar.vertical: ScrollBar {}
                delegate: ItemDelegate {
                    required property var modelData
                    required property int index
                    objectName: "assetDeletionRestoreEntry_" + index
                    readonly property bool recoverable: modelData.recoverable === true || modelData.state === "active"
                    width: ListView.view.width
                    enabled: recoverable
                    text: String(modelData.created_at || modelData.receipt_id) + " · " + (modelData.affected_ids || []).length
                        + (recoverable ? " 项资产组成 · 恢复…" : " 项资产组成 · 记录状态：" + window.recycleStateLabel(String(modelData.state || "")))
                    onClicked: { window.restoreRecord = modelData; restoreDialog.open() }
                }
                ThemeControls.ThemeLabel { anchors.centerIn: parent; visible: parent.count === 0; text: "当前资产库没有可恢复记录"; muted: true }
            }
            ThemeControls.ThemeButton { text: "关闭"; onClicked: recycleDialog.close() }
        }
    }
    ThemeControls.ThemeDialogBase {
        id: restoreDialog
        objectName: "assetRestoreDialog"
        title: "确认恢复资产"
        modal: true
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(560, parent ? parent.width - 30 : 560)
        closePolicy: window.cancellable ? Popup.CloseOnEscape : Popup.NoAutoClose
        contentItem: ColumnLayout {
            ThemeControls.ThemeLabel {
                text: "恢复至：" + window.libraryLabel + "\n回收时间：" + String(window.restoreRecord.created_at || "")
                    + "\n恢复位置：" + String(window.restoreRecord.recovery_path || "")
                    + "\n恢复不会覆盖当前同名或同身份资产。"
                wrapMode: Text.WrapAnywhere
                Layout.fillWidth: true
            }
            ThemeControls.ThemeLabel { text: window.resourceBridge ? window.resourceBridge.deletionMessage : ""; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true }
            RowLayout {
                ThemeControls.ThemeButton { text: "取消"; enabled: window.cancellable; onClicked: restoreDialog.close() }
                ThemeControls.ThemeButton {
                    objectName: "assetRestoreConfirmButton"
                    text: "确认恢复"
                    enabled: !window.operationBusy
                    onClicked: window.resourceBridge.restoreAssetDeletion(String(window.restoreRecord.receipt_id || ""))
                }
            }
        }
    }
    ThemeControls.ThemeDialogBase {
        id: materialCloseDialog
        objectName: "assetMaterialCloseDialog"
        title: "材料有未保存修改"
        modal: true
        parent: Overlay.overlay
        anchors.centerIn: parent
        contentItem: ColumnLayout {
            ThemeControls.ThemeLabel { text: "可保存后关闭、放弃修改并关闭，或取消返回资产库。"; wrapMode: Text.Wrap }
            RowLayout {
                ThemeControls.ThemeButton {
                    text: "保存并关闭"
                    enabled: !!window.resourceBridge.materialLibrary && window.resourceBridge.materialLibrary.valid
                    onClicked: {
                        if (window.resourceBridge.saveMaterialForClose()) {
                            window.allowClose = true
                            materialCloseDialog.close()
                            window.close()
                        }
                    }
                }
                ThemeControls.ThemeButton {
                    text: "放弃并关闭"
                    onClicked: { window.allowClose = true; materialCloseDialog.close(); window.close() }
                }
                ThemeControls.ThemeButton { text: "取消"; onClicked: materialCloseDialog.close() }
            }
        }
    }
    Component.onCompleted: window.resourceBridge.attachAssetWindow(window)
}
