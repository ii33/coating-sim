import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "." as LocalComponents
import "theme_controls" as ThemeControls

LocalComponents.AppWindow {
    id: window
    // The bridge is installed on QQmlContext by both launcher entry points.
    // qmllint disable unqualified
    readonly property var editorBridge: bridge
    // qmllint enable unqualified
    property var projectNavigation: null
    property var projectResources: null
    property bool projectPendingTextEdited: false
    property bool projectCloseAuthorized: false
    LocalComponents.ProjectPendingEdits { editorWindow: window }
    Connections {
        target: typeof Theme !== "undefined" ? Theme : null
        function onSnapshotChanged() { preview.requestPaint(); pagePane.scheduleFocusedSourceFieldReveal() }
    }
    title: "蒸发源 — " + window.editorBridge.displayName
    caption: title + (window.editorBridge.dirty ? " *" : "")
    statusText: bridge.valid ? (bridge.status || "当前模型有效，可保存。") : bridge.error
    statusSeverity: bridge.valid ? "info" : "error"
    statusDetail: bridge.valid ? "" : bridge.error
    minimumWidth: 640
    minimumHeight: 540
    property bool allowClose: false
    property bool saveThenContinue: false
    property var materialLibraryDialogInstance: null
    property var materialLibraryDialogIncubator: null
    property bool materialLibraryOpening: false
    property bool materialLibraryOpenRequested: false
    property bool materialReferenceOpenRequested: false
    property bool sourceReferenceOpenRequested: false
    onClosing: close => {
        materialLibraryOpenRequested = false
        materialReferenceOpenRequested = false
        sourceReferenceOpenRequested = false
        if (window.projectCloseAuthorized) {
            window.projectCloseAuthorized = false
            close.accepted = true
        } else if (window.projectResources !== null) {
            close.accepted = window.projectResources.requestClose()
        } else if (!allowClose) close.accepted = window.editorBridge.requestClose()
        if (close.accepted && materialLibraryDialogIncubator) {
            const incubator = materialLibraryDialogIncubator
            materialLibraryDialogIncubator = null
            materialLibraryOpening = false
            incubator.clear()
        }
    }
    function commitPendingEdits() {
        if (contentItem) contentItem.forceActiveFocus()
    }
    function finishMaterialLibraryDialogIncubation(incubator) {
        if (materialLibraryDialogIncubator !== incubator) return
        if (incubator.status === Component.Ready) {
            materialLibraryDialogInstance = incubator.object
            materialLibraryDialogIncubator = null
            materialLibraryOpening = false
            if (materialLibraryOpenRequested && materialLibraryDialogInstance) {
                materialLibraryOpenRequested = false
                materialLibraryDialogInstance.open()
            }
        } else if (incubator.status === Component.Error) {
            materialLibraryDialogIncubator = null
            materialLibraryOpening = false
            materialLibraryOpenRequested = false
            console.error("Unable to create Source material library dialog")
        }
    }
    function openMaterialLibrary() {
        if (materialLibraryDialogInstance) {
            materialLibraryDialogInstance.open()
            return
        }
        materialLibraryOpenRequested = true
        if (materialLibraryDialogIncubator) return
        materialLibraryOpening = true
        const incubator = materialLibraryDialogComponent.incubateObject(
                    window, {}, Qt.Asynchronous)
        materialLibraryDialogIncubator = incubator
        if (!incubator) {
            materialLibraryOpening = false
            materialLibraryOpenRequested = false
            console.error("Unable to start Source material library dialog creation")
            return
        }
        incubator.onStatusChanged = function() {
            window.finishMaterialLibraryDialogIncubation(incubator)
        }
        finishMaterialLibraryDialogIncubation(incubator)
    }
    function openMaterialReferenceDialog() {
        if (materialReferenceLoader.item) {
            materialReferenceLoader.item.open()
            return
        }
        materialReferenceOpenRequested = true
        materialReferenceLoader.active = true
    }
    function openSourceReferenceDialog() {
        if (sourceReferenceLoader.item) {
            sourceReferenceLoader.item.open()
            return
        }
        sourceReferenceOpenRequested = true
        sourceReferenceLoader.active = true
    }
    function saveAndContinue() {
        if (bridge.pendingAction === "close" && bridge.materialLibrary.dirty && !bridge.materialLibrary.save()) return
        if (bridge.dirty) {
            if (bridge.currentPath.length === 0) {
                discardDialog.close()
                saveThenContinue = true
                saveDialog.open()
                return
            }
            if (!bridge.save()) return
        }
        discardDialog.close()
        bridge.confirmDiscard(true)
    }
    function finishSaveAs(saved) {
        if (bridge.pendingSaveAs.length > 0) return
        if (window.saveThenContinue) {
            window.saveThenContinue = false
            if (saved) bridge.confirmDiscard(true)
            else discardDialog.open()
        }
    }

    Connections {
        target: window
        function onActiveFocusItemChanged() { pagePane.scheduleFocusedSourceFieldReveal() }
    }

    Connections {
        target: bridge
        // SourceBridge mirrors changed through sourceDataChanged, so this single
        // repaint covers edits and document replacement without duplicate work.
        function onSourceDataChanged() { preview.requestPaint() }
        function onDiscardRequested() { discardDialog.open() }
        function onCloseApproved() { window.allowClose = true; window.close() }
        function onOverwriteRequested(url) { overwriteDialog.targetPath = url.toString(); overwriteDialog.open() }
    }

    Loader {
        id: sourceReferenceLoader
        asynchronous: true
        active: false
        sourceComponent: Component {
            LocalComponents.SourceReferenceDialog {
                objectName: "sourceReferenceDialog"
                calculator: bridge.referenceCalculator
            }
        }
        onLoaded: {
            if (!window.sourceReferenceOpenRequested) return
            window.sourceReferenceOpenRequested = false
            item.open()
        }
        onStatusChanged: {
            if (status !== Loader.Error) return
            window.sourceReferenceOpenRequested = false
            active = false
            console.error("Unable to create Source reference dialog")
        }
    }
    Loader {
        id: materialReferenceLoader
        asynchronous: true
        active: false
        sourceComponent: Component {
            LocalComponents.MaterialReferenceDialog {
                objectName: "materialReferenceDialog"
                sections: visible ? bridge.materialReferenceSections : []
            }
        }
        onLoaded: {
            if (!window.materialReferenceOpenRequested) return
            window.materialReferenceOpenRequested = false
            item.open()
        }
        onStatusChanged: {
            if (status !== Loader.Error) return
            window.materialReferenceOpenRequested = false
            active = false
            console.error("Unable to create Source material reference dialog")
        }
    }
    Component {
        id: materialLibraryDialogComponent
        LocalComponents.MaterialLibraryDialog {
            objectName: "materialLibraryDialog"
            library: bridge.materialLibrary
        }
    }
    LocalComponents.ThemePageDialog {
        id: discardDialog
        objectName: "sourceDiscardDialog"
        title: "有尚未保存的修改"
        modal: true
        anchors.centerIn: parent
        width: 470
        height: 205
        closePolicy: Popup.NoAutoClose
        contentItem: ThemeControls.ThemeLabel { text: "保存后继续，或明确放弃修改。取消会保留当前源设计和材料修改。"; wrapMode: Text.Wrap }
        footer: DialogButtonBox {
            ThemeControls.ThemeButton { objectName: "sourceSaveAndContinueButton"; text: "保存并继续"; enabled: bridge.valid && (bridge.pendingAction !== "close" || !bridge.materialLibrary.dirty || bridge.materialLibrary.valid); onClicked: window.saveAndContinue() }
            ThemeControls.ThemeButton { objectName: "sourceDiscardChangesButton"; text: "放弃并继续"; onClicked: { discardDialog.close(); bridge.confirmDiscard(true) } }
            ThemeControls.ThemeButton { objectName: "sourceCancelDiscardButton"; text: "取消"; onClicked: { discardDialog.close(); bridge.confirmDiscard(false) } }
        }
    }

    Connections {
        target: bridge
        function onReloadRequested() { reloadDialog.open() }
        function onDocumentReplaceRequested() { documentReplaceDialog.open() }
    }
    LocalComponents.ThemePageDialog {
        id: documentReplaceDialog
        title: "替换未保存的设计？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.Cancel
        onAccepted: window.editorBridge.confirmDocumentReplace(true)
        onRejected: window.editorBridge.confirmDocumentReplace(false)
        ThemeControls.ThemeLabel { text: "当前源设计有未保存修改。继续会放弃这些修改并打开或新建设计。"; width: 420; wrapMode: Text.Wrap }
    }
    LocalComponents.ThemePageDialog {
        id: reloadDialog
        objectName: "reloadConfirmDialog"
        title: "重新载入文件？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.Cancel
        onAccepted: bridge.confirmReload(true)
        onRejected: bridge.confirmReload(false)
        ThemeControls.ThemeLabel { text: "刷新将放弃当前设计的未保存修改，重新读取同一文件。取消会保留当前修改。"; width: 420; wrapMode: Text.Wrap }
    }

    FileDialog {
        id: openDialog
        currentFolder: bridge.defaultDirectory
        title: "打开蒸发源"
        fileMode: FileDialog.OpenFile
        nameFilters: ["蒸发源文件 (*.csrc)"]
        onAccepted: bridge.openPath(selectedFile)
    }
    FileDialog {
        id: saveDialog
        objectName: "sourceSaveDialog"
        currentFolder: bridge.defaultDirectory
        title: "蒸发源另存为"
        fileMode: FileDialog.SaveFile
        options: FileDialog.DontConfirmOverwrite
        nameFilters: ["蒸发源文件 (*.csrc)"]
        defaultSuffix: "csrc"
        onAccepted: window.finishSaveAs(bridge.saveAsPath(selectedFile))
        onRejected: { bridge.confirmSaveAs(false); window.saveThenContinue = false; bridge.confirmDiscard(false) }
    }
    LocalComponents.ThemePageDialog {
        id: overwriteDialog
        objectName: "sourceOverwriteDialog"
        property string targetPath: ""
        title: "覆盖已有蒸发源？"
        modal: true
        anchors.centerIn: parent
        width: Math.min(500, window.width - 32)
        standardButtons: Dialog.Yes | Dialog.No
        onAccepted: window.finishSaveAs(bridge.confirmSaveAs(true))
        onRejected: { bridge.confirmSaveAs(false); window.finishSaveAs(false) }
        contentItem: ThemeControls.ThemeLabel { text: "确认以当前设计覆盖此文件？\n" + overwriteDialog.targetPath; wrapMode: Text.WrapAnywhere }
    }

    Pane {
        id: pagePane
        objectName: "sourcePagePane"
        anchors.fill: parent
        padding: 0
        background: null
        palette: window.palette

        TextMetrics {
            id: sourcePanelMetrics
            font.family: typeof Theme !== "undefined" ? Theme.snapshot.fontFamily : "sans-serif"
            font.pointSize: typeof Theme !== "undefined" ? Theme.snapshot.fontSizeBody : 9
            text: "设备最大有效沉积速率 / nm/s"
        }
        TextMetrics {
            id: previewPanelMetrics
            font.family: typeof Theme !== "undefined" ? Theme.snapshot.fontFamily : "sans-serif"
            font.pointSize: typeof Theme !== "undefined" ? Theme.snapshot.fontSizeBody : 9
            text: "源 +Z 俯视布局 XY"
        }
        TextMetrics {
            id: previewRateMetrics
            font.family: typeof Theme !== "undefined" ? Theme.snapshot.fontFamily : "sans-serif"
            font.pointSize: typeof Theme !== "undefined" ? Theme.snapshot.fontSizeBody : 9
            text: "-123.456"
        }
        TextMetrics {
            id: previewRateLabelMetrics
            font.family: typeof Theme !== "undefined" ? Theme.snapshot.fontFamily : "sans-serif"
            font.pointSize: typeof Theme !== "undefined" ? Theme.snapshot.fontSizeBody : 9
            text: "预览速率 / Å/s"
        }
        TextMetrics {
            id: sourceGridLabelMetrics
            font.family: typeof Theme !== "undefined" ? Theme.snapshot.fontFamily : "sans-serif"
            font.pointSize: typeof Theme !== "undefined" ? Theme.snapshot.fontSizeBody : 9
            text: "矩形方向 / °"
        }
        TextMetrics {
            id: sourceKindLabelMetrics
            font.family: typeof Theme !== "undefined" ? Theme.snapshot.fontFamily : "sans-serif"
            font.pointSize: typeof Theme !== "undefined" ? Theme.snapshot.fontSizeBody : 9
            text: "源类型"
        }
        // Match WindowChrome's caption font and keep this page's controls
        // below both the caption and the window-control row at large scales.
        TextMetrics {
            id: sourceWindowCaptionMetrics
            objectName: "sourceWindowCaptionMetrics"
            font.family: typeof Theme !== "undefined" ? Theme.snapshot.fontFamily : "sans-serif"
            font.pointSize: typeof Theme !== "undefined" ? Theme.snapshot.fontSizeBody : 9
            font.weight: typeof Theme !== "undefined" ? Theme.snapshot.fontWeightEmphasis : Font.Bold
            text: window.caption
        }
        readonly property real spaceXs: typeof Theme !== "undefined" ? Theme.snapshot.spaceXs : 4
        readonly property real spaceSm: typeof Theme !== "undefined" ? Theme.snapshot.spaceSm : 8
        readonly property real spaceLg: typeof Theme !== "undefined" ? Theme.snapshot.spaceLg : 14
        readonly property real spaceMd: typeof Theme !== "undefined" ? Theme.snapshot.spaceMd : 12
        readonly property real headerControlHeight: typeof Theme !== "undefined"
                                                      ? Theme.snapshot.controlHeightSm : 30
        readonly property real headerCaptionBottom: sourceWindowCaptionMetrics.height + spaceSm
        readonly property real headerControlsBottom: headerControlHeight + spaceXs + 4
        readonly property real actionTopY: Math.max(headerCaptionBottom, headerControlsBottom) + spaceSm
        readonly property real minimumFormPanelWidth:
            Math.max(320, sourcePanelMetrics.width * 0.55 + 128)
        readonly property real minimumPreviewPanelWidth:
            Math.max(320, previewPanelMetrics.width * 1.45 + 140)
        readonly property real minimumSourceFieldColumnWidth:
            Math.max(160, sourceGridLabelMetrics.width * 0.65 + 112)
        property bool focusRevealQueued: false

        function scheduleFocusedSourceFieldReveal() {
            if (focusRevealQueued) return
            focusRevealQueued = true
            Qt.callLater(function() {
                pagePane.focusRevealQueued = false
                const focused = window.activeFocusItem
                if (!focused || sourceWorkspaceViewport.height <= 0) return
                let ancestor = focused
                let belongsToWorkspace = false
                let belongsToSourceForm = false
                while (ancestor) {
                    if (ancestor === sourceFormLoader.item) {
                        belongsToSourceForm = true
                    }
                    if (ancestor === sourceWorkspace) {
                        belongsToWorkspace = true
                        break
                    }
                    ancestor = ancestor.parent
                }
                if (!belongsToWorkspace) return
                if (belongsToSourceForm && sourceFormScroll.height > 0) {
                    const point = focused.mapToItem(sourceFormScroll.contentItem, 0, 0)
                    const top = point.y
                    const bottom = top + Math.max(1, focused.height)
                    const viewTop = sourceFormScroll.contentY
                    const viewBottom = viewTop + sourceFormScroll.height
                    if (top < viewTop) sourceFormScroll.contentY = Math.max(0, top)
                    else if (bottom > viewBottom) {
                        sourceFormScroll.contentY = Math.max(0, Math.min(
                                    sourceFormScroll.contentHeight - sourceFormScroll.height,
                                    bottom - sourceFormScroll.height))
                    }
                }
                const workspacePoint = focused.mapToItem(sourceWorkspaceViewport.contentItem, 0, 0)
                const workspaceTop = workspacePoint.y
                const workspaceBottom = workspaceTop + Math.max(1, focused.height)
                const viewportTop = sourceWorkspaceViewport.contentY
                const viewportBottom = viewportTop + sourceWorkspaceViewport.height
                if (workspaceTop < viewportTop) {
                    sourceWorkspaceViewport.contentY = Math.max(0, workspaceTop)
                } else if (workspaceBottom > viewportBottom) {
                    sourceWorkspaceViewport.contentY = Math.max(0, Math.min(
                                sourceWorkspaceViewport.contentHeight - sourceWorkspaceViewport.height,
                                workspaceBottom - sourceWorkspaceViewport.height))
                }
            })
        }

        Column {
            id: actionArea
            objectName: "sourceActionArea"
            x: 18
            y: window.contentTopMargin
            width: Math.max(0, parent.width - 36)
            spacing: pagePane.spaceSm
            height: actions.height + (fileNameRow.visible ? spacing + fileNameRow.implicitHeight : 0)

            LocalComponents.ResponsiveOperationBar {
                id: actions
                objectName: "sourceOperationBar"
                Accessible.name: "操作栏"
                width: parent.width
                spacing: pagePane.spaceSm
                height: childrenRect.height
            LocalComponents.ProjectModuleSelector {
                navigation: window.projectNavigation
                currentModuleKey: "source"
            }
            LocalComponents.ProjectResourceControls {
                projectResources: window.projectResources
                informationProvider: window.projectResources !== null ? window.projectResources : window.editorBridge.informationController
                saveButtonObjectName: window.projectResources !== null ? "sourceSaveButton" : "projectResourceSaveButton"
            }
            ThemeControls.ThemeButton { objectName: "materialLibraryButton"; text: window.materialLibraryOpening ? "载入中…" : "材料库"; visible: bridge.emissionFields.kind !== "uniform"; onClicked: { bridge.materialLibrary.prepareForSource(); window.openMaterialLibrary() } }
            ThemeControls.ThemeButton { text: "新建"; visible: window.projectResources === null; onClicked: bridge.newDocument() }
            ThemeControls.ThemeButton { text: "打开"; visible: window.projectResources === null; onClicked: openDialog.open() }
            ThemeControls.ThemeButton {
                objectName: "reloadCurrentFile"
                text: "刷新"
                Accessible.name: "重新载入当前设计文件"
                enabled: bridge.canReload
                ToolTip.visible: hovered
                ToolTip.text: "从原路径重新读取设计；未保存修改需确认放弃"
                onClicked: bridge.reloadCurrent()
            }
            ThemeControls.ThemeButton {
                objectName: window.projectResources === null ? "sourceSaveButton" : "sourceGlobalSaveButton"
                visible: window.projectResources === null
                text: "保存"
                enabled: bridge.valid
                onClicked: {
                    if (window.projectResources !== null) window.projectResources.saveCurrent()
                    else if (bridge.currentPath.length === 0) saveDialog.open()
                    else bridge.save()
                }
            }
            ThemeControls.ThemeButton { text: "另存为"; visible: window.projectResources === null; enabled: bridge.valid; onClicked: saveDialog.open() }
            }
            RowLayout {
                id: fileNameRow
                visible: window.projectResources === null
                width: parent.width
                ThemeControls.ThemeLabel {
                    objectName: "sourceResourceDisplayName"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    text: (window.editorBridge.dirty ? "• " : "") + window.editorBridge.displayName
                    textFormat: Text.PlainText
                    HoverHandler { id: sourceNameHover }
                    ToolTip.visible: sourceNameHover.hovered
                    ToolTip.text: window.editorBridge.currentPath
                    elide: Text.ElideMiddle
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: pagePane.spaceSm
                    wrapMode: Text.NoWrap
                }
            }
        }


        Flickable {
            id: sourceWorkspaceViewport
            objectName: "sourceWorkspaceViewport"
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: actionArea.bottom
            anchors.bottom: parent.bottom
            anchors.leftMargin: 18
            anchors.rightMargin: 18
            anchors.topMargin: pagePane.spaceSm
            anchors.bottomMargin: 12
            contentWidth: width
            contentHeight: sourceWorkspace.height
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            interactive: sourceWorkspace.height > height + 1
            ScrollBar.vertical: ScrollBar {
                palette: window.palette
                visible: sourceWorkspaceViewport.interactive
            }

            GridLayout {
                id: sourceWorkspace
                objectName: "sourceWorkspace"
                width: sourceWorkspaceViewport.width
                property real formMinimumWidth: pagePane.minimumFormPanelWidth
                property real previewMinimumWidth: pagePane.minimumPreviewPanelWidth
                property bool stacked: width < formMinimumWidth + previewMinimumWidth + columnSpacing
                columns: stacked ? 1 : 2
                columnSpacing: pagePane.spaceLg
                rowSpacing: pagePane.spaceMd
                property real minimumPreviewHeight:
                    Math.max(220, previewTitle.implicitHeight + previewControls.implicitHeight
                             + previewNote.implicitHeight + 190)
                property real stackedFormHeight: Math.max(220, sourceWorkspaceViewport.height * 0.55)
                property real stackedPreviewHeight:
                    Math.max(minimumPreviewHeight, sourceWorkspaceViewport.height * 0.45)
                height: Math.max(sourceWorkspaceViewport.height,
                                 stacked
                                 ? stackedFormHeight + rowSpacing + stackedPreviewHeight
                                 : minimumPreviewHeight)

            ThemeControls.ThemeSurface {
                id: sourceFormSurface
                objectName: "sourceFormSurface"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumWidth: sourceWorkspace.stacked ? 0 : sourceWorkspace.formMinimumWidth
                Layout.preferredWidth: sourceWorkspace.stacked
                    ? sourceWorkspace.width
                    : Math.min(440, Math.max(sourceWorkspace.formMinimumWidth, sourceWorkspace.width * 0.37))
                Layout.minimumHeight: sourceWorkspace.stacked ? 220 : 0
                Layout.preferredHeight: sourceWorkspace.stacked ? sourceWorkspace.stackedFormHeight : -1
                contentSpacing: 0

                Flickable {
                    id: sourceFormScroll
                    objectName: "sourceFormScroll"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    contentWidth: width
                    contentHeight: sourceFormLoader.height + 12
                    onWidthChanged: pagePane.scheduleFocusedSourceFieldReveal()
                    onHeightChanged: pagePane.scheduleFocusedSourceFieldReveal()
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar { palette: window.palette }

                    Loader {
                        id: sourceFormLoader
                        objectName: "sourceFormLoader"
                        width: parent.width - 12
                        height: item ? item.implicitHeight : 40
                        active: window.visible
                        asynchronous: true
                        onHeightChanged: pagePane.scheduleFocusedSourceFieldReveal()
                        sourceComponent: Component {
                            Column {
                                id: form
                                width: sourceFormLoader.width
                                spacing: 10
                        ThemeControls.ThemeLabel { text: "源类型/发射模型"; font.bold: true }
                        ThemeControls.ThemeComboBox {
                            objectName: "emissionKind"
                            width: parent.width
                            model: [{text: "简单快速模型", value: "uniform"}, {text: "电阻蒸发 / 舟", value: "resistance_boat"}, {text: "电子束有效发射 / 扫描", value: "electron_beam"}]
                            textRole: "text"; valueRole: "value"
                            currentIndex: ["uniform", "resistance_boat", "electron_beam"].indexOf(bridge.emissionFields.kind)
                            onActivated: bridge.setEmissionField("kind", currentValue)
                        }
                        ThemeControls.ThemeLabel { width: parent.width; wrapMode: Text.Wrap; visible: bridge.emissionFields.kind !== "uniform" && bridge.hasMaterial; text: "切换到电阻舟或电子束模型会载入材料的对应默认参数。几何尺寸仍按实际设备编辑。"; opacity: 0.75 }
                        ThemeControls.ThemeLabel {
                            text: "发射源参数"
                            heading: true
                        }
                        ThemeControls.ThemeLabel {
                            width: parent.width
                            wrapMode: Text.WordWrap
                            text: "源文件保存几何、材料快照和发射模型。安装坐标在机器中设置，运行速率在工艺步骤中设置。"
                        }
                        Column {
                            objectName: "sourceMaterialSnapshotSection"
                            width: parent.width
                            spacing: 6
                            visible: bridge.emissionFields.kind !== "uniform"
                        ThemeControls.ThemeLabel { visible: bridge.emissionFields.kind !== "uniform"; text: "材料快照"; font.bold: true }
                            RowLayout { objectName: "sourceMaterialPickerRow"; visible: bridge.emissionFields.kind !== "uniform"
                            width: parent.width
                            ThemeControls.ThemeMaterialPicker {
                                objectName: "sourceFilmMaterialPicker"
                                Layout.fillWidth: true
                                Layout.minimumWidth: 0
                                Layout.preferredWidth: 230
                                model: bridge.materialLibrary.filmEntries
                                textRole: "name"; valueRole: "key"
                                currentIndex: -1
                                displayText: bridge.materialLibrary.loading ? "正在加载材料…" : bridge.materialName
                                enabled: !bridge.materialLibrary.loading
                                accessibleLabel: "蒸发源材料"
                                placeholderText: "输入材料名称搜索"
                                onActivated: function(index) {
                                    bridge.materialLibrary.selectSourceMaterial(valueAt(index))
                                }
                            }
            ThemeControls.ThemeButton { id: sourceMaterialReferenceButton; objectName: "sourceMaterialReferenceButton"; text: "参考"; enabled: bridge.hasMaterial && !bridge.materialReferenceLoading; onClicked: window.openMaterialReferenceDialog() }
                        }
                        ThemeControls.ThemeLabel { width: parent.width; visible: bridge.emissionFields.kind !== "uniform" && bridge.materialReferenceLoading; text: "正在准备参考详情…"; opacity: 0.75 }
                        ThemeControls.ThemeLabel { width: parent.width; wrapMode: Text.Wrap; visible: bridge.emissionFields.kind !== "uniform" && bridge.hasMaterial; text: "已捕获独立副本；库中更改不会自动同步。重新选用才应用最新版本。"; opacity: 0.75 }
                            Flow { objectName: "sourceMaterialActionsRow"; visible: bridge.emissionFields.kind !== "uniform"; width: parent.width; spacing: pagePane.spaceSm; height: childrenRect.height
                            ThemeControls.ThemeButton { text: "选择 / 编辑材料"; onClicked: { bridge.materialLibrary.prepareForSource(); window.openMaterialLibrary() } }
                            ThemeControls.ThemeButton { text: "移除快照"; enabled: bridge.hasMaterial; onClicked: bridge.clearMaterial() }
                        }
                        ThemeControls.ThemeButton { text: "编辑源内材料快照"; visible: bridge.emissionFields.kind !== "uniform" && bridge.hasMaterial; onClicked: { window.openMaterialLibrary(); bridge.editMaterialSnapshot() } }
                        }
                        ThemeControls.ThemeLabel { text: "\u51e0\u4f55\u5f62\u72b6" }
                        RowLayout {
                            width: parent.width
                            spacing: 6
                            ThemeControls.ThemeLabel { text: "\u6e90\u7c7b\u578b"; Layout.minimumWidth: Math.max(70, sourceKindLabelMetrics.width) }
                            ThemeControls.ThemeComboBox {
                                id: shape
                                objectName: "sourceGeometryShape"
                                Layout.fillWidth: true
                                model: [
                                    { text: "\u70b9\u6e90", value: "point" },
                                    { text: "\u7ebf\u6e90", value: "line" },
                                    { text: "\u5706\u76d8\u6e90", value: "disk" },
                                    { text: "\u73af\u5f62\u6e90", value: "annulus" },
                                    { text: "\u77e9\u5f62\u6e90", value: "rectangle" }
                                ]
                                textRole: "text"
                                valueRole: "value"
                                currentIndex: ["point", "line", "disk", "annulus", "rectangle"].indexOf(String(bridge.fields.kind))
                                onActivated: bridge.setField("kind", currentValue)
                            }
                        }
                        GridLayout {
                            objectName: "sourceGeometryFields"
                            width: parent.width
                            columns: width >= 2 * pagePane.minimumSourceFieldColumnWidth + columnSpacing ? 2 : 1
                            columnSpacing: 8
                            rowSpacing: 4
                            LocalComponents.SourceValueField {
                                objectName: "sourceExponentValueField"
                                Layout.fillWidth: true
                                fieldName: "source_exponent"
                                compact: true
                                compactFieldWidth: 84
                                label: "cos\u03b8 \u6307\u6570 n"
                                value: String(bridge.fields.source_exponent)
                                hint: "n=0 \u4e3a\u5404\u5411\u540c\u6027\uff1bn=1 \u4e3a cos\u03b8\u3002\u53ea\u6539\u53d8\u89d2\u5206\u5e03\uff0c\u4e0d\u6539\u51e0\u4f55\u8fb9\u754c\u3002"
                                onEdited: value => bridge.setField("source_exponent", value)
                            }
                            LocalComponents.SourceValueField {
                                objectName: "sourceLengthValueField"
                                Layout.fillWidth: true
                                fieldName: "length_mm"
                                compact: true
                                compactFieldWidth: 84
                                visible: shape.currentValue === "line" || shape.currentValue === "rectangle"
                                label: shape.currentValue === "rectangle" ? "\u77e9\u5f62\u957f / mm" : "\u7ebf\u957f / mm"
                                value: String(bridge.fields.length_mm)
                                onEdited: value => bridge.setField("length_mm", value)
                            }
                            LocalComponents.SourceValueField {
                                objectName: "sourceWidthValueField"
                                Layout.fillWidth: true
                                fieldName: "width_mm"
                                compact: true
                                compactFieldWidth: 84
                                visible: shape.currentValue === "rectangle"
                                label: "\u77e9\u5f62\u5bbd / mm"
                                value: String(bridge.fields.width_mm)
                                onEdited: value => bridge.setField("width_mm", value)
                            }
                            LocalComponents.SourceValueField {
                                objectName: "sourceAngleValueField"
                                Layout.fillWidth: true
                                fieldName: "angle_deg"
                                compact: true
                                compactFieldWidth: 84
                                visible: shape.currentValue === "line" || shape.currentValue === "rectangle"
                                label: shape.currentValue === "rectangle" ? "\u77e9\u5f62\u65b9\u5411 / \u00b0" : "\u7ebf\u65b9\u5411 / \u00b0"
                                value: String(bridge.fields.angle_deg)
                                onEdited: value => bridge.setField("angle_deg", value)
                            }
                            LocalComponents.SourceValueField {
                                objectName: "sourceRadiusValueField"
                                Layout.fillWidth: true
                                fieldName: "radius_mm"
                                compact: true
                                compactFieldWidth: 84
                                visible: shape.currentValue === "disk"
                                label: "\u534a\u5f84 / mm"
                                value: String(bridge.fields.radius_mm)
                                onEdited: value => bridge.setField("radius_mm", value)
                            }
                            LocalComponents.SourceValueField {
                                objectName: "sourceInnerRadiusValueField"
                                Layout.fillWidth: true
                                fieldName: "inner_radius_mm"
                                compact: true
                                compactFieldWidth: 84
                                visible: shape.currentValue === "annulus"
                                label: "\u5185\u534a\u5f84 / mm"
                                value: String(bridge.fields.inner_radius_mm)
                                onEdited: value => bridge.setField("inner_radius_mm", value)
                            }
                            LocalComponents.SourceValueField {
                                objectName: "sourceOuterRadiusValueField"
                                Layout.fillWidth: true
                                fieldName: "outer_radius_mm"
                                compact: true
                                compactFieldWidth: 84
                                visible: shape.currentValue === "annulus"
                                label: "\u5916\u534a\u5f84 / mm"
                                value: String(bridge.fields.outer_radius_mm)
                                onEdited: value => bridge.setField("outer_radius_mm", value)
                            }
                        }
                                LocalComponents.SourceEmissionForm {
                                    objectName: "sourceEmissionForm"
                                    width: parent.width
                                    editor: bridge
                                }
                            }
                        }
                    }
                    ThemeControls.ThemeLabel {
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.top: parent.top
                        height: 40
                        visible: sourceFormLoader.status !== Loader.Ready
                        text: "正在准备源参数…"
                        opacity: 0.75
                    }
                }
            }

            Rectangle {
                id: previewPanel
                objectName: "previewPanel"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumWidth: sourceWorkspace.stacked ? 0 : sourceWorkspace.previewMinimumWidth
                Layout.preferredWidth: sourceWorkspace.stacked ? sourceWorkspace.width : Math.max(sourceWorkspace.previewMinimumWidth, sourceWorkspace.width - sourceFormSurface.Layout.preferredWidth - sourceWorkspace.columnSpacing)
                Layout.minimumHeight: sourceWorkspace.stacked ? sourceWorkspace.minimumPreviewHeight : 0
                Layout.preferredHeight: sourceWorkspace.stacked ? sourceWorkspace.stackedPreviewHeight : -1
                radius: 8
                color: window.themeColors.colorSurface
                border.color: window.themeColors.colorBorder

                ThemeControls.ThemeLabel {
                    id: previewTitle
                    objectName: "previewTitle"
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 18
                    wrapMode: Text.WordWrap
                    text: "从 +Z 向下俯视 · 局部 XY"
                    heading: true
                }
                Flow {
                    id: previewControls
                    objectName: "previewControls"
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: previewTitle.bottom
                    anchors.margins: 18
                    spacing: pagePane.spaceSm
                    height: childrenRect.height
                    ThemeControls.ThemeLabel {
                        width: Math.min(previewControls.width, previewRateLabelMetrics.width + 4)
                        wrapMode: Text.WordWrap
                        text: "预览速率 / Å/s"
                    }
                    ThemeControls.ThemeField {
                        objectName: "previewRateField"
                        width: Math.min(previewControls.width,
                                        Math.max(88, Math.min(220, previewRateMetrics.width + 32)))
                        numericDisplayValue: bridge.previewRate
                        onTextEdited: bridge.setPreviewRate(text)
                    }
                }
                Canvas {
                    id: preview
                    objectName: "preview"
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: previewControls.bottom
                    anchors.bottom: previewNote.top
                    anchors.margins: 18
                    onWidthChanged: requestPaint()
                    onHeightChanged: requestPaint()
                    onPaint: {
                        const c = getContext("2d")
                        c.clearRect(0, 0, width, height)
                        const f = bridge.fields
                        const cx = width / 2, cy = height / 2
                        const kind = f.kind
                        const darkCanvas = window.themePaletteMode === "dark"
                        const gridColor = window.themeColors ? window.themeColors.colorBorder : (darkCanvas ? "#506475" : "#d4dde6")
                        const axisColor = window.themeColors ? window.themeColors.colorBorderStrong : (darkCanvas ? "#8395a1" : "#8a9ba5")
                        const secondaryLabelColor = window.themeColors ? window.themeColors.colorTextMuted : (darkCanvas ? "#c0ccd4" : "#506475")
                        const reach = kind === "line" ? Math.abs(Number(f.length_mm)) / 2
                                    : kind === "disk" ? Math.abs(Number(f.radius_mm))
                                    : kind === "annulus" ? Math.abs(Number(f.outer_radius_mm))
                                    : kind === "rectangle" ? Math.hypot(Number(f.length_mm), Number(f.width_mm)) / 2 : 0
                        const scale = 0.38 * Math.min(width, height) / Math.max(20, reach * 1.35)
                        c.lineWidth = 1
                        c.strokeStyle = gridColor
                        c.globalAlpha = 0.55
                        c.beginPath()
                        for (let i = 1; i < 6; ++i) {
                            if (i === 3) continue
                            const gx = 16 + (width - 32) * i / 6
                            const gy = 16 + (height - 32) * i / 6
                            c.moveTo(gx, 16); c.lineTo(gx, height - 16)
                            c.moveTo(16, gy); c.lineTo(width - 16, gy)
                        }
                        c.stroke()
                        c.globalAlpha = 1
                        c.strokeStyle = axisColor
                        c.beginPath(); c.moveTo(16, cy); c.lineTo(width - 16, cy)
                        c.moveTo(cx, 16); c.lineTo(cx, height - 16); c.stroke()
                        c.fillStyle = secondaryLabelColor
                        c.font = window.themeColors ? window.themeColors.fontSizeBody + "pt sans-serif" : "15px sans-serif"
                        c.fillText("+X", width - 42, cy - 8)
                        c.fillText("+Y", cx + 8, 25)
                        c.strokeStyle = darkCanvas ? "#69baff" : "#2a70ca"
                        c.fillStyle = darkCanvas ? "#365775" : "#cfe4ff"
                        c.lineWidth = 3
                        if (kind === "line" && Number.isFinite(reach)) {
                            const angle = Number(f.angle_deg) * Math.PI / 180
                            const dx = Math.cos(angle) * reach * scale
                            const dy = Math.sin(angle) * reach * scale
                            c.lineWidth = 6
                            c.beginPath(); c.moveTo(cx - dx, cy + dy)
                            c.lineTo(cx + dx, cy - dy); c.stroke()
                        } else if (kind === "rectangle" && Number.isFinite(reach)) {
                            const length = Number(f.length_mm) * scale
                            const sourceWidth = Number(f.width_mm) * scale
                            const angle = Number(f.angle_deg) * Math.PI / 180
                            if (Number.isFinite(length) && Number.isFinite(sourceWidth) && Number.isFinite(angle)) {
                                c.save()
                                c.translate(cx, cy)
                                c.rotate(-angle)
                                c.beginPath()
                                c.rect(-length / 2, -sourceWidth / 2, length, sourceWidth)
                                c.fill(); c.stroke()
                                c.restore()
                            }
                        } else if ((kind === "disk" || kind === "annulus") && Number.isFinite(reach)) {
                            c.beginPath(); c.arc(cx, cy, reach * scale, 0, Math.PI * 2)
                            c.fill(); c.stroke()
                            if (kind === "annulus") {
                                const inner = Math.max(0, Number(f.inner_radius_mm) * scale)
                                 c.fillStyle = window.themeColors.colorSurface
                                c.beginPath(); c.arc(cx, cy, inner, 0, Math.PI * 2)
                                c.fill(); c.stroke()
                            }
                        } else if (kind === "point") {
                            c.fillStyle = darkCanvas ? "#69baff" : "#2a70ca"
                            c.beginPath(); c.arc(cx, cy, 6, 0, Math.PI * 2); c.fill()
                        }
                        c.fillStyle = darkCanvas ? "#92d7ff" : "#1d4ed8"
                        const samplePoints = bridge.samplePoints
                        for (const point of samplePoints) {
                            c.beginPath()
                            c.arc(cx + point.x * scale, cy - point.y * scale,
                                  Math.max(1.2, Math.min(8, Math.sqrt(point.weight * samplePoints.length) * 3)), 0, Math.PI * 2)
                            c.fill()
                        }
                        const e = bridge.emissionFields
                        if (e.kind === "resistance_boat" && e.boat_mode === "covered") {
                            c.save()
                            c.translate(cx, cy)
                            c.rotate(-Number(f.angle_deg || 0) * Math.PI / 180)
                            c.strokeStyle = darkCanvas ? "#eacb71" : "#96721a"
                            c.lineWidth = 2
                            for (const hole of bridge.apertures) {
                                const values = [hole.offset_x_mm, hole.offset_y_mm, hole.angle_deg, hole.length_mm, hole.width_mm, hole.radius_mm].map(Number)
                                if (!values.every(Number.isFinite)) continue
                                c.save()
                                c.translate(Number(hole.offset_x_mm) * scale, -Number(hole.offset_y_mm) * scale)
                                c.rotate(-Number(hole.angle_deg || 0) * Math.PI / 180)
                                c.beginPath()
                                if (hole.kind === "disk") c.arc(0, 0, Math.max(0, Number(hole.radius_mm) * scale), 0, Math.PI * 2)
                                else c.rect(-Number(hole.length_mm) * scale / 2, -Number(hole.width_mm) * scale / 2, Number(hole.length_mm) * scale, Number(hole.width_mm) * scale)
                                c.stroke()
                                c.restore()
                            }
                            c.restore()
                        }
                        c.strokeStyle = darkCanvas ? "#ff9979" : "#ba4a31"; c.lineWidth = 2
                        c.beginPath(); c.moveTo(cx - 7, cy); c.lineTo(cx + 7, cy)
                        c.moveTo(cx, cy - 7); c.lineTo(cx, cy + 7); c.stroke()
                    }
                }
                ThemeControls.ThemeLabel {
                    id: previewNote
                    objectName: "previewNote"
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    anchors.margins: 18
                    wrapMode: Text.WordWrap
                    text: bridge.previewSummary + "\n红十字为局部原点；预览速率不修改工艺。盖板开口仅表示俯视轮廓，射线遮挡在仿真中计算。"
                }
            }
            }
        }

    }

}
