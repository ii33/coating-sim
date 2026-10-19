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
    // QQmlContext.setContextProperty("launcher", ...) supplies the shared window host.
    // qmllint disable unqualified
    readonly property var moduleLauncher: typeof launcher !== "undefined" ? launcher : null
    // qmllint enable unqualified
    property var projectNavigation: null
    property var projectResources: null
    property bool projectPendingTextEdited: false
    property bool projectCloseAuthorized: false
    LocalComponents.ProjectPendingEdits { editorWindow: window }
    titleBase: "工艺 · " + (window.editorBridge.documentMetadata.name || "未命名")
           + (window.editorBridge.dirty ? " *" : "")
    projectNameForTitle: projectNavigation ? projectNavigation.projectName : ""
    statusText: bridge.errors.length ? bridge.errors[0] : bridge.status
    statusDetail: bridge.errors.length
                  ? bridge.errors.join("\n") + (bridge.status.length ? "\n" + bridge.status : "")
                  : ""
    statusSeverity: bridge.errors.length ? "error"
                    : bridge.optimization.busy ? "busy" : "info"
    minimumWidth: 720
    minimumHeight: 560
    readonly property bool previewEmpty: Boolean((bridge.sourcePreview.message || "").length)
    property bool sourcePreviewRequested: false
    property bool sourcePreviewScheduled: false
    onClosing: function(close) {
        if (window.projectCloseAuthorized) {
            window.projectCloseAuthorized = false
            close.accepted = true
            return
        }
        if (window.projectResources !== null) {
            close.accepted = window.projectResources.requestClose()
            return
        }
    }
    function commitPendingEdits() {
        if (contentItem) contentItem.forceActiveFocus()
    }

    function refreshMetadata() {
        const item = bridge.documentMetadata
        if (!nameField.activeFocus)
            nameField.text = item.name || ""
        if (!machineField.activeFocus)
            machineField.text = item.machine_file || ""
        if (!substrateField.activeFocus)
            substrateField.text = item.substrate_file || ""
    }
    function refreshEnvironmentSummary() {
        if (!environmentStep || !stepEnvironmentSummary) return
        const state = bridge.environmentEditor(environmentStep.value - 1)
        stepEnvironmentSummary.text = state.summary +
            (Object.keys(state.raw || {}).length ? " · 本层有独立覆盖" : " · 本层全部继承")
    }
    function sourceKind(kind) {
        return ({point: "点源", line: "线源", disk: "圆盘源", annulus: "环形源", rectangle: "矩形源"})[kind] || ""
    }
    function sourceDevice(kind) {
        return ({custom: "自定义", resistance_boat: "电阻舟", crucible: "坩埚", electron_beam: "电子束"})[kind] || ""
    }
    function focusStepFieldAt(targetStepIndex, columnName, delta) {
        if (targetStepIndex < 0 || targetStepIndex >= bridge.stepCount)
            return false
        const targetPage = Math.floor(targetStepIndex / 40)
        if (targetPage !== bridge.stepPage) {
            bridge.setStepPage(targetPage)
            Qt.callLater(function() {
                window.focusStepFieldAt(targetStepIndex, columnName, delta)
            })
            return true
        }
        const targetRow = stepRows.itemAt(targetStepIndex - bridge.stepPage * 40)
        if (!targetRow) {
            Qt.callLater(function() {
                window.focusStepFieldAt(targetStepIndex, columnName, delta)
            })
            return true
        }
        if (targetRow.focusColumnField(columnName))
            return true
        return window.focusStepFieldAt(targetStepIndex + delta, columnName, delta)
    }
    function moveStepColumnFocus(fromStepIndex, columnName, delta) {
        focusStepFieldAt(fromStepIndex + delta, columnName, delta)
    }
    function xyz(item) {
        return "X " + item.x_mm + "  ·  Y " + item.y_mm + "  ·  Z " + (item.z_mm === undefined ? 0 : item.z_mm) + " mm"
    }
    function sendToSimulation() {
        if (!bridge.valid || bridge.optimization.busy)
            return
        if (!bridge.currentPath ||
                (bridge.dirty && bridge.currentPath.toLowerCase().endsWith(".coatasset"))) {
            saveBeforeSendAsDialog.open()
            return
        }
        if (bridge.dirty) {
            saveBeforeSendDialog.open()
            return
        }
        if (window.projectNavigation)
            window.projectNavigation.openProjectModule("main")
        else
            window.moduleLauncher.openWindow("main", bridge.currentPath)
    }
    function saveAndSendToSimulation() {
        if (!bridge.currentPath || bridge.currentPath.toLowerCase().endsWith(".coatasset"))
            return
        bridge.save()
        if (!bridge.dirty && bridge.currentPath)
            if (window.projectNavigation)
                window.projectNavigation.openProjectModule("main")
            else
                window.moduleLauncher.openWindow("main", bridge.currentPath)
    }
    Component.onCompleted: { refreshMetadata(); refreshEnvironmentSummary() }
    Connections {
        target: typeof Theme !== "undefined" ? Theme : null
        function onSnapshotChanged() {
            if (window.sourcePreviewRequested) sourceCanvas.requestPaint()
        }
    }
    Connections {
        target: window
        function onFrameSwapped() {
            if (!window.visible || window.sourcePreviewScheduled) return
            window.sourcePreviewScheduled = true
            Qt.callLater(function() {
                if (!window.visible) {
                    window.sourcePreviewScheduled = false
                    return
                }
                window.sourcePreviewRequested = true
                bridge.prepareSourcePreview()
            })
        }
    }
    Connections {
        target: bridge
        function onOverwriteRequested(url) { overwriteDialog.targetPath = url.toString(); overwriteDialog.open() }
        function onReloadRequested() { reloadDialog.open() }
        function onDocumentReplaceRequested() { documentReplaceDialog.open() }
        function onFilmDesignImportReady() { filmDesignImportDialog.open() }
        function onChanged() { window.refreshMetadata(); window.refreshEnvironmentSummary() }
    }


    ThemeControls.ThemeDialogBase {
        palette: window.palette
        id: reloadDialog
        objectName: "reloadDialog"
        title: "放弃修改并刷新？"
        modal: true
        anchors.centerIn: parent
        width: Math.min(480, window.width - 40)
        standardButtons: Dialog.Yes | Dialog.No
        onAccepted: bridge.confirmReload(true)
        onRejected: bridge.confirmReload(false)
        contentItem: Label { text: "刷新会重新读取工艺、机器、基片及其依赖，并放弃当前未保存修改。不会写入文件。"; wrapMode: Text.Wrap }
    }

    FileDialog {
        id: openDialog
        currentFolder: bridge.defaultDirectory
        title: "打开工艺"
        fileMode: FileDialog.OpenFile
        nameFilters: ["工艺设计 (*.cproc)"]
        onAccepted: bridge.openPath(selectedFile)
    }
    FileDialog {
        id: saveDialog
        currentFolder: bridge.defaultDirectory
        title: "工艺另存为"
        fileMode: FileDialog.SaveFile
        options: FileDialog.DontConfirmOverwrite
        nameFilters: ["工艺设计 (*.cproc)"]
        defaultSuffix: "cproc"
        onAccepted: bridge.saveAsPath(selectedFile)
    }
    ThemeControls.ThemeDialogBase {
        palette: window.palette
        id: overwriteDialog
        objectName: "overwriteDialog"
        property string targetPath: ""
        title: "覆盖已有工艺？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.No
        onAccepted: bridge.confirmSaveAs(true)
        onRejected: bridge.confirmSaveAs(false)
        ThemeControls.ThemeLabel { text: "文件已存在，确认覆盖？\n" + overwriteDialog.targetPath; wrapMode: Text.WrapAnywhere; width: 480 }
    }
    ThemeControls.ThemeDialogBase {
        palette: window.palette
        id: documentReplaceDialog
        title: "放弃未保存的工艺修改？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.No
        onAccepted: bridge.confirmDocumentReplace(true)
        onRejected: bridge.confirmDocumentReplace(false)
        contentItem: Label {
            width: 440
            text: "继续新建或打开其他工艺将丢弃当前未保存修改。选择“是”继续；选择“否”保留当前工艺。"
            wrapMode: Text.WordWrap
        }
    }
    ThemeControls.ThemeDialogBase {
        palette: window.palette
        id: saveBeforeSendDialog
        objectName: "saveBeforeSendDialog"
        title: "保存并发送当前工艺？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.NoButton
        contentItem: ColumnLayout {
            spacing: 12
            ThemeControls.ThemeLabel {
                Layout.preferredWidth: 460
                text: "当前工艺有未保存修改。继续将覆盖以下文件，仿真随后会从刚保存的版本加载：\n" + bridge.currentPath
                wrapMode: Text.WordWrap
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                ThemeControls.ThemeButton {
                    text: "保存并发送"
                    primary: true
                    objectName: "saveAndSendToSimulationButton"
                    onClicked: { saveBeforeSendDialog.close(); window.saveAndSendToSimulation() }
                }
                ThemeControls.ThemeButton {
                    text: "取消"
                    objectName: "cancelSendToSimulationButton"
                    onClicked: saveBeforeSendDialog.close()
                }
            }
        }
    }
    ThemeControls.ThemeDialogBase {
        palette: window.palette
        id: saveBeforeSendAsDialog
        title: "先保存工艺"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.NoButton
        contentItem: ColumnLayout {
            spacing: 12
            ThemeControls.ThemeLabel {
                Layout.preferredWidth: 460
                text: "当前工艺还没有可用于仿真的已保存文件。请先使用“另存为”选择路径；保存完成后再点“发送至仿真”。已有目标文件会继续通过覆盖确认。"
                wrapMode: Text.WordWrap
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                ThemeControls.ThemeButton {
                    text: "另存为"
                    primary: true
                    onClicked: { saveBeforeSendAsDialog.close(); saveDialog.open() }
                }
                ThemeControls.ThemeButton {
                    text: "取消"
                    onClicked: saveBeforeSendAsDialog.close()
                }
            }
        }
    }
    FileDialog {
        id: machineDialog
        currentFolder: bridge.dependencyDirectory("machine")
        title: "选择机器文件"
        fileMode: FileDialog.OpenFile
        nameFilters: ["机器设计 (*.cmac)"]
        onAccepted: bridge.chooseDependency("machine_file", selectedFile)
    }
    FileDialog {
        id: substrateDialog
        currentFolder: bridge.dependencyDirectory("substrate")
        title: "选择基片文件"
        fileMode: FileDialog.OpenFile
        nameFilters: ["基片设计 (*.csub)"]
        onAccepted: bridge.chooseDependency("substrate_file", selectedFile)
    }

    LocalComponents.FilmStackDialog { id: filmStackDialog; parent: Overlay.overlay; editor: window.editorBridge }
    LocalComponents.FilmDesignImportDialog { id: filmDesignImportDialog; parent: Overlay.overlay; editor: window.editorBridge; palette: window.palette }
    LocalComponents.ProcessEnvironmentDialog { id: environmentDialog; parent: Overlay.overlay; editor: window.editorBridge }

    Pane {
        anchors.fill: parent
        padding: 0
        background: null
        palette: window.palette
        font.family: window.themeColors.fontFamily
        font.pointSize: window.themeColors.fontSizeBody
        font.weight: window.themeColors.fontWeightNormal

        LocalComponents.ResponsiveOperationBar {
            id: actions
            objectName: "processActions"
            Accessible.name: "操作栏"
            x: 18
            y: window.contentTopMargin
            width: parent.width - 36
            height: implicitHeight
            spacing: 8
            LocalComponents.ProjectModuleSelector {
                navigation: window.projectNavigation
                currentModuleKey: "process"
            }
            LocalComponents.ProjectResourceControls {
                projectResources: window.projectResources
                informationProvider: window.projectResources !== null ? window.projectResources : window.editorBridge.informationController
            }
            ThemeControls.ThemeButton { text: "新建"; visible: window.projectResources === null; onClicked: window.editorBridge.newDocument() }
            ThemeControls.ThemeButton { text: "打开"; visible: window.projectResources === null; onClicked: openDialog.open() }
            ThemeControls.ThemeButton {
                objectName: "reloadButton"
                Accessible.name: "刷新并重新载入文件"
                text: "刷新"
                enabled: bridge.canReload
                onClicked: bridge.reloadCurrent()
                ToolTip.visible: hovered
                ToolTip.text: "重新读取当前工艺及全部依赖"
            }
            ThemeControls.ThemeButton {
                text: "保存"
                visible: window.projectResources === null
                onClicked: bridge.currentPath.length ? bridge.save() : saveDialog.open()
            }
            ThemeControls.ThemeButton { text: "另存为"; visible: window.projectResources === null; onClicked: saveDialog.open() }
            ThemeControls.ThemeButton {
                objectName: "sendToSimulationButton"
                text: "发送至仿真"
                primary: true
                enabled: bridge.valid
                onClicked: window.sendToSimulation()
            }
            // Keep the batch controller and dialog available to existing callers.
            ThemeControls.ThemeButton { objectName: "batchFilmStackButton"; text: "批量膜层"; visible: false; enabled: window.editorBridge.sourcePreview.sources.length > 0 && substrateField.text.length > 0; onClicked: filmStackDialog.open() }
            ThemeControls.ThemeButton {
                objectName: "filmDesignImportButton"
                text: "从膜系导入"
                onClicked: bridge.requestFilmDesignImport()
                ToolTip.visible: hovered
                ToolTip.text: "从活动膜系窗口读取当前内存设计；需明确选择一面和每种材料的蒸发源位"
            }
            ThemeControls.ThemeLabel {
                objectName: "processResourceDisplayName"
                property bool operationBarFlexible: true
                visible: window.projectResources === null
                text: (window.editorBridge.dirty ? "● " : "") + (window.editorBridge.documentMetadata.name || "未命名工艺")
                textFormat: Text.PlainText
                width: Math.max(150, Math.min(300, actions.width * 0.24))
                height: 38
                verticalAlignment: Text.AlignVCenter
                leftPadding: 10
                elide: Text.ElideMiddle
                HoverHandler { id: processNameHover }
                ToolTip.visible: processNameHover.hovered && window.editorBridge.currentPath.length > 0
                ToolTip.text: window.editorBridge.currentPath
            }
        }

        Item {
            id: workspace
            objectName: "processWorkspace"
            x: 18
            y: actions.y + actions.height + 8
            width: parent.width - 36
            height: Math.max(0, parent.height - y - 18)
        }

        Flickable {
            id: scroll
            objectName: "processEditorScroll"
            x: workspace.x
            y: workspace.y
            width: Math.max(0, workspace.width
                            )
            height: workspace.height
            clip: true
            contentWidth: width
            contentHeight: body.implicitHeight + 16
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { palette: window.palette }

            ColumnLayout {
                id: body
                width: scroll.width - 14
                spacing: 8

                ThemeControls.ThemeSection {
                    objectName: "processPlanSection"
                    title: "方案文件"
                    accessibleLabel: "方案文件，展开或折叠"
                    expanded: true
                    Layout.fillWidth: true
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 5
                        RowLayout {
                            objectName: "processPreviewContentRow"
                            visible: false
                            Layout.fillWidth: true
                            spacing: 6
                            ThemeControls.ThemeLabel { objectName: "processNameLabel"; text: "工艺名称"; Layout.preferredWidth: 82; Layout.maximumWidth: 82 }
                            ThemeControls.ThemeField {
                                id: nameField
                                objectName: "processNameField"
                                Layout.fillWidth: true
                                Layout.minimumWidth: 120
                                onEditingFinished: bridge.setField("name", text)
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 6
                            ThemeControls.ThemeLabel { objectName: "processMachineLabel"; text: "机器文件"; Layout.preferredWidth: 82; Layout.maximumWidth: 82 }
                            ThemeControls.ThemeField {
                                id: machineField
                                objectName: "processMachineField"
                                Layout.fillWidth: true
                                Layout.minimumWidth: 120
                                onEditingFinished: bridge.setField("machine_file", text)
                            }
                            ThemeControls.ThemeButton { objectName: "selectMachineFileButton"; text: "浏览"; Accessible.name: "浏览机器文件"; onClicked: machineDialog.open() }
                            LocalComponents.ProjectResourceDependencyPicker {
                                objectName: "processMachineDependencyPicker"
                                projectResources: window.projectResources
                                kind: "machine"
                                pointer: "machine_file"
                                currentReference: window.editorBridge.document.machine_file || ""
                                visible: window.projectResources !== null
                            }
                            ThemeControls.ThemeButton {
                                objectName: "machineDesignActionButton"
                                text: (window.editorBridge.document.machine_file || "").length > 0 ? "编辑" : "新建"
                                Accessible.name: (window.editorBridge.document.machine_file || "").length > 0 ? "编辑机器" : "新建机器"
                                onClicked: {
                                    if (window.projectResources !== null)
                                        window.projectResources.openDependencyEditor("machine_file")
                                    else
                                        window.moduleLauncher.openWindow(
                                            "machine", window.editorBridge.dependencyPath("machine_file"))
                                }
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 6
                            ThemeControls.ThemeLabel { objectName: "processSubstrateLabel"; text: "基片文件"; Layout.preferredWidth: 82; Layout.maximumWidth: 82 }
                            ThemeControls.ThemeField {
                                id: substrateField
                                objectName: "processSubstrateField"
                                Layout.fillWidth: true
                                Layout.minimumWidth: 120
                                onEditingFinished: bridge.setField("substrate_file", text)
                            }
                            ThemeControls.ThemeButton { objectName: "selectSubstrateFileButton"; text: "浏览"; Accessible.name: "浏览基片文件"; onClicked: substrateDialog.open() }
                            LocalComponents.ProjectResourceDependencyPicker {
                                objectName: "processSubstrateDependencyPicker"
                                projectResources: window.projectResources
                                kind: "substrate"
                                pointer: "substrate_file"
                                currentReference: window.editorBridge.document.substrate_file || ""
                                visible: window.projectResources !== null
                            }
                            ThemeControls.ThemeButton {
                                objectName: "substrateDesignActionButton"
                                text: (window.editorBridge.document.substrate_file || "").length > 0 ? "编辑" : "新建"
                                Accessible.name: (window.editorBridge.document.substrate_file || "").length > 0 ? "编辑基片" : "新建基片"
                                onClicked: {
                                    if (window.projectResources !== null)
                                        window.projectResources.openDependencyEditor("substrate_file")
                                    else
                                        window.moduleLauncher.openWindow(
                                            "substrate", window.editorBridge.dependencyPath("substrate_file"))
                                }
                            }
                        }
                    }
                }

                ThemeControls.ThemeSection {
                    objectName: "processPreviewSection"
                    title: "机器结构预览 · 俯视"
                    accessibleLabel: "机器结构预览·俯视，展开或折叠"
                    expanded: true
                    Layout.fillWidth: true
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 5
                        RowLayout {
                            Layout.fillWidth: true
                            ThemeControls.ThemeLabel {
                                text: "固定机架 XYZ / mm；圆环表示行星盘中心轨道。实线为板外轮廓，虚线为孔洞。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            ThemeControls.ThemeButton { text: "刷新预览"; onClicked: { window.sourcePreviewRequested = true; bridge.refreshSourcePreview() } }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            Layout.preferredHeight: window.previewEmpty ? 112 : 220
                            Layout.minimumHeight: window.previewEmpty ? 96 : 190
                            Layout.maximumHeight: 250
                            spacing: 8
                            Rectangle {
                                objectName: "processPlanFrame"
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                Layout.minimumWidth: 250
                                color: window.themeColors.colorSurface
                                border.color: window.themeColors.colorBorder
                                radius: 4
                                LocalComponents.MachinePlanView {
                                    id: sourceCanvas
                                    objectName: "processPlanPreview"
                                    anchors.fill: parent
                                    anchors.margins: 5
                                    plan: bridge.sourcePreview
                                    ink: window.themeColors.colorText
                                    axisColor: window.themeColors.colorBorderStrong
                                    orbitColor: window.themeColors.colorAccent
                                    sourceColors: [window.themeColors.colorAccent,
                                                   window.themeColors.colorInfo,
                                                   window.themeColors.colorSuccess,
                                                   window.themeColors.colorWarning]
                                    maskColors: [window.themeColors.colorInfo,
                                                 window.themeColors.colorAccent]
                                }
                                ThemeControls.ThemeLabel {
                                    anchors.centerIn: parent
                                    width: parent.width - 30
                                    horizontalAlignment: Text.AlignHCenter
                                    wrapMode: Text.WordWrap
                                    text: bridge.sourcePreview.message || ""
                                    visible: text.length > 0
                                }
                                ThemeControls.ThemeLabel {
                                    anchors.centerIn: parent
                                    width: parent.width - 30
                                    horizontalAlignment: Text.AlignHCenter
                                    wrapMode: Text.WordWrap
                                    text: "机架轮廓在首帧后载入；来源和工艺控件已就绪。"
                                    visible: !window.sourcePreviewRequested && !window.previewEmpty
                                    color: window.themeColors.colorTextMuted
                                }
                            }
                            ScrollView {
                                id: previewDetails
                                objectName: "processPreviewDetails"
                                Layout.preferredWidth: 286
                                Layout.minimumWidth: 210
                                Layout.maximumWidth: 330
                                Layout.fillHeight: true
                                contentWidth: availableWidth
                                clip: true
                                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                                ColumnLayout {
                                    width: previewDetails.availableWidth
                                    spacing: 4
                                    ThemeControls.ThemeLabel {
                                        objectName: "processPreviewEmptyDetails"
                                        visible: window.previewEmpty
                                        text: "载入机器后显示蒸发源、监测点与修正板。"
                                        Layout.fillWidth: true
                                        wrapMode: Text.WordWrap
                                        color: window.themeColors.colorTextMuted
                                    }
                                    ThemeControls.ThemeLabel { visible: !window.previewEmpty; text: "蒸发源 · " + (bridge.sourcePreview.sources || []).length; font.bold: true }
                                    Repeater {
                                        visible: !window.previewEmpty
                                        model: bridge.sourcePreview.sources || []
                                        delegate: ColumnLayout {
                                            required property var modelData
                                            required property int index
                                            Layout.fillWidth: true
                                            spacing: 2
                                            ThemeControls.ThemeLabel {
                                                objectName: "previewSourceLabel" + modelData.slot
                                                Layout.fillWidth: true
                                                text: "● 源 " + modelData.slot + (modelData.name ? " · " + modelData.name : "")
                                                color: sourceCanvas.sourceColors[index % sourceCanvas.sourceColors.length]
                                                wrapMode: Text.WrapAnywhere
                                                font.bold: true
                                            }
                                            ThemeControls.ThemeLabel { text: modelData.file || ""; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere }
                                            ThemeControls.ThemeLabel {
                                                text: [window.sourceKind(modelData.kind), window.sourceDevice(modelData.device_type)].filter(function(value) { return value.length > 0 }).join(" · ")
                                                visible: text.length > 0
                                                Layout.fillWidth: true
                                                wrapMode: Text.WordWrap
                                                color: window.themeColors.colorTextMuted
                                            }
                                            ThemeControls.ThemeLabel { objectName: "previewSourcePosition" + modelData.slot; text: window.xyz(modelData); Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                        }
                                    }
                                    ThemeControls.ThemeLabel { visible: !window.previewEmpty; text: "监测点 · " + (bridge.sourcePreview.monitors || []).length; font.bold: true; topPadding: 3 }
                                    ThemeControls.ThemeLabel { visible: !window.previewEmpty && !(bridge.sourcePreview.monitors || []).length; text: "未配置监测点"; color: window.themeColors.colorTextMuted }
                                    Repeater {
                                        visible: !window.previewEmpty
                                        model: bridge.sourcePreview.monitors || []
                                        delegate: ColumnLayout {
                                            required property var modelData
                                            Layout.fillWidth: true
                                            spacing: 2
                                            ThemeControls.ThemeLabel {
                                                objectName: "previewMonitorLabel" + modelData.slot
                                                text: "■ 监测点 " + modelData.slot + " · " + modelData.name + " · " + (modelData.kind === "optical" ? "光控" : "晶控")
                                                color: window.themeColors.colorInfo
                                                font.bold: true
                                                Layout.fillWidth: true
                                                wrapMode: Text.WrapAnywhere
                                            }
                                            ThemeControls.ThemeLabel { objectName: "previewMonitorPosition" + modelData.slot; text: window.xyz(modelData); Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                        }
                                    }
                                    ThemeControls.ThemeLabel { visible: !window.previewEmpty; text: "修正板 · " + (bridge.sourcePreview.masks || []).length; font.bold: true; topPadding: 3 }
                                    ThemeControls.ThemeLabel { visible: !window.previewEmpty && !(bridge.sourcePreview.masks || []).length; text: "未安装修正板"; color: window.themeColors.colorTextMuted }
                                    Repeater {
                                        visible: !window.previewEmpty
                                        model: bridge.sourcePreview.masks || []
                                        delegate: Label {
                                            required property var modelData
                                            Layout.fillWidth: true
                                            text: "板 " + (modelData.slot + 1) + " · " + modelData.file
                                                  + (modelData.z_mm === undefined ? "" : " · Z " + modelData.z_mm + " mm")
                                                  + " · " + (modelData.holes || []).length + " 个孔洞"
                                            color: sourceCanvas.maskColors[modelData.slot % sourceCanvas.maskColors.length]
                                            wrapMode: Text.WrapAnywhere
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                ThemeControls.ThemeSection {
                    objectName: "processEnvironmentSection"
                    title: "温度与真空"
                    accessibleLabel: "温度与真空，展开或折叠"
                    expanded: true
                    Layout.fillWidth: true
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 4
                        RowLayout {
                            Layout.fillWidth: true
                            ThemeControls.ThemeButton {
                                objectName: "editEnvironmentDefaults"
                                text: "工艺默认参数"
                                onClicked: environmentDialog.prepare(-1)
                            }
                            ThemeControls.ThemeLabel {
                                objectName: "environmentDefaultsSummary"
                                Layout.fillWidth: true; wrapMode: Text.WordWrap
                                text: bridge.environmentDefaultsSummary
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            ThemeControls.ThemeLabel { text: "单层覆盖" }
                            ThemeControls.ThemeSpinBox {
                                id: environmentStep
                                objectName: "environmentStepChoice"
                                from: 1; to: Math.max(1, bridge.stepCount)
                                editable: true
                                Layout.preferredWidth: 112
                                Layout.maximumWidth: 128
                                onValueChanged: window.refreshEnvironmentSummary()
                            }
                            ThemeControls.ThemeButton {
                                objectName: "editStepEnvironment"
                                text: "编辑第 " + environmentStep.value + " 层"
                                enabled: bridge.stepCount > 0
                                onClicked: environmentDialog.prepare(environmentStep.value - 1)
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true; wrapMode: Text.WordWrap
                                text: "未设置字段继承工艺默认；清空阻止继承。保存后保留每层覆盖。"
                            }
                        }
                        ThemeControls.ThemeLabel {
                            id: stepEnvironmentSummary
                            objectName: "stepEnvironmentSummary"
                            Layout.fillWidth: true; wrapMode: Text.WordWrap
                        }
                        ThemeControls.ThemeLabel {
                            Layout.fillWidth: true; wrapMode: Text.WordWrap
                            color: window.themeColors.colorTextMuted
                            text: "基片 / 源温度、本底压力、气体流量仅记录。零碰撞束损失需显式启用并提供工作绝对压力、气温及截面依据；不预测反应化学、散射沉积或绝对净生长速率。"
                        }
                    }
                }

                GroupBox {
                    title: "镀膜步骤"
                    Layout.fillWidth: true
                    padding: 6
                    topPadding: 25
                    bottomPadding: 6
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 4
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 5
                            ThemeControls.ThemeLabel { text: "序号"; Layout.preferredWidth: 32; Layout.maximumWidth: 32 }
                            ThemeControls.ThemeLabel { text: "已保存蒸发源 / 材料"; Layout.preferredWidth: 184; Layout.maximumWidth: 184 }
                            ThemeControls.ThemeLabel { text: "厚度 / nm"; Layout.preferredWidth: 78; Layout.maximumWidth: 78 }
                            ThemeControls.ThemeLabel { text: "速率 / Å/s"; Layout.preferredWidth: 78; Layout.maximumWidth: 78 }
                            ThemeControls.ThemeLabel { text: "厚度基准"; Layout.preferredWidth: 98; Layout.maximumWidth: 98 }
                            ThemeControls.ThemeLabel { text: "监测位次"; Layout.preferredWidth: 54; Layout.maximumWidth: 54 }
                            ThemeControls.ThemeLabel { text: "修正板"; Layout.preferredWidth: 72; Layout.maximumWidth: 72 }
                            Item { Layout.fillWidth: true }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            ThemeControls.ThemeLabel { text: "共 " + bridge.stepCount + " 层 · 第 " + (bridge.stepPage + 1) + " / " + bridge.stepPageCount + " 页（每页 40 层）"; Layout.fillWidth: true }
                            ThemeControls.ThemeButton { text: "上一页"; enabled: bridge.stepPage > 0; onClicked: bridge.setStepPage(bridge.stepPage - 1) }
                            ThemeControls.ThemeButton { text: "下一页"; enabled: bridge.stepPage + 1 < bridge.stepPageCount; onClicked: bridge.setStepPage(bridge.stepPage + 1) }
                        }
                        Repeater {
                            id: stepRows
                            model: bridge.visibleSteps
                            delegate: RowLayout {
                                id: stepRow
                                required property int index
                                required property var modelData
                                readonly property int stepIndex: bridge.stepPage * 40 + index
                                function focusColumnField(columnName) {
                                    const target = columnName === "thickness" ? stepThickness
                                                 : columnName === "rate" ? stepRate
                                                 : columnName === "monitor" ? stepMonitorSlot : null
                                    if (!target || !target.visible || !target.enabled)
                                        return false
                                    target.focusAndSelectAll()
                                    return true
                                }
                                Layout.fillWidth: true
                                Layout.preferredHeight: 38
                                spacing: 4
                                ThemeControls.ThemeLabel { text: String(stepIndex + 1); Layout.preferredWidth: 32; Layout.maximumWidth: 32 }
                                ThemeControls.ThemeComboBox {
                                    objectName: "stepSourceChoice" + stepIndex
                                    Layout.preferredWidth: 184; Layout.maximumWidth: 220
                                    Layout.minimumWidth: 150
                                    model: bridge.sourcePreview.sources || []
                                    textRole: "label"
                                    currentIndex: modelData.source_slot - 1
                                    displayText: currentIndex >= 0 && currentIndex < count ? currentText : "源位次 " + modelData.source_slot + " 无效"
                                    onActivated: function(choiceIndex) { bridge.chooseStepSource(stepIndex, model[choiceIndex].slot) }
                                    ToolTip.visible: hovered
                                    ToolTip.text: (modelData.source_file || "源文件无效") + "\n材料：" + (modelData.material_name || "未指定材料（兼容旧源）") +
                                        (modelData.film_layer_ref ? "\n膜系来源：" + modelData.film_layer_ref.surface + " / " + modelData.film_layer_ref.layer_id : "") +
                                        "\n选择新源时沿用其参考速率，可在右侧调整"
                                    Accessible.description: modelData.film_layer_ref
                                        ? "膜系层 " + modelData.film_layer_ref.surface + " / " + modelData.film_layer_ref.layer_id
                                        : "普通工艺步骤"
                                }
                                ThemeControls.ThemeField {
                                    id: stepThickness
                                    objectName: "stepThicknessField" + stepIndex
                                    numericDisplayValue: modelData.target_nm
                                    Layout.preferredWidth: 78; Layout.maximumWidth: 88
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    ToolTip.visible: hovered
                                    ToolTip.text: modelData.film_design_thickness_nm === undefined
                                        ? "可编辑机器目标厚度"
                                        : "设计物理厚度快照：" + modelData.film_design_thickness_nm + " nm。此值记录导入设计厚度；当前可编辑目标厚度可与其不同。"
                                    onEditingFinished: if (numericValueEdited) bridge.setStep(stepIndex, "target_nm", text)
                                    Keys.onPressed: function(event) {
                                        handleColumnNavigation(event, function(delta) {
                                            window.moveStepColumnFocus(stepIndex, "thickness", delta)
                                        })
                                    }
                                }
                                ThemeControls.ThemeField {
                                    id: stepRate
                                    objectName: "stepRateField" + stepIndex
                                    numericDisplayValue: modelData.rate_angstrom_s
                                    Layout.preferredWidth: 78; Layout.maximumWidth: 88
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    onEditingFinished: if (numericValueEdited) bridge.setStep(stepIndex, "rate_angstrom_s", text)
                                    Keys.onPressed: function(event) {
                                        handleColumnNavigation(event, function(delta) {
                                            window.moveStepColumnFocus(stepIndex, "rate", delta)
                                        })
                                    }
                                }
                                ThemeControls.ThemeComboBox {
                                    Layout.preferredWidth: 98; Layout.maximumWidth: 112
                                    Layout.minimumWidth: 84
                                    model: ["整片平均", "基片中心", "指定监测器"]
                                    currentIndex: modelData.target_mode === "center" ? 1
                                                  : modelData.target_mode === "monitor" ? 2 : 0
                                    onActivated: bridge.setStep(stepIndex, "target_mode",
                                                               ["mean", "center", "monitor"][currentIndex])
                                }
                                ThemeControls.ThemeField {
                                    id: stepMonitorSlot
                                    objectName: "stepMonitorSlotField" + stepIndex
                                    Layout.preferredWidth: 54; Layout.maximumWidth: 62
                                    enabled: modelData.target_mode === "monitor"
                                    numericDisplayValue: modelData.monitor_slot === undefined
                                          ? "" : String(modelData.monitor_slot)
                                    inputMethodHints: Qt.ImhDigitsOnly
                                    onEditingFinished: if (numericValueEdited) bridge.setStep(stepIndex, "monitor_slot", text)
                                    Keys.onPressed: function(event) {
                                        handleColumnNavigation(event, function(delta) {
                                            window.moveStepColumnFocus(stepIndex, "monitor", delta)
                                        })
                                    }
                                }
                                RowLayout {
                                    Layout.preferredWidth: 72; Layout.maximumWidth: 72
                                    spacing: 2
                                    CheckBox {
                                        text: "1"; enabled: bridge.maskCount > 0
                                        checked: modelData.mask_slots === undefined ? bridge.maskCount > 0 : modelData.mask_slots.indexOf(0) >= 0
                                        onClicked: bridge.setStepMask(stepIndex, 0, checked)
                                    }
                                    CheckBox {
                                        text: "2"; enabled: bridge.maskCount > 1
                                        checked: modelData.mask_slots === undefined ? bridge.maskCount > 1 : modelData.mask_slots.indexOf(1) >= 0
                                        onClicked: bridge.setStepMask(stepIndex, 1, checked)
                                    }
                                }
                                ThemeControls.ThemeButton { flat: true; text: "↑"; onClicked: bridge.moveStep(stepIndex, -1) }
                                ThemeControls.ThemeButton { flat: true; text: "↓"; onClicked: bridge.moveStep(stepIndex, 1) }
                                ThemeControls.ThemeButton { flat: true; text: "×"; onClicked: bridge.deleteStep(stepIndex) }
                                Item { Layout.fillWidth: true }
                            }
                        }
                        ThemeControls.ThemeButton {
                            objectName: "addStepButton"
                            text: "添加步骤"
                            Layout.alignment: Qt.AlignLeft
                            onClicked: bridge.addStep()
                        }
                    }
                }


            }
        }

    }
}
