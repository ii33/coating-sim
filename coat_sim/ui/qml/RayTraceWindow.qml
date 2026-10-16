pragma ComponentBehavior: Bound
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
    property var projectResources: null
    property string projectModuleKey: "ray_trace"
    property var controller: bridge
    property bool allowClose: false
    property string pendingOverwriteKind: ""
    property string pendingOverwritePath: ""
    property string pendingFilmImportMessage: ""
    readonly property bool compactLayout: width < 980 || height < 620
    property bool sidebarExpanded: true
    property bool appearanceSidebarExpanded: true
    property string selectedCategory: "asset"
    property bool restoreAssetFocusPending: false
    titleBase: window.controller.workspaceMode === "appearance"
           ? "薄膜外观"
           : controller && window.controller.documentName.length > 0
             ? window.controller.documentName + (window.controller.documentDirty ? " •" : "")
             : "几何光线追迹"
    captionBase: window.controller.workspaceMode === "appearance"
             ? "本地光谱渲染" : "膜系 · 沉积厚度 · 成像光线"
    projectNameForTitle: projectNavigation ? projectNavigation.projectName : ""
    statusText: window.projectResources && (window.projectResources.resourceBusy
                    || window.projectResources.resourceOperationError.length > 0)
                ? window.projectResources.status
                : window.controller.error.length > 0 ? window.controller.error : window.controller.status
    statusSeverity: window.projectResources && window.projectResources.resourceBusy ? "busy"
        : window.projectResources && window.projectResources.resourceOperationError.length > 0 ? "error"
        : window.controller.error.length > 0 || window.controller.phase === "failed" ? "error"
        : window.controller.phase === "cancelling" ? "warning"
        : window.controller.busy ? "busy"
        : window.controller.phase === "complete" ? "success" : "info"
    statusDetail: window.controller.phase + " · " + window.controller.status
        + (window.controller.error.length > 0 ? " · " + window.controller.error : "")
    width: 1180
    height: 820
    minimumWidth: 720
    minimumHeight: 520
    initialMaximized: false
    color: window.themeColors.colorWindow
    property int themeRevision: Theme.revision

    onClosing: function(close) {
        if (window.projectResources && window.projectResources.resourceBusy) {
            if (!window.projectResources.resourceCancellable) {
                close.accepted = false
                return
            }
            if (!window.projectResources.cancelResourceOperation()) {
                close.accepted = false
                return
            }
        }
        if (!allowClose && !projectCloseAuthorized) {
            close.accepted = false
            window.controller.requestClose()
        } else {
            window.controller.detach_observers()
        }
    }

    Component.onCompleted: {
        if (window.width < 980) {
            window.sidebarExpanded = false
            window.appearanceSidebarExpanded = false
        }
    }
    onWidthChanged: {
        if (window.width < 850) {
            window.sidebarExpanded = false
            window.appearanceSidebarExpanded = false
        }
    }
    function restoreCloseFocus() {
        if (window.controller.workspaceMode === "appearance") {
            if (appearanceWorkspaceLoader.item)
                appearanceWorkspaceLoader.item.forceActiveFocus()
            else
                appearanceChooseAssetButton.forceActiveFocus()
        } else {
            requestMenuButton.forceActiveFocus()
        }
    }
    function restoreLayerAssetFocus() {
        if (window.controller.workspaceMode === "appearance") {
            if (appearanceWorkspaceLoader.item)
                appearanceWorkspaceLoader.item.focusAssetAction()
            else
                appearanceChooseAssetButton.forceActiveFocus()
        } else {
            layerAssetButton.forceActiveFocus()
        }
    }
    Shortcut {
        objectName: "rayTraceAppearanceSidebarShortcut"
        sequence: "F4"
        context: Qt.WindowShortcut
        enabled: window.controller.workspaceMode === "appearance"
                 && (window.controller.appearanceController === null
                     || window.controller.appearanceController === undefined)
        onActivated: window.appearanceSidebarExpanded = !window.appearanceSidebarExpanded
    }

    Connections {
        target: window.controller
        function onCloseConfirmationRequested() { closeDialog.open() }
        function onCloseReady() {
            window.allowClose = true
            Qt.callLater(window.close)
        }
        function onRequestSaveAsRequested() { requestSaveDialog.open() }
        function onRequestOverwriteRequested(kind, path) {
            window.pendingOverwriteKind = kind
            window.pendingOverwritePath = path
            overwriteDialog.open()
        }
        function onFilmImportConfirmationRequested(message) {
            window.pendingFilmImportMessage = message
            filmReplaceDialog.open()
        }
        function onChanged() {
            pathCanvas.requestPaint()
            histogramCanvas.requestPaint()
            if (window.restoreAssetFocusPending && !window.controller.geometricBusy) {
                window.restoreAssetFocusPending = false
                Qt.callLater(window.restoreLayerAssetFocus)
            }
        }
    }

    FileDialog {
        id: requestOpenDialog
        title: "打开光追请求或窗口文档"
        fileMode: FileDialog.OpenFile
        currentFolder: window.controller.defaultDirectory
        nameFilters: ["光追请求/窗口文档 (*.json)", "JSON 文件 (*.json)", "所有文件 (*)"]
        onAccepted: {
            window.controller.loadRequest(selectedFile.toString())
            Qt.callLater(function() { requestMenuButton.forceActiveFocus() })
        }
        onRejected: requestMenuButton.forceActiveFocus()
    }
    FileDialog {
        id: requestSaveDialog
        title: "保存光追请求"
        fileMode: FileDialog.SaveFile
        currentFolder: window.controller.defaultDirectory
        defaultSuffix: "json"
        nameFilters: ["光追窗口文档 (*.raytrace.json)", "JSON 文件 (*.json)"]
        onAccepted: {
            window.controller.saveRequest(selectedFile.toString(), false)
            Qt.callLater(function() { requestMenuButton.forceActiveFocus() })
        }
        onRejected: {
            window.controller.cancelSaveAs()
            Qt.callLater(function() { requestMenuButton.forceActiveFocus() })
        }
    }
    FileDialog {
        id: appearanceResultSaveDialog
        objectName: "appearanceResultSaveDialog"
        title: "导出外观结果 PNG"
        fileMode: FileDialog.SaveFile
        currentFolder: window.controller.defaultDirectory
        defaultSuffix: "png"
        nameFilters: ["PNG 图像 (*.png)"]
        onAccepted: window.controller.exportAppearancePreview(selectedFile.toString(), false)
    }
    FileDialog {
        id: resultOpenDialog
        title: "打开已保存光追结果"
        fileMode: FileDialog.OpenFile
        currentFolder: window.controller.defaultDirectory
        nameFilters: ["光追结果 JSON (*.json)", "所有文件 (*)"]
        onAccepted: {
            window.controller.loadResult(selectedFile.toString())
            Qt.callLater(function() { resultMenuButton.forceActiveFocus() })
        }
        onRejected: resultMenuButton.forceActiveFocus()
    }
    FileDialog {
        id: resultSaveDialog
        title: "保存光追结果"
        fileMode: FileDialog.SaveFile
        currentFolder: window.controller.defaultDirectory
        defaultSuffix: "json"
        nameFilters: ["光追结果 JSON (*.json)"]
        onAccepted: {
            window.controller.saveResult(selectedFile.toString(), false)
            Qt.callLater(function() { resultMenuButton.forceActiveFocus() })
        }
        onRejected: Qt.callLater(function() { resultMenuButton.forceActiveFocus() })
    }
    FileDialog {
        id: filmOpenDialog
        title: "选择已有膜系设计"
        fileMode: FileDialog.OpenFile
        currentFolder: window.controller.filmDesignDirectory
        nameFilters: ["膜系设计 (*.cfilm *.json)", "所有文件 (*)"]
        onAccepted: {
            window.controller.attachFilmDesign(selectedFile.toString(), surfaceCombo.currentText,
                                        filmSideCombo.currentText)
            Qt.callLater(function() { importFilmButton.forceActiveFocus() })
        }
        onRejected: Qt.callLater(function() { importFilmButton.forceActiveFocus() })
    }
    FileDialog {
        id: layerAssetDialog
        objectName: "rayTraceLayerAssetDialog"
        property bool restoreFocusOnDismiss: false
        onVisibleChanged: {
            if (visible) {
                restoreFocusOnDismiss = true
            } else if (restoreFocusOnDismiss) {
                restoreFocusOnDismiss = false
                Qt.callLater(function() {
                    window.requestActivate()
                    Qt.callLater(function() { Qt.callLater(window.restoreLayerAssetFocus) })
                })
            }
        }
        title: "选择包含逐层厚度场的沉积资产"
        fileMode: FileDialog.OpenFile
        currentFolder: window.controller.defaultDirectory
        nameFilters: ["旧版沉积结果包 (*.coatasset)", "所有文件 (*)"]
        onAccepted: {
            window.restoreAssetFocusPending = window.controller.workspaceMode === "appearance"
            window.controller.loadLayerAsset(selectedFile.toString())
            Qt.callLater(function() {
                if (!window.controller.geometricBusy)
                    window.restoreLayerAssetFocus()
            })
        }
        onRejected: Qt.callLater(function() {
            window.requestActivate()
            Qt.callLater(window.restoreLayerAssetFocus)
        })
    }

    ThemeControls.ThemeDialogBase {
        id: closeDialog
        objectName: "rayTraceCloseDialog"
        title: "关闭光线追迹窗口？"
        modal: true
        anchors.centerIn: parent
        width: Math.min(530, window.width - 2 * window.themeColors.spaceXl)
        contentItem: Label {
            text: window.controller.appearanceRunning
                  ? (window.controller.documentDirty
                     ? "本地外观渲染正在运行，几何请求也有未保存修改。确认会在当前波长帧结束后关闭，并丢弃未保存的几何请求修改。"
                     : "本地外观渲染正在运行。确认会在当前波长帧结束后关闭；选择返回可继续编辑。")
                  : window.controller.busy
                    ? (window.controller.documentDirty
                       ? "几何追迹正在运行，关闭会在当前光线/表面边界取消计算，并丢弃未保存的请求修改。"
                       : "后台几何计算正在运行。关闭会在当前光线/表面边界请求取消。")
                    : window.projectResources
                      ? "工程输入有未保存修改。可先保存，或关闭窗口并在当前工程会话中保留当前内容。"
                      : "光追请求含未保存修改。选择保存、放弃修改或返回继续编辑。"
            color: window.themeColors.colorText
            wrapMode: Text.WordWrap
            padding: window.themeColors.spaceMd
        }
        footer: RowLayout {
            spacing: window.themeColors.spaceSm
            ThemeControls.ThemeButton {
                visible: window.controller.documentDirty && !window.controller.busy && !window.controller.appearanceRunning
                text: "保存并关闭"
                primary: true
                onClicked: {
                    closeDialog.close()
                    window.controller.saveAndClose()
                }
            }
            ThemeControls.ThemeButton {
                objectName: "rayTraceCloseDiscardButton"
                text: window.controller.appearanceRunning
                      ? (window.controller.documentDirty ? "取消渲染并丢弃修改" : "取消渲染并关闭")
                      : window.controller.busy
                        ? (window.controller.documentDirty ? "取消计算并丢弃修改" : "取消计算并关闭")
                        : window.projectResources ? "保留当前内容并关闭" : "放弃修改并关闭"
                onClicked: {
                    closeDialog.close()
                    window.controller.resolveClose(true)
                }
            }
            ThemeControls.ThemeButton {
                objectName: "rayTraceCloseReturnButton"
                text: "返回"
                onClicked: {
                    closeDialog.close()
                    window.controller.resolveClose(false)
                        Qt.callLater(window.restoreCloseFocus)
                }
            }
        }
    }

    ThemeControls.ThemeDialogBase {
        id: overwriteDialog
        title: "覆盖已有文件？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Label {
            text: "目标文件已经存在。要用当前内容替换吗？\n" + window.pendingOverwritePath
            color: window.themeColors.colorText
            wrapMode: Text.WordWrap
            padding: window.themeColors.spaceMd
        }
        onAccepted: {
            const overwriteKind = window.pendingOverwriteKind
            window.controller.resolveOverwrite(true)
            window.pendingOverwriteKind = ""
            window.pendingOverwritePath = ""
            Qt.callLater(function() {
                if (overwriteKind === "result") resultMenuButton.forceActiveFocus()
                else saveRequestButton.forceActiveFocus()
            })
        }
        onRejected: {
            const overwriteKind = window.pendingOverwriteKind
            window.controller.resolveOverwrite(false)
            window.pendingOverwriteKind = ""
            window.pendingOverwritePath = ""
            Qt.callLater(function() {
                if (overwriteKind === "result") resultMenuButton.forceActiveFocus()
                else saveRequestButton.forceActiveFocus()
            })
        }
    }

    ThemeControls.ThemeDialogBase {
        id: filmReplaceDialog
        title: "替换光学面膜层？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Label {
            text: window.pendingFilmImportMessage
            color: window.themeColors.colorText
            wrapMode: Text.WordWrap
            padding: window.themeColors.spaceMd
        }
        onAccepted: {
            window.controller.confirmFilmDesignImport(true)
            window.pendingFilmImportMessage = ""
            Qt.callLater(function() { surfaceCombo.forceActiveFocus() })
        }
        onRejected: {
            window.controller.confirmFilmDesignImport(false)
            window.pendingFilmImportMessage = ""
            surfaceCombo.forceActiveFocus()
        }
    }

    Menu {
        id: requestMenu
        MenuItem {
            id: newRequestButton
            objectName: "rayTraceNewRequestButton"
            text: "新建请求"
            enabled: !window.controller.busy && !window.controller.appearanceRunning
            onTriggered: window.controller.newRequest()
        }
        MenuItem {
            id: openRequestButton
            objectName: "rayTraceOpenRequestButton"
            text: "打开请求"
            enabled: !window.controller.busy && !window.controller.appearanceRunning
            onTriggered: requestOpenDialog.open()
        }
        MenuItem {
            id: saveRequestButton
            objectName: "rayTraceSaveRequestButton"
            text: "保存请求"
            enabled: window.controller.hasRequest && !window.controller.busy && !window.controller.appearanceRunning
            onTriggered: window.controller.requestPath.length > 0
                         ? window.controller.saveRequest(window.controller.requestPath, true)
                         : window.controller.requestSaveAs()
        }
        MenuItem {
            id: saveRequestAsButton
            objectName: "rayTraceSaveRequestAsButton"
            text: "请求另存为"
            enabled: window.controller.hasRequest && !window.controller.busy && !window.controller.appearanceRunning
            onTriggered: window.controller.requestSaveAs()
        }
    }
    Menu {
        id: resultMenu
        MenuItem {
            id: openResultButton
            objectName: "rayTraceOpenResultButton"
            text: "打开结果"
            enabled: !window.controller.busy && !window.controller.appearanceRunning
            onTriggered: resultOpenDialog.open()
        }
        MenuItem {
            id: saveResultButton
            objectName: "rayTraceSaveResultButton"
            text: "结果另存为"
            enabled: window.controller.hasResult
            onTriggered: resultSaveDialog.open()
        }
    }

    Shortcut {
        sequence: "F4"
        context: Qt.WindowShortcut
        enabled: window.controller.workspaceMode === "geometric"
        onActivated: window.sidebarExpanded = !window.sidebarExpanded
    }

    ColumnLayout {
        objectName: "rayTraceContentLayout"
        anchors.fill: parent
        anchors.leftMargin: window.themeColors.spaceMd
        anchors.rightMargin: window.themeColors.spaceMd
        anchors.topMargin: window.contentTopMargin + window.themeColors.spaceSm
        anchors.bottomMargin: window.themeColors.spaceMd
        spacing: window.themeColors.spaceSm

        ResponsiveOperationBar {
            id: opticalInputHeader
            visible: window.projectResources !== null || window.projectNavigation !== null
            Layout.fillWidth: true
            Layout.preferredHeight: childrenRect.height
            spacing: window.themeColors.spaceXs
            ProjectModuleSelector {
                navigation: window.projectNavigation
                currentModuleKey: window.projectModuleKey
            }
            ProjectResourceControls {
                id: opticalResourceControls
                projectResources: window.projectResources
                width: visible ? Math.min(520, opticalInputHeader.width) : 0
                beforeDocumentAction: function(action, documentId) {
                    const basicEditor = window.activeFocusItem as TextField
                    if (basicEditor) basicEditor.editingFinished()
                    opticalResourceControls.forceActiveFocus()
                    return !window.controller.busy && !window.controller.appearanceRunning
                }
                extraTransferActions: window.projectModuleKey === "ray_trace" ? [
                    {key: "result_open", text: "结果：打开光追结果…", enabled: !window.controller.busy},
                    {key: "result_save", text: "结果：导出光追结果…", enabled: window.controller.hasResult}
                ] : [
                    {key: "appearance_png", text: "结果：导出外观 PNG…",
                     enabled: window.controller.appearanceController && window.controller.appearanceController.hasPreview
                              && !window.controller.busy && !window.controller.appearanceRunning}
                ]
                onExtraTransferActionRequested: function(key) {
                    if (key === "result_open") resultOpenDialog.open()
                    else if (key === "result_save") resultSaveDialog.open()
                    else if (key === "appearance_png") appearanceResultSaveDialog.open()
                }
            }
        }

        ThemeControls.ThemeSurface {
            objectName: "rayTraceContentToolbar"
            Accessible.name: "操作栏"
            visible: window.controller.workspaceMode === "geometric"
                     || (window.controller.workspaceMode === "appearance" && !appearanceWorkspaceLoader.item)
            Layout.fillWidth: true
            contentSpacing: 0
            RowLayout {
                Layout.fillWidth: true
                spacing: window.themeColors.spaceXs
                ThemeControls.ThemeComboBox {
                    objectName: "rayTraceWorkspaceMode"
                    Layout.preferredWidth: window.compactLayout ? 164 : 210
                    Layout.minimumWidth: 138
                    model: ["外观渲染", "几何光追（高级）"]
                    currentIndex: window.controller.workspaceMode === "geometric" ? 1 : 0
                    Accessible.name: "工作模式"
                    onActivated: function(index) { window.controller.setWorkspaceMode(index === 0 ? "appearance" : "geometric") }
                }
                ThemeControls.ThemeLabel {
                    visible: window.controller.workspaceMode === "appearance"
                    Layout.fillWidth: true
                    text: "本地光谱渲染 · 仅几何预览"
                    muted: true
                    elide: Text.ElideRight
                }
                ThemeControls.ThemeButton {
                    id: requestMenuButton
                    objectName: "rayTraceRequestMenuButton"
                    visible: !window.projectResources && window.controller.workspaceMode === "geometric"
                    text: "请求 ▾"
                    onClicked: requestMenu.open()
                    Accessible.name: "请求文件操作"
                }
                ThemeControls.ThemeButton {
                    id: resultMenuButton
                    objectName: "rayTraceResultMenuButton"
                    visible: !window.projectResources && window.controller.workspaceMode === "geometric"
                    text: "结果 ▾"
                    onClicked: resultMenu.open()
                    Accessible.name: "结果文件操作"
                }
                ThemeControls.ThemeButton {
                    id: importFilmButton
                    objectName: "rayTraceImportFilmButton"
                    visible: window.controller.workspaceMode === "geometric"
                    text: window.compactLayout ? "膜系" : "从膜系导入"
                    enabled: !window.controller.busy && !window.controller.appearanceRunning && surfaceCombo.currentText.length > 0
                    onClicked: filmOpenDialog.open()
                }
                ThemeControls.ThemeButton {
                    id: layerAssetButton
                    objectName: "rayTraceLoadLayerAssetButton"
                    visible: window.controller.workspaceMode === "geometric"
                    text: window.compactLayout ? "资产" : "选择逐层资产"
                    enabled: !window.controller.busy && !window.controller.appearanceRunning
                    onClicked: layerAssetDialog.open()
                }
                Item { visible: window.controller.workspaceMode === "geometric"; Layout.fillWidth: true }
                ThemeControls.ThemeButton {
                    objectName: "rayTraceCancelButton"
                    visible: window.controller.workspaceMode === "geometric" || window.controller.appearanceRunning
                    text: window.controller.appearanceRunning ? "取消外观"
                          : window.controller.phase === "cancelling" ? "取消中" : "取消"
                    enabled: window.controller.appearanceRunning
                             || (window.controller.busy && window.controller.phase !== "cancelling")
                    onClicked: window.controller.appearanceRunning
                               ? window.controller.appearanceController.cancelRender()
                               : window.controller.cancelCalculation()
                }
                ThemeControls.ThemeButton {
                    objectName: "rayTraceRunButton"
                    visible: window.controller.workspaceMode === "geometric"
                    text: window.controller.phase === "running" ? "正在计算" : "开始追迹"
                    primary: true
                    enabled: window.controller.canRun && !window.controller.appearanceRunning
                    onClicked: window.controller.startCalculation()
                }
            }
        }

        Item {
            id: workspaceArea
            Layout.fillWidth: true
            Layout.fillHeight: true
            RowLayout {
                id: geometryWorkspace
                objectName: "rayTraceGeometryWorkspace"
                anchors.fill: parent
                spacing: window.themeColors.spaceSm
                visible: window.controller.workspaceMode === "geometric"
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 260
                    Layout.fillHeight: true
                    spacing: window.themeColors.spaceSm
            ThemeControls.ThemeSurface {
                id: viewPanel
                objectName: "rayTraceViewPanel"
                Layout.fillWidth: true
                Layout.minimumWidth: 260
                Layout.fillHeight: true
                contentSpacing: window.themeColors.spaceSm
                TabBar {
                    id: resultTabs
                    objectName: "rayTraceCentralViewTabs"
                    Layout.fillWidth: true
                    TabButton {
                        objectName: "rayTracePathsTab"
                        text: "路径"
                        Accessible.name: "光线路径预览"
                    }
                    TabButton {
                        objectName: "rayTraceSpectrumTab"
                        text: "光谱能量"
                        Accessible.name: "光谱能量结果"
                    }
                    TabButton {
                        objectName: "rayTraceDetectorTab"
                        text: "像面能量"
                        Accessible.name: "像面射线能量结果"
                    }
                }
                StackLayout {
                    id: resultPanel
                    objectName: "rayTraceResultTabsPanel"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    currentIndex: resultTabs.currentIndex
                    Item {
                        ColumnLayout {
                            anchors.fill: parent
                            spacing: window.themeColors.spaceSm
                RowLayout {
                    Layout.fillWidth: true
                    Label {
                        objectName: "rayTracePathHeading"
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        text: window.controller.hasResult
                              ? "光线路径 · XZ 子午面"
                              : "光线路径与像面"
                        color: window.themeColors.colorText
                        font.pointSize: window.themeColors.fontSizeHeading
                        font.weight: window.themeColors.fontWeightEmphasis
                        elide: Text.ElideRight
                    }
                    Label {
                        text: window.compactLayout ? "λ" : "波长"
                        color: window.themeColors.colorTextMuted
                    }
                    ThemeControls.ThemeComboBox {
                        id: wavelengthCombo
                        objectName: "rayTraceDisplayWavelength"
                        Layout.preferredWidth: window.compactLayout ? 92 : 126
                        Layout.minimumWidth: 80
                        enabled: window.controller.hasResult && count > 0
                        model: window.controller.wavelengthLabels
                        currentIndex: Math.min(window.controller.selectedSpectrumIndex, Math.max(0, count - 1))
                        onActivated: function(index) { window.controller.setSelectedSpectrumIndex(index) }
                        Accessible.name: "路径图显示波长"
                    }
                }
                Canvas {
                    id: pathCanvas
                    objectName: "rayTracePathCanvas"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumHeight: window.compactLayout ? 170 : 230
                    antialiasing: true
                    onPaint: {
                        const ctx = getContext("2d")
                        ctx.clearRect(0, 0, width, height)
                        ctx.fillStyle = window.themeColors.colorSurfaceRaised.toString()
                        ctx.fillRect(0, 0, width, height)
                        const traces = window.controller.displayPaths
                        if (!traces || traces.length === 0) {
                            ctx.fillStyle = window.themeColors.colorTextMuted.toString()
                            ctx.font = Math.max(13, window.themeColors.fontSizeBody * 1.33) + "px " + window.themeColors.fontFamily
                            ctx.textAlign = "center"
                            ctx.fillText(window.controller.hasResult
                                          ? "结果中没有可显示的光线路径"
                                          : "载入请求并开始追迹后显示光路", width / 2, height / 2)
                            return
                        }
                        let xmin = Infinity, xmax = -Infinity, zmin = Infinity, zmax = -Infinity
                        for (let i = 0; i < traces.length; ++i) {
                            const pts = traces[i].points
                            for (let j = 0; j < pts.length; ++j) {
                                xmin = Math.min(xmin, pts[j][0]); xmax = Math.max(xmax, pts[j][0])
                                zmin = Math.min(zmin, pts[j][1]); zmax = Math.max(zmax, pts[j][1])
                            }
                        }
                        if (xmax - xmin < 1e-9) { xmin -= 1; xmax += 1 }
                        if (zmax - zmin < 1e-9) { zmin -= 1; zmax += 1 }
                        const px = Math.max(18, window.themeColors.spaceMd)
                        const top = Math.max(16, window.themeColors.spaceMd)
                        const right = Math.max(16, window.themeColors.spaceLg)
                        const bottom = Math.max(30, window.themeColors.controlHeightSm)
                        const plotWidth = Math.max(1, width - px - right)
                        const plotHeight = Math.max(1, height - top - bottom)
                        const padX = (xmax - xmin) * 0.08
                        const padZ = (zmax - zmin) * 0.08
                        xmin -= padX; xmax += padX; zmin -= padZ; zmax += padZ
                        function mapX(v) { return px + (v - xmin) / (xmax - xmin) * plotWidth }
                        function mapZ(v) { return top + (zmax - v) / (zmax - zmin) * plotHeight }
                        ctx.strokeStyle = window.themeColors.colorBorder.toString()
                        ctx.lineWidth = window.themeColors.borderWidth
                        ctx.beginPath()
                        for (let k = 0; k <= 4; ++k) {
                            const xx = px + plotWidth * k / 4
                            const yy = top + plotHeight * k / 4
                            ctx.moveTo(xx, top); ctx.lineTo(xx, top + plotHeight)
                            ctx.moveTo(px, yy); ctx.lineTo(px + plotWidth, yy)
                        }
                        ctx.stroke()
                        const colors = ["#276FBF", "#E58B2A", "#168C9C", "#845EC2", "#3F9B69", "#C04450"]
                        for (let i = 0; i < traces.length; ++i) {
                            const pts = traces[i].points
                            ctx.strokeStyle = traces[i].status === "transmitted"
                                              ? colors[i % colors.length] : "#C07A2A"
                            ctx.lineWidth = Math.max(1.4, window.themeColors.borderWidth * 1.5)
                            ctx.beginPath()
                            ctx.moveTo(mapX(pts[0][0]), mapZ(pts[0][1]))
                            for (let j = 1; j < pts.length; ++j)
                                ctx.lineTo(mapX(pts[j][0]), mapZ(pts[j][1]))
                            ctx.stroke()
                            for (let j = 1; j < pts.length; ++j) {
                                ctx.fillStyle = colors[i % colors.length]
                                ctx.beginPath()
                                ctx.arc(mapX(pts[j][0]), mapZ(pts[j][1]), 2.6, 0, Math.PI * 2)
                                ctx.fill()
                            }
                        }
                        ctx.fillStyle = window.themeColors.colorTextMuted.toString()
                        ctx.font = Math.max(10, window.themeColors.fontSizeSmall * 1.33) + "px " + window.themeColors.fontFamily
                        ctx.textAlign = "center"
                        ctx.fillText("x / mm", px + plotWidth / 2, height - 5)
                        ctx.save()
                        ctx.translate(11, top + plotHeight / 2)
                        ctx.rotate(-Math.PI / 2)
                        ctx.fillText("z / mm", 0, 0)
                        ctx.restore()
                    }
                }
                Label {
                    objectName: "rayTracePathSummary"
                    Layout.fillWidth: true
                    text: window.controller.hasResult
                          ? window.controller.displayPaths.length + " 条路径已绘制（最多显示 400 条）；仅首选波长。路径按几何模型传播。"
                          : "光线方向须沿 +z；横轴 x 与纵轴 z 均为 mm。"
                    color: window.themeColors.colorTextMuted
                    font.pointSize: window.themeColors.fontSizeSmall
                    wrapMode: Text.NoWrap
                    elide: Text.ElideRight
                }

                        }
                    }
                Item {
                    ColumnLayout {
                        anchors.fill: parent
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: window.themeColors.spaceMd
                            Label { Layout.preferredWidth: 90; text: "λ / nm"; color: window.themeColors.colorTextMuted; font.weight: window.themeColors.fontWeightEmphasis }
                            Label { Layout.fillWidth: true; text: "入射"; color: window.themeColors.colorTextMuted; font.weight: window.themeColors.fontWeightEmphasis }
                            Label { Layout.fillWidth: true; text: "透射"; color: window.themeColors.colorTextMuted; font.weight: window.themeColors.fontWeightEmphasis }
                            Label { Layout.fillWidth: true; text: "反射"; color: window.themeColors.colorTextMuted; font.weight: window.themeColors.fontWeightEmphasis }
                            Label { Layout.fillWidth: true; text: "吸收"; color: window.themeColors.colorTextMuted; font.weight: window.themeColors.fontWeightEmphasis }
                            Label { Layout.fillWidth: true; text: "遮挡"; color: window.themeColors.colorTextMuted; font.weight: window.themeColors.fontWeightEmphasis }
                        }
                        ScrollView {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            ScrollBar.vertical: ScrollBar {
                                policy: ScrollBar.AsNeeded
                                width: window.themeColors.spaceXs
                                background: Rectangle { color: "transparent" }
                                contentItem: Rectangle {
                                    implicitWidth: window.themeColors.spaceXs
                                    radius: height / 2
                                    color: window.themeColors.colorBorderStrong
                                }
                            }
                            ColumnLayout {
                                width: parent.width
                                spacing: 0
                                Repeater {
                                    model: window.controller.spectra
                                    delegate: RowLayout {
                                        id: spectrumRow
                                        required property var modelData
                                        Layout.fillWidth: true
                                        spacing: window.themeColors.spaceMd
                                        Label { Layout.preferredWidth: 90; text: Number(spectrumRow.modelData.wavelength_nm).toPrecision(6); color: window.themeColors.colorText }
                                        Label { Layout.fillWidth: true; text: Number(spectrumRow.modelData.incident_weight).toPrecision(5); color: window.themeColors.colorText }
                                        Label { Layout.fillWidth: true; text: Number(spectrumRow.modelData.transmitted_weight).toPrecision(5); color: window.themeColors.colorText }
                                        Label { Layout.fillWidth: true; text: Number(spectrumRow.modelData.reflected_weight).toPrecision(5); color: window.themeColors.colorText }
                                        Label { Layout.fillWidth: true; text: Number(spectrumRow.modelData.absorbed_weight).toPrecision(5); color: window.themeColors.colorText }
                                        Label { Layout.fillWidth: true; text: Number(spectrumRow.modelData.missed_weight).toPrecision(5); color: window.themeColors.colorText }
                                    }
                                }
                                Label {
                                    Layout.fillWidth: true
                                    visible: !window.controller.hasResult
                                    text: "结果尚未生成。运行后显示每个波长的相对能量与闭合分项。"
                                    color: window.themeColors.colorTextMuted
                                }
                            }
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "权重使用请求中的相对能量；探测器遗漏是透射能量的子集，不会再次加入光学能量平衡。"
                            color: window.themeColors.colorTextMuted
                            font.pointSize: window.themeColors.fontSizeSmall
                            wrapMode: Text.WordWrap
                        }
                    }
                }
                Item {
                    ColumnLayout {
                        anchors.fill: parent
                        RowLayout {
                            Layout.fillWidth: true
                            Label {
                                Layout.fillWidth: true
                                text: window.controller.hasDetector
                                      ? "探测器能量分箱 · " + window.controller.detectorInterpretation
                                      : "请求未定义探测器"
                                color: window.themeColors.colorTextMuted
                                wrapMode: Text.WordWrap
                            }
                        }
                        Canvas {
                            id: histogramCanvas
                            objectName: "rayTraceDetectorHistogramCanvas"
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            onPaint: {
                                const ctx = getContext("2d")
                                ctx.clearRect(0, 0, width, height)
                                ctx.fillStyle = window.themeColors.colorSurfaceRaised.toString()
                                ctx.fillRect(0, 0, width, height)
                                const bins = window.controller.detectorHistogram
                                if (!window.controller.hasDetector || bins.length === 0) {
                                    ctx.fillStyle = window.themeColors.colorTextMuted.toString()
                                    ctx.font = Math.max(12, window.themeColors.fontSizeBody * 1.33) + "px " + window.themeColors.fontFamily
                                    ctx.textAlign = "center"
                                    ctx.fillText(window.controller.hasResult
                                                  ? "请求没有探测器或该波长无探测器分箱"
                                                  : "运行含探测器的请求后显示几何射线能量", width / 2, height / 2)
                                    return
                                }
                                const margin = Math.max(16, window.themeColors.spaceMd)
                                const plotW = Math.max(1, width - 2 * margin)
                                const plotH = Math.max(1, height - 28)
                                const maxV = Math.max(1e-15, ...bins)
                                const gap = Math.min(2, plotW / bins.length * 0.15)
                                const bw = plotW / bins.length
                                ctx.strokeStyle = window.themeColors.colorBorderStrong.toString()
                                ctx.lineWidth = window.themeColors.borderWidth
                                ctx.beginPath(); ctx.moveTo(margin, plotH); ctx.lineTo(margin + plotW, plotH); ctx.stroke()
                                ctx.fillStyle = "#168C9C"
                                for (let i = 0; i < bins.length; ++i) {
                                    const h = Math.max(0, bins[i]) / maxV * (plotH - 8)
                                    ctx.fillRect(margin + i * bw + gap / 2, plotH - h,
                                                 Math.max(1, bw - gap), h)
                                }
                                const edges = window.controller.detectorBinEdges
                                ctx.fillStyle = window.themeColors.colorTextMuted.toString()
                                ctx.font = Math.max(10, window.themeColors.fontSizeSmall * 1.33) + "px " + window.themeColors.fontFamily
                                ctx.textAlign = "left"
                                if (edges.length > 1) {
                                    ctx.fillText(Number(edges[0]).toPrecision(4) + " mm", margin, height - 4)
                                    ctx.textAlign = "right"
                                    ctx.fillText(Number(edges[edges.length - 1]).toPrecision(4) + " mm", margin + plotW, height - 4)
                                }
                            }
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "点列和分箱受输入采样与 bin 数影响，不是波动光学点扩散函数（PSF）。"
                            color: window.themeColors.colorWarning
                            font.pointSize: window.themeColors.fontSizeSmall
                            wrapMode: Text.WordWrap
                        }
                    }
                }

            }
        }
        }

            ThemeControls.ThemeSurface {
                id: inputPanel
                objectName: "rayTraceInputPanel"
                Layout.preferredWidth: window.sidebarExpanded
                                       ? Math.max(270, Math.min(360, window.width * 0.31)) : 44
                Layout.minimumWidth: window.sidebarExpanded ? 270 : 44
                Layout.maximumWidth: window.sidebarExpanded ? 360 : 44
                Layout.fillHeight: true
                padding: window.sidebarExpanded ? window.themeColors.spaceMd : 0
                contentSpacing: window.themeColors.spaceSm

                RowLayout {
                    Layout.fillWidth: true
                    ThemeControls.ThemeLabel {
                        visible: window.sidebarExpanded
                        text: "几何输入"
                        heading: true
                    }
                    ThemeControls.ThemeDoubleChevron {
                        objectName: "rayTraceSidebarToggle"
                        text: window.sidebarExpanded ? "收起" : "›"
                        direction: window.sidebarExpanded ? "right" : "left"
                        preserveTextMetrics: true
                        Accessible.name: window.sidebarExpanded ? "收起几何设置侧栏" : "展开几何设置侧栏"
                        ToolTip.visible: hovered
                        ToolTip.text: Accessible.name + "（F4）"
                        onClicked: window.sidebarExpanded = !window.sidebarExpanded
                    }
                    Item { Layout.fillWidth: true }
                }
                ScrollView {
                    visible: window.sidebarExpanded
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    ScrollBar.horizontal: ScrollBar {
                        policy: ScrollBar.AsNeeded
                        height: window.themeColors.spaceXs
                        background: Rectangle { color: "transparent" }
                        contentItem: Rectangle {
                            implicitHeight: window.themeColors.spaceXs
                            radius: width / 2
                            color: window.themeColors.colorBorderStrong
                        }
                    }
                    ScrollBar.vertical: ScrollBar {
                        policy: ScrollBar.AsNeeded
                        width: window.themeColors.spaceXs
                        background: Rectangle { color: "transparent" }
                        contentItem: Rectangle {
                            implicitWidth: window.themeColors.spaceXs
                            radius: height / 2
                            color: window.themeColors.colorBorderStrong
                        }
                    }

                    ColumnLayout {
                        width: inputPanel.availableWidth
                        spacing: window.themeColors.spaceSm

                        ThemeControls.ThemeSection {
                            Layout.fillWidth: true
                            objectName: "rayTraceLightSection"
                            title: "光源"
                            expanded: true
                            ColumnLayout {
                                width: parent.width
                                spacing: window.themeColors.spaceXs
                                RowLayout {
                                    Layout.fillWidth: true
                                    Label { text: "模型"; color: window.themeColors.colorTextMuted }
                                    ThemeControls.ThemeComboBox {
                                        id: sourceModeCombo
                                        objectName: "rayTraceSourceMode"
                                        Layout.fillWidth: true
                                        model: ["point", "extended"]
                                        Accessible.name: "光源模型"
                                    }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Label { text: "光线数"; color: window.themeColors.colorTextMuted }
                                    ThemeControls.ThemeField {
                                        id: sourceCountField
                                        objectName: "rayTraceSourceCount"
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 64
                                        text: "5"
                                        inputMethodHints: Qt.ImhDigitsOnly
                                        accessibleLabel: "光线样本数"
                                    }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Label { text: "x0 / mm"; color: window.themeColors.colorTextMuted }
                                    ThemeControls.ThemeField {
                                        id: sourceXField
                                        objectName: "rayTraceSourceX"
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 64
                                        text: "0"
                                        accessibleLabel: "光源中心 x 坐标，毫米"
                                    }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Label { text: "z / mm"; color: window.themeColors.colorTextMuted }
                                    ThemeControls.ThemeField {
                                        id: sourceZField
                                        objectName: "rayTraceSourceZ"
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 64
                                        text: "-100"
                                        accessibleLabel: "光源 z 坐标，毫米"
                                    }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Label {
                                        Layout.minimumWidth: 88
                                        text: sourceModeCombo.currentText === "point" ? "半视场 / °" : "线宽 / mm"
                                        color: window.themeColors.colorTextMuted
                                    }
                                    ThemeControls.ThemeField {
                                        id: sourceExtentField
                                        objectName: "rayTraceSourceExtent"
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 64
                                        text: sourceModeCombo.currentText === "point" ? "3" : "10"
                                        accessibleLabel: sourceModeCombo.currentText === "point"
                                                        ? "点光源半视场角，度"
                                                        : "扩展线光源宽度，毫米"
                                    }
                                }
                                ThemeControls.ThemeButton {
                                    objectName: "rayTraceGenerateSourceButton"
                                    Layout.fillWidth: true
                                    text: "生成确定性光线样本"
                                    enabled: window.controller.hasRequest && !window.controller.busy
                                    onClicked: {
                                        const error = window.controller.generateSourceSamples(
                                            sourceModeCombo.currentText, sourceCountField.text,
                                            sourceXField.text, sourceZField.text,
                                            sourceExtentField.text)
                                        if (error.length > 0)
                                            sourceError.text = error
                                        else
                                            sourceError.text = ""
                                    }
                                }
                                Label {
                                    id: sourceError
                                    Layout.fillWidth: true
                                    visible: text.length > 0
                                    text: ""
                                    color: window.themeColors.colorError
                                    wrapMode: Text.WordWrap
                                }
                                Label {
                                    Layout.fillWidth: true
                                    text: sourceModeCombo.currentText === "point"
                                          ? "点光源：共点起始，按角度在 ±半视场内均匀取样。"
                                          : "扩展源：在 x 方向均匀取样，所有射线沿 +z 平行传播。"
                                    color: window.themeColors.colorTextMuted
                                    font.pointSize: window.themeColors.fontSizeSmall
                                    wrapMode: Text.WordWrap
                                }
                            }
                        }

                        ThemeControls.ThemeSection {
                            Layout.fillWidth: true
                            objectName: "rayTraceAssetSection"
                            title: "资产"
                            expanded: true
                            ColumnLayout {
                                width: parent.width
                                spacing: window.themeColors.spaceXs
                                RowLayout {
                                    Layout.fillWidth: true
                                    Label { text: "光学面"; color: window.themeColors.colorTextMuted }
                                    ThemeControls.ThemeComboBox {
                                        id: surfaceCombo
                                        objectName: "rayTraceSurfaceChoice"
                                        Layout.fillWidth: true
                                        model: window.controller.surfaceIds
                                        onActivated: window.controller.setSelectedSurfaceId(currentText)
                                        Component.onCompleted: if (count > 0) window.controller.setSelectedSurfaceId(currentText)
                                        onModelChanged: {
                                            if (count > 0 && currentIndex < 0) currentIndex = 0
                                            if (count === 0) window.controller.setSelectedSurfaceId("")
                                        }
                                        Accessible.name: "目标光学面"
                                    }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Label { text: "膜系面"; color: window.themeColors.colorTextMuted }
                                    ThemeControls.ThemeComboBox {
                                        id: filmSideCombo
                                        objectName: "rayTraceFilmDesignSide"
                                        Layout.fillWidth: true
                                        model: ["front", "back"]
                                        Accessible.name: "导入膜系的正面或背面"
                                    }
                                    ThemeControls.ThemeButton {
                                        text: "选择 .cfilm"
                                        enabled: !window.controller.busy && surfaceCombo.currentText.length > 0
                                        onClicked: filmOpenDialog.open()
                                    }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Label { text: "膜层"; color: window.themeColors.colorTextMuted }
                                    ThemeControls.ThemeComboBox {
                                        id: layerCombo
                                        objectName: "rayTraceLayerChoice"
                                        Layout.fillWidth: true
                                        model: window.controller.layerIds
                                        onActivated: window.controller.setSelectedLayerId(currentText)
                                        onModelChanged: if (count > 0 && currentIndex < 0) currentIndex = 0
                                        Accessible.name: "使用沉积厚度的膜层"
                                    }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    Label { text: "沉积步骤"; color: window.themeColors.colorTextMuted }
                                    ThemeControls.ThemeComboBox {
                                        id: stepCombo
                                        objectName: "rayTraceDepositionStepChoice"
                                        Layout.fillWidth: true
                                        model: window.controller.depositionStepChoices
                                        textRole: "label"
                                        valueRole: "step_id"
                                        onActivated: window.controller.setSelectedStepId(currentValue)
                                        Accessible.name: "逐层沉积厚度步骤"
                                    }
                                }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    Label {
                                        Layout.fillWidth: true
                                        text: "全局 XY→网格 XY"
                                        color: window.themeColors.colorTextMuted
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            Label { text: "转角 / °"; color: window.themeColors.colorTextMuted }
                                            ThemeControls.ThemeField {
                                                id: fieldRotation
                                                objectName: "rayTraceGridRotation"
                                                Layout.fillWidth: true
                                                Layout.minimumWidth: 64
                                                text: "0"
                                                accessibleLabel: "坐标变换转角，度"
                                            }
                                        }
                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            Label { text: "平移 x,y / mm"; color: window.themeColors.colorTextMuted }
                                            ThemeControls.ThemeField {
                                                id: fieldTranslation
                                                objectName: "rayTraceGridTranslation"
                                                Layout.fillWidth: true
                                                Layout.minimumWidth: 64
                                                text: "0, 0"
                                                accessibleLabel: "坐标变换 x,y 平移，毫米"
                                            }
                                        }
                                    }
                                }
                                ThemeControls.ThemeButton {
                                    objectName: "rayTraceBindLayerFieldButton"
                                    Layout.fillWidth: true
                                    text: "将选中膜层绑定到逐层厚度"
                                    enabled: !window.controller.busy && window.controller.hasLayerAsset
                                             && layerCombo.currentText.length > 0
                                             && stepCombo.currentValue !== undefined
                                    onClicked: {
                                        const error = window.controller.bindLayerToDepositionStep(
                                            layerCombo.currentText, stepCombo.currentValue,
                                            fieldRotation.text, fieldTranslation.text)
                                        if (error.length > 0)
                                            bindingError.text = error
                                        else
                                            bindingError.text = ""
                                    }
                                }
                                Label {
                                    id: bindingError
                                    Layout.fillWidth: true
                                    visible: text.length > 0
                                    text: ""
                                    color: window.themeColors.colorError
                                    wrapMode: Text.WordWrap
                                }
                                Label {
                                    Layout.fillWidth: true
                                    text: window.controller.layerAssetSummary
                                    color: window.controller.hasLayerAsset
                                           ? window.themeColors.colorTextMuted : window.themeColors.colorWarning
                                    wrapMode: Text.WordWrap
                                    font.pointSize: window.themeColors.fontSizeSmall
                                }
                                ThemeControls.ThemeButton {
                                    objectName: "rayTraceThinFilmAppearanceButton"
                                    Layout.fillWidth: true
                                    text: "薄膜外观"
                                    enabled: window.controller.canOpenAppearance
                                    onClicked: window.controller.openAppearance()
                                }
                                Label {
                                    Layout.fillWidth: true
                                    objectName: "rayTraceAppearanceAvailability"
                                    text: window.controller.appearanceAvailabilityMessage
                                    color: window.controller.canOpenAppearance
                                           ? window.themeColors.colorTextMuted : window.themeColors.colorWarning
                                    wrapMode: Text.WordWrap
                                    font.pointSize: window.themeColors.fontSizeSmall
                                }
                            }
                        }

                        ThemeControls.ThemeSection {
                            Layout.fillWidth: true
                            objectName: "rayTraceComputeSection"
                            title: "计算"
                            expanded: true
                            ColumnLayout {
                                Layout.fillWidth: true
                                Label {
                                    Layout.fillWidth: true
                                    text: window.controller.appearanceRunning
                                          ? "本地光谱渲染正在运行。"
                                          : window.controller.busy ? "几何追迹正在运行。"
                                                          : "几何路径追迹；不提供 GPU 渲染选项。"
                                    color: window.themeColors.colorTextMuted
                                    wrapMode: Text.WordWrap
                                }
                                ProgressBar {
                                    objectName: "rayTraceProgressBar"
                                    Layout.fillWidth: true
                                    visible: window.controller.busy || window.controller.appearanceRunning
                                    indeterminate: window.controller.appearanceRunning
                                    from: 0
                                    to: 1
                                    value: window.controller.progress
                                }
                            }
                        }
                        ThemeControls.ThemeSection {
                            Layout.fillWidth: true
                            objectName: "rayTraceAdvancedJsonSection"
                            title: "高级 JSON"
                            expanded: false
                            ColumnLayout {
                                width: parent.width
                                spacing: window.themeColors.spaceXs
                                Label {
                                    Layout.fillWidth: true
                                    text: "请求定义有序光学面、n/k、谱线、偏振、探测器和坐标。可打开任意兼容请求；膜系按钮会把现有设计采样后写入所选光学面。"
                                    color: window.themeColors.colorTextMuted
                                    wrapMode: Text.WordWrap
                                    font.pointSize: window.themeColors.fontSizeSmall
                                }
                                ScrollView {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 335
                                    Layout.minimumHeight: 230
                                    clip: true
                                    ScrollBar.horizontal: ScrollBar {
                                        policy: ScrollBar.AsNeeded
                                        height: window.themeColors.spaceXs
                                        background: Rectangle { color: "transparent" }
                                        contentItem: Rectangle {
                                            implicitHeight: window.themeColors.spaceXs
                                            radius: width / 2
                                            color: window.themeColors.colorBorderStrong
                                        }
                                    }
                                    ScrollBar.vertical: ScrollBar {
                                        policy: ScrollBar.AsNeeded
                                        width: window.themeColors.spaceXs
                                        background: Rectangle { color: "transparent" }
                                        contentItem: Rectangle {
                                            implicitWidth: window.themeColors.spaceXs
                                            radius: height / 2
                                            color: window.themeColors.colorBorderStrong
                                        }
                                    }
                                    TextArea {
                                        id: requestEditor
                                        objectName: "rayTraceRequestEditor"
                                        text: window.controller.requestText
                                        enabled: !window.controller.busy
                                        selectByMouse: true
                                        wrapMode: TextEdit.NoWrap
                                        font.family: window.themeColors.fontFamily
                                        font.pointSize: window.themeColors.fontSizeSmall
                                        color: window.themeColors.colorText
                                        selectionColor: window.themeColors.colorSelection
                                        background: Rectangle {
                                            color: window.themeColors.colorSurfaceRaised
                                            border.width: window.themeColors.borderWidth
                                            border.color: requestEditor.activeFocus
                                                           ? window.themeColors.colorFocusRing
                                                           : window.themeColors.colorBorder
                                            radius: window.themeColors.radiusSm
                                        }
                                        onTextEdited: window.controller.setRequestText(text)
                                    }
                                }
                                Label {
                                    Layout.fillWidth: true
                                    visible: window.controller.validationMessage.length > 0
                                    text: window.controller.validationMessage
                                    color: window.themeColors.colorWarning
                                    wrapMode: Text.WordWrap
                                    font.pointSize: window.themeColors.fontSizeSmall
                                }
                            }
                        }
                    }
                }
            }

            }

        Loader {
            id: appearanceWorkspaceLoader
            objectName: "rayTraceAppearanceWorkspaceLoader"
            anchors.fill: parent
            active: window.controller.appearanceController !== null
                    && window.controller.appearanceController !== undefined
            onLoaded: window.selectedCategory = "asset"
            sourceComponent: Component {
                AppearanceWorkspace {
                    objectName: "rayTraceAppearanceWorkspace"
                    anchors.fill: parent
                    controller: window.controller.appearanceController
                    selectedCategory: window.selectedCategory
                    visible: window.controller.workspaceMode === "appearance"
                    showAssetSelector: true
                    showModeSelector: true
                    activeWorkspace: window.controller.workspaceMode === "appearance"
                    externalBusy: window.controller.geometricBusy
                    onGeometricModeRequested: window.controller.setWorkspaceMode("geometric")
                    onCategorySelectionRequested: function(category) {
                        window.selectedCategory = category
                    }
                    focus: true
                    activeFocusOnTab: true
                    onSelectAssetRequested: layerAssetDialog.open()
                }
            }
        }

        RowLayout {
            id: appearanceEmptyState
            objectName: "rayTraceAppearanceEmptyState"
            anchors.fill: parent
            spacing: window.themeColors.spaceMd
            focus: true
            activeFocusOnTab: true
            visible: window.controller.workspaceMode === "appearance"
                     && (window.controller.appearanceController === null
                         || window.controller.appearanceController === undefined)

            ThemeControls.ThemeSurface {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumWidth: 260
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    RowLayout {
                        Layout.fillWidth: true
                        ThemeControls.ThemeLabel {
                            Layout.fillWidth: true
                            text: "薄膜外观"
                            heading: true
                        }
                        ThemeControls.ThemeButton {
                            id: appearanceChooseAssetButton
                            objectName: "rayTraceAppearanceChooseAssetButton"
                            text: "选择逐层资产"
                            primary: true
                            enabled: !window.controller.geometricBusy && !window.controller.appearanceRunning
                            onClicked: layerAssetDialog.open()
                        }
                    }
                    ThemeControls.ThemeLabel {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        text: "选择沉积资产后，可查看相机几何预览并运行本地光谱渲染。"
                        muted: true
                        wrapMode: Text.WordWrap
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                    }
                }
            }

            ThemeControls.ThemeSurface {
                id: appearanceWorkflowSidebar
                objectName: "rayTraceAppearanceWorkflowSidebar"
                Layout.preferredWidth: window.appearanceSidebarExpanded
                                       ? Math.max(300, Math.min(370, workspaceArea.width * 0.34)) : 44
                Layout.minimumWidth: window.appearanceSidebarExpanded ? 300 : 44
                Layout.maximumWidth: window.appearanceSidebarExpanded ? 370 : 44
                Layout.fillHeight: true
                padding: window.appearanceSidebarExpanded ? window.themeColors.spaceSm : 0
                contentSpacing: window.themeColors.spaceSm
                RowLayout {
                    Layout.fillWidth: true
                    ThemeControls.ThemeLabel {
                        visible: window.appearanceSidebarExpanded
                        text: "外观设置"
                        heading: true
                    }
                    ThemeControls.ThemeDoubleChevron {
                        objectName: "rayTraceAppearanceSidebarToggle"
                        text: window.appearanceSidebarExpanded ? "收起" : "‹"
                        direction: window.appearanceSidebarExpanded ? "right" : "left"
                        preserveTextMetrics: true
                        Accessible.name: window.appearanceSidebarExpanded
                                        ? "收起外观工作流侧栏" : "展开外观工作流侧栏"
                        ToolTip.visible: hovered
                        ToolTip.text: Accessible.name + "（F4）"
                        onClicked: window.appearanceSidebarExpanded = !window.appearanceSidebarExpanded
                    }
                    Item { Layout.fillWidth: true }
                }
                GridLayout {
                    id: rayTraceEmptyCategoryNavigation
                    objectName: "rayTraceEmptyCategoryNavigation"
                    visible: window.appearanceSidebarExpanded
                    Layout.fillWidth: true
                    columns: 2
                    rowSpacing: window.themeColors.spaceXs
                    columnSpacing: window.themeColors.spaceXs
                    ThemeControls.ThemeButton {
                        objectName: "rayTraceEmptyCategory_asset"
                        Layout.fillWidth: true
                        text: "资产"
                        checkable: true
                        checked: window.selectedCategory === "asset"
                        Accessible.name: "资产工作流类别"
                        onClicked: window.selectedCategory = "asset"
                    }
                    ThemeControls.ThemeButton {
                        objectName: "rayTraceEmptyCategory_light"
                        Layout.fillWidth: true
                        text: "光源"
                        checkable: true
                        checked: window.selectedCategory === "light"
                        Accessible.name: "光源工作流类别"
                        onClicked: window.selectedCategory = "light"
                    }
                    ThemeControls.ThemeButton {
                        objectName: "rayTraceEmptyCategory_camera"
                        Layout.fillWidth: true
                        text: "相机"
                        checkable: true
                        checked: window.selectedCategory === "camera"
                        Accessible.name: "相机工作流类别"
                        onClicked: window.selectedCategory = "camera"
                    }
                    ThemeControls.ThemeButton {
                        objectName: "rayTraceEmptyCategory_compute"
                        Layout.fillWidth: true
                        text: "计算"
                        checkable: true
                        checked: window.selectedCategory === "compute"
                        Accessible.name: "计算工作流类别"
                        onClicked: window.selectedCategory = "compute"
                    }
                }
                StackLayout {
                    id: rayTraceEmptyCategoryStack
                    objectName: "rayTraceEmptyCategoryStack"
                    visible: window.appearanceSidebarExpanded
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumHeight: 0
                    currentIndex: window.selectedCategory === "asset" ? 0
                                  : window.selectedCategory === "light" ? 1
                                  : window.selectedCategory === "camera" ? 2 : 3
                    ColumnLayout {
                        objectName: "rayTraceEmptyCategoryPanel_asset"
                        Layout.fillWidth: true
                        spacing: window.themeColors.spaceXs
                        ThemeControls.ThemeLabel {
                            Layout.fillWidth: true
                            text: "选择逐层沉积资产。膜系与基片信息由资产关联提供。"
                            muted: true
                            wrapMode: Text.WordWrap
                        }
                        ThemeControls.ThemeButton {
                            objectName: "rayTraceAppearanceSidebarAssetButton"
                            Layout.fillWidth: true
                            text: "选择逐层资产"
                            primary: true
                            enabled: !window.controller.geometricBusy && !window.controller.appearanceRunning
                            onClicked: layerAssetDialog.open()
                        }
                        Item { Layout.fillHeight: true }
                    }
                    ColumnLayout {
                        objectName: "rayTraceEmptyCategoryPanel_light"
                        Layout.fillWidth: true
                        spacing: window.themeColors.spaceXs
                        ThemeControls.ThemeLabel {
                            Layout.fillWidth: true
                            text: "支持矩形面光源与均匀环境光谱，可导入标量 SPD。选择资产后即可配置。"
                            muted: true
                            wrapMode: Text.WordWrap
                        }
                        Item { Layout.fillHeight: true }
                    }
                    ColumnLayout {
                        objectName: "rayTraceEmptyCategoryPanel_camera"
                        Layout.fillWidth: true
                        spacing: window.themeColors.spaceXs
                        ThemeControls.ThemeLabel {
                            Layout.fillWidth: true
                            text: "相机位置、目标与上方向使用局部毫米坐标；中央 3D 视图仅展示几何预览。"
                            muted: true
                            wrapMode: Text.WordWrap
                        }
                        Item { Layout.fillHeight: true }
                    }
                    ColumnLayout {
                        objectName: "rayTraceEmptyCategoryPanel_compute"
                        Layout.fillWidth: true
                        spacing: window.themeColors.spaceXs
                        ThemeControls.ThemeLabel {
                            Layout.fillWidth: true
                            text: "本地光谱渲染可设置分辨率、每像素采样数、随机种子和波长采样，并支持按波长取消。"
                            muted: true
                            wrapMode: Text.WordWrap
                        }
                        Item { Layout.fillHeight: true }
                    }
                }
            }
        }
        }
    }
}
