import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "."
import "theme_controls" as ThemeControls

AppWindow {
    id: window
    property bool projectCloseAuthorized: false
    property var projectNavigation: null
    property var projectResources: null
    titleBase: "沉积仿真"
    projectNameForTitle: projectNavigation ? projectNavigation.projectName : ""
    statusText: bridge.status
    statusSeverity: bridge.busy ? "busy" : "info"
    minimumWidth: 1080
    minimumHeight: 680
    property bool uniformitySidebarExpanded: width >= 1200
    property var uniformitySidebarFocusItem: null
    property int uniformitySidebarFocusRequestSerial: 0
    property var uniformitySidebarNativeF4Combos: []

    onActiveFocusItemChanged: {
        if (window.focusIsInsideUniformitySidebar(activeFocusItem))
            uniformitySidebarFocusItem = activeFocusItem
    }

    function focusIsInsideUniformitySidebar(item) {
        let current = item
        while (current) {
            if (current === uniformitySidebarContent) return true
            current = current.parent
        }
        return false
    }

    function restoreUniformitySidebarFocusLater() {
        const request = ++uniformitySidebarFocusRequestSerial
        Qt.callLater(function() {
            if (request !== window.uniformitySidebarFocusRequestSerial
                    || !window.uniformitySidebarExpanded)
                return
            const target = window.uniformitySidebarFocusItem
            if (target && target.visible && target.enabled && target.forceActiveFocus)
                target.forceActiveFocus()
        })
    }

    function toggleUniformitySidebar() {
        if (uniformitySidebarExpanded) {
            if (focusIsInsideUniformitySidebar(activeFocusItem))
                uniformitySidebarRailToggle.forceActiveFocus()
            ++uniformitySidebarFocusRequestSerial
            uniformitySidebarExpanded = false
        } else {
            uniformitySidebarExpanded = true
            restoreUniformitySidebarFocusLater()
        }
    }

    function openUniformitySidebar() {
        if (!uniformitySidebarExpanded) {
            uniformitySidebarExpanded = true
            restoreUniformitySidebarFocusLater()
        }
    }

    function activeFocusIsInsideNativeF4Combo(item) {
        let current = item
        while (current) {
            if (current.nativeF4Popup === true) return true
            current = current.parent
        }
        return false
    }

    function registerNativeF4Combo(combo) {
        uniformitySidebarNativeF4Combos = (uniformitySidebarNativeF4Combos || []).concat([combo])
    }

    function hasVisibleNativeF4ComboPopup() {
        const combos = uniformitySidebarNativeF4Combos || []
        for (let index = 0; index < combos.length; ++index) {
            const combo = combos[index]
            if (combo && combo.popup && combo.popup.visible) return true
        }
        return false
    }

    Shortcut {
        objectName: "uniformitySidebarShortcut"
        sequence: "F4"
        context: Qt.WindowShortcut
        enabled: !window.activeFocusIsInsideNativeF4Combo(window.activeFocusItem)
                 && !window.hasVisibleNativeF4ComboPopup()
        onActivated: window.toggleUniformitySidebar()
    }

    FileDialog {
        id: processDialog
        currentFolder: bridge.processDirectory
        title: "选择工艺"
        fileMode: FileDialog.OpenFile
        nameFilters: ["工艺文件 (*.cproc)"]
        onAccepted: bridge.chooseFile("process", selectedFile)
    }
    FileDialog {
        id: pdfDialog
        currentFolder: bridge.resultsDirectory
        title: "保存膜厚结果 PDF"
        fileMode: FileDialog.SaveFile
        nameFilters: ["PDF (*.pdf)"]
        defaultSuffix: "pdf"
        onAccepted: bridge.exportPdf(selectedFile)
    }
    ThemeControls.ThemeDialogBase {
        palette: window.palette
        id: pdfOptionsDialog
        objectName: "pdfOptionsDialog"
        title: "PDF 内容"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: {
            const sections = []
            if (pdfHeatmap.checked) sections.push("heatmap")
            if (pdfThickness.checked) sections.push("thickness")
            if (pdfUniformity.checked) sections.push("uniformity")
            if (pdfComputation.checked) sections.push("computation")
            if (pdfSettings.checked) sections.push("settings")
            if (pdfMachine.checked) sections.push("machine")
            if (pdfProcess.checked) sections.push("process")
            if (pdfLayers.checked) sections.push("layers")
            bridge.setPdfSections(sections)
            pdfDialog.selectedFile = bridge.pdfSuggestedUrl
            pdfDialog.open()
        }
        Column {
            spacing: 5
            CheckBox { id: pdfHeatmap; text: "膜厚热图"; checked: true }
            CheckBox { id: pdfThickness; text: "膜厚统计"; checked: true }
            CheckBox { id: pdfUniformity; text: "均匀性"; checked: true }
            CheckBox { id: pdfComputation; text: "计算设备与用时" }
            CheckBox { id: pdfSettings; text: "计算设置" }
            CheckBox { id: pdfMachine; text: "机器结构" }
            CheckBox { id: pdfProcess; text: "工艺方案" }
            CheckBox { id: pdfLayers; text: "各层详细结果" }
        }
    }
    ThemeControls.ThemeDialogBase {
        palette: window.palette
        id: historyDialog
        objectName: "historyDialog"
        title: "计算历史"
        modal: true
        anchors.centerIn: parent
        width: Math.min(window.width - 90, 800)
        height: Math.min(window.height - 90, 580)
        standardButtons: Dialog.Close
        ScrollView {
            palette: window.palette
            anchors.fill: parent
            Column {
                width: parent.width
                spacing: 8
                Repeater {
                    model: bridge.historyRows
                    ThemeControls.ThemeLabel {
                        required property string modelData
                        width: parent.width
                        text: modelData
                        wrapMode: Text.WrapAnywhere
                    }
                }
            }
        }
    }

    Pane {
        id: pagePane
        anchors.fill: parent
        padding: 0
        background: null
        palette: window.palette
        font.family: window.themeColors.fontFamily
        font.pointSize: window.themeColors.fontSizeBody
        font.weight: window.themeColors.fontWeightNormal
        ResponsiveOperationBar {
            id: toolbar
            objectName: "mainToolbar"
            Accessible.name: "操作栏"
            x: 18
            y: window.contentTopMargin
            width: Math.max(0, parent.width - 36)
            height: Math.max(36, window.themeColors.controlHeightSm + 6, implicitHeight)
            spacing: 6
            ProjectModuleSelector {
                id: projectSelector
                navigation: window.projectNavigation
                currentModuleKey: "main"
            }
            ProjectResourceControls {
                projectResources: window.projectResources
                width: visible ? Math.min(520, toolbar.width) : 0
                saveButtonObjectName: "projectDepositionSaveConfiguration"
                beforeDocumentAction: function(action, documentId) {
                    // Explicitly commit numeric text before switching inputs.
                    const basicEditor = window.activeFocusItem as TextField
                    if (basicEditor) basicEditor.editingFinished()
                    toolbar.forceActiveFocus()
                    return !bridge.busy && !bridge.projectEditingFrozen
                }
                extraTransferActions: [
                    {key: "load_default", text: "配置：载入最近保存配置", enabled: !bridge.busy && !bridge.projectEditingFrozen},
                    {key: "result_pdf", text: "结果：导出膜厚 PDF…", enabled: bridge.hasResult && !bridge.busy}
                ]
                onExtraTransferActionRequested: function(key) {
                    if (key === "load_default") window.projectResources.loadDefault()
                    else if (key === "result_pdf") pdfOptionsDialog.open()
                }
            }
            ThemeControls.ThemeButton {
                visible: !window.projectResources
                text: "保存结果 PDF"
                enabled: bridge.hasResult && !bridge.busy
                onClicked: pdfOptionsDialog.open()
            }
            ThemeControls.ThemeButton { objectName: "openHistoryButton"; text: "计算历史"; onClicked: historyDialog.open() }
            ThemeControls.ThemeButton { objectName: "openComputeQueue"; text: "计算队列"; onClicked: bridge.showQueue() }
        }

        Rectangle {
            id: sidebar
            objectName: "simulationInputPane"
            anchors.left: parent.left
            anchors.top: toolbar.bottom
            anchors.bottom: parent.bottom
            width: Math.min(350, Math.max(310, parent.width * 0.25))
            color: window.themeColors.colorWindow
            border.color: window.themeColors.colorBorder

            Flickable {
                id: inputs
                objectName: "simulationInputScroll"
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: bottomActionPanel.top
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                anchors.topMargin: 12
                anchors.bottomMargin: 12
                clip: true
                contentWidth: width
                contentHeight: inputColumn.implicitHeight + 16
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { palette: window.palette }

                Column {
                    id: inputColumn
                    width: inputs.width - 14
                    spacing: 12

                    ThemeControls.ThemeLabel {
                        text: "输入参数"
                        font.bold: true
                        font.pointSize: Math.round(pagePane.font.pointSize * 1.25)
                    }
                    ThemeControls.ThemeLabel {
                        width: parent.width
                        text: "选择工艺后计算膜厚。"
                        wrapMode: Text.WordWrap
                    }

                    GroupBox {
                        title: "工艺"
                        width: parent.width
                        Column {
                            width: parent.width
                            spacing: 7
                            Row {
                                width: parent.width
                                spacing: 6
                                ThemeControls.ThemeButton {
                                    objectName: "chooseProcessButton"
                                    text: bridge.paths.process ? "更换工艺" : "选择工艺"
                                    onClicked: processDialog.open()
                                }
                                ThemeControls.ThemeButton {
                                    objectName: "processEditorActionButton"
                                    text: bridge.paths.process.length > 0 ? "编辑工艺" : "新建工艺"
                                    onClicked: {
                                        if (window.projectNavigation)
                                            window.projectNavigation.openProjectModule("process")
                                        else
                                            launcher.openWindow("process", bridge.paths.process || "")
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel {
                                width: parent.width
                                text: bridge.paths.process || "未选择工艺"
                                elide: Text.ElideMiddle
                            }
                            ThemeControls.ThemeButton {
                                objectName: "reloadCurrentFile"
                                text: "刷新工艺及依赖"
                                Accessible.name: "重新载入当前工艺及全部依赖文件"
                                enabled: bridge.canReload
                                ToolTip.visible: hovered
                                ToolTip.text: "从原路径读取；保留计算参数，不影响已提交任务"
                                onClicked: bridge.reloadCurrent()
                            }
                            ThemeControls.ThemeButton {
                                text: "清除工艺"
                                visible: bridge.paths.process.length > 0
                                onClicked: bridge.clearFile("process")
                            }
                        }
                    }

                    GroupBox {
                        title: "运动"
                        width: parent.width
                        Column {
                            width: parent.width
                            spacing: 6
                            Row {
                                width: parent.width
                                spacing: 8
                                ThemeControls.ThemeLabel { text: "大盘转速 / rpm"; width: parent.width * 0.56 }
                                SimulationValueField {
                                    width: parent.width * 0.44 - 8
                                    controller: bridge
                                    fieldKey: "carrier_rpm"

                                }
                            }
                            Row {
                                width: parent.width
                                spacing: 8
                                ThemeControls.ThemeLabel { text: "大盘初相 / °"; width: parent.width * 0.56 }
                                SimulationValueField {
                                    width: parent.width * 0.44 - 8
                                    controller: bridge
                                    fieldKey: "carrier_phase_deg"

                                }
                            }
                            Row {
                                width: parent.width
                                spacing: 8
                                ThemeControls.ThemeLabel { text: "行星盘初相 / °"; width: parent.width * 0.56 }
                                SimulationValueField {
                                    width: parent.width * 0.44 - 8
                                    controller: bridge
                                    fieldKey: "planet_phase_deg"

                                }
                            }
                        }
                    }

                    GroupBox {
                        title: "计算"
                        width: parent.width
                        Column {
                            width: parent.width
                            spacing: 6
                            ThemeControls.ThemeLabel { text: "计算路径" }
                            ThemeControls.ThemeComboBox {
                                objectName: "algorithmSelector"
                                property bool nativeF4Popup: true
                                Component.onCompleted: window.registerNativeF4Combo(this)
                                width: parent.width
                                model: ["工艺积分", "时间收敛检查", "转速局部优化"]
                                property var algorithmKeys: ["fast", "reference", "optimize"]
                                currentIndex: algorithmKeys.indexOf(bridge.algorithm)
                                enabled: true
                                onActivated: bridge.setAlgorithm(algorithmKeys[currentIndex])
                            }
                            ThemeControls.ThemeLabel {
                                width: parent.width
                                text: bridge.algorithm === "reference"
                                      ? "CPU 有限膜层积分，完整周期与真实相位尾段均保留；按公转/自转设定起始采样，默认最多 6 次评估、无计算时限。只检查时间一致性；未收敛结果不可作为精度证据。空间、源离散与物理模型另验。"
                                      : bridge.algorithm === "optimize"
                                      ? "以当前工艺及大盘转速为初值，仅搜索转速倍率 0.8–1.2；CPU、默认最多 9 次评估、无计算时限，含候选复核。候选通过 8 倍时间采样的初值对照才采用；不保证全局最优或生产改善，不会改写工艺。"
                                      : "按设备与工艺选择周期快算或全时长积分；周期快算保持默认。"
                                wrapMode: Text.WordWrap
                            }
                            Row {
                                width: parent.width
                                spacing: 8
                                visible: bridge.algorithm !== "fast"
                                ThemeControls.ThemeLabel { text: "时限（秒；0=不限）"; anchors.verticalCenter: parent.verticalCenter }
                                ThemeControls.ThemeSpinBox {
                                    objectName: "algorithmBudgetInput"
                                    from: 0; to: 300
                                    value: bridge.algorithmBudgetSeconds
                                    editable: true
                                    enabled: true
                                    onValueModified: bridge.setAlgorithmBudgetSeconds(value)
                                }
                            }
                            Column {
                                width: parent.width
                                spacing: 5
                                visible: bridge.algorithm === "fast"
                                ThemeControls.ThemeLabel {
                                    text: "积分方式"
                                    width: parent.width
                                }
                                ThemeControls.ThemeComboBox {
                                    objectName: "fastCycleSelector"
                                    property bool nativeF4Popup: true
                                    Component.onCompleted: window.registerNativeF4Combo(this)
                                    width: parent.width
                                    model: ["真实时长积分", "周期快算"]
                                    currentIndex: bridge.fastCycle ? 1 : 0
                                    enabled: true
                                    onActivated: bridge.setFastCycle(currentIndex === 1)
                                }
                            }
                            Row {
                                width: parent.width
                                spacing: 8
                                visible: bridge.algorithm === "fast" && bridge.fastCycle
                                ThemeControls.ThemeLabel {
                                    text: "残余时长阈值 / %"
                                    width: parent.width * 0.56
                                    anchors.verticalCenter: parent.verticalCenter
                                }
                                ThemeControls.ThemeField {
                                    width: parent.width * 0.44 - 8
                                    objectName: "fastThresholdField"
                                    numericDisplayValue: bridge.fastThresholdText
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    onTextEdited: bridge.setFastThreshold(text)
                                }
                            }
                            ThemeControls.ThemeButton { flat: true;
                                visible: bridge.algorithm === "fast"
                                text: algorithmHelp.expanded ? "积分方式说明 ▾" : "积分方式说明 ▸"
                                onClicked: algorithmHelp.expanded = !algorithmHelp.expanded
                            }
                            Column {
                                id: algorithmHelp
                                property bool expanded: false
                                width: parent.width
                                spacing: 4
                                visible: expanded && bridge.algorithm === "fast"
                                ThemeControls.ThemeLabel {
                                    width: parent.width
                                    text: "真实时长积分：所有层都按设定速率推得的真实时长、连续传递的初相逐时间步积分；仍受时间与空间采样误差影响。"
                                    wrapMode: Text.WordWrap
                                }
                                ThemeControls.ThemeLabel {
                                    width: parent.width
                                    text: "周期快算：由太阳轮与行星轮齿数求材料点完整回归周期。残余时长占比不超过阈值的所有层共用这一周期的相位采样，各按目标厚度缩放后叠加；相同源位次的周期分布只计算一次。"
                                    wrapMode: Text.WordWrap
                                }
                                ThemeControls.ThemeLabel {
                                    width: parent.width
                                    text: "薄层或残余较大的层继续使用真实时长及连续初相积分。周期快算忽略残余段的初相效应；阈值越大，可能越快，近似也越强。"
                                    wrapMode: Text.WordWrap
                                }
                            }
                            Row {
                                width: parent.width
                                spacing: 8
                                ThemeControls.ThemeLabel { text: "时间步长 / s"; width: parent.width * 0.56 }
                                SimulationValueField {
                                    width: parent.width * 0.44 - 8
                                    controller: bridge
                                    fieldKey: "time_step_s"

                                }
                            }
                            Row {
                                width: parent.width
                                spacing: 8
                                ThemeControls.ThemeLabel { text: "时间步数"; width: parent.width * 0.56 }
                                SimulationValueField {
                                    width: parent.width * 0.44 - 8
                                    controller: bridge
                                    fieldKey: "time_steps"
                                    inputMethodHints: Qt.ImhDigitsOnly
                                }
                            }
                            Row {
                                width: parent.width
                                spacing: 8
                                ThemeControls.ThemeLabel { text: "网格边长"; width: parent.width * 0.56 }
                                SimulationValueField {
                                    width: parent.width * 0.44 - 8
                                    controller: bridge
                                    fieldKey: "grid_size"
                                    inputMethodHints: Qt.ImhDigitsOnly
                                }
                            }
                        }
                    }

                    ThemeControls.ThemeLabel {
                        width: parent.width
                        text: bridge.memoryEstimate
                        wrapMode: Text.WordWrap
                        color: window.themeColors.colorTextMuted
                    }

                    ThemeControls.ThemeButton { flat: true;
                        text: quickGeometry.visible ? "机器几何预览 ▾" : "机器几何预览 ▸"
                        onClicked: quickGeometry.visible = !quickGeometry.visible
                    }
                    Column {
                        id: quickGeometry
                        width: parent.width
                        visible: false
                        spacing: 7
                        ThemeControls.ThemeLabel {
                            width: parent.width
                            text: bridge.paths.process
                                  ? "以下为工艺所引用的机器结构（只读）。"
                                  : "选择工艺后显示机器结构。"
                            wrapMode: Text.WordWrap
                        }
                        Repeater {
                            model: bridge.geometryRows
                            delegate: Label {
                                required property string modelData
                                width: quickGeometry.width
                                text: modelData
                                wrapMode: Text.WordWrap
                            }
                        }
                    }
                }
            }

            Rectangle {
                id: bottomActionPanel
                objectName: "simulationBottomPanel"
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 112
                color: window.themeColors.colorWindow
                border.color: window.themeColors.colorBorder
                Column {
                    anchors.fill: parent
                    anchors.margins: 9
                    spacing: 6
                    ProgressBar {
                        objectName: "algorithmProgress"
                        width: parent.width
                        visible: bridge.busy
                        from: 0; to: 1
                        value: Math.max(0, bridge.progressValue)
                        indeterminate: bridge.progressValue < 0
                    }
                    ThemeControls.ThemeLabel {
                        width: parent.width
                        text: bridge.estimate
                        elide: Text.ElideRight
                    }
                    Row {
                        width: parent.width
                        spacing: 9
                        ComputeTargetSelector {
                            width: 106
                            compact: true
                            target: bridge.computeTarget
                            registerNativeF4Combo: window.registerNativeF4Combo
                        }
                        ThemeControls.ThemeButton {
                            text: bridge.resultViewOnly ? "已完成结果（只读）" : (bridge.busy ? "提交当前输入到队列" : "开始计算")
                            enabled: !bridge.resultViewOnly && bridge.paths.process.length > 0
                                     && bridge.computeTarget.canSubmit
                                     && (bridge.algorithm !== "fast" || !bridge.fastCycle || !bridge.fastThresholdInvalid)
                            objectName: "calculateButton"
                            onClicked: bridge.calculate()
                        }
                        ThemeControls.ThemeButton {
                            objectName: "cancelCalculationButton"
                            text: "取消"
                            enabled: bridge.busy
                            onClicked: bridge.cancel()
                        }
                    }
                    ThemeControls.ThemeLabel {
                        width: parent.width
                        text: bridge.computeTarget.message
                        wrapMode: Text.WordWrap
                        color: window.themeColors.colorTextMuted
                    }
                }
            }
        }

        Item {
            id: resultArea
            anchors.left: sidebar.right
            anchors.right: parent.right
            anchors.top: toolbar.bottom
            anchors.bottom: parent.bottom
            anchors.margins: 14

            RowLayout {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                spacing: 15

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 8
                    ThemeControls.ThemeLabel {
                        text: bridge.hasResult ? "膜厚分布" : "等待计算"
                        font.bold: true
                        Layout.alignment: Qt.AlignHCenter
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        spacing: 8
                        Item {
                            id: chartArea
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            property real plotSize: Math.max(0, Math.min(width - 94, height - 66))

                            Rectangle {
                                id: chartPlot
                                objectName: "depositionChartPlot"
                                x: 68 + (chartArea.width - 94 - chartArea.plotSize) / 2
                                y: 8 + (chartArea.height - 66 - chartArea.plotSize) / 2
                                width: chartArea.plotSize
                                height: chartArea.plotSize
                                color: window.themeColors.colorSurface
                                border.color: window.themeColors.colorBorder
                                property var selectedThickness: null
                                property int displayedImageRevision: bridge.imageRevision
                                property bool displayedResultAvailable: bridge.hasResult
                                onDisplayedImageRevisionChanged: selectedThickness = null
                                onDisplayedResultAvailableChanged: {
                                    if (!displayedResultAvailable)
                                        selectedThickness = null
                                }
                                Image {
                                    id: fieldImage
                                    objectName: "depositionFieldImage"
                                    anchors.fill: parent
                                    visible: bridge.hasResult
                                    source: "image://thickness/field?rev=" + bridge.imageRevision
                                    cache: false
                                    fillMode: Image.PreserveAspectFit
                                    horizontalAlignment: Image.AlignHCenter
                                    verticalAlignment: Image.AlignVCenter
                                    smooth: true
                                }
                                TapHandler {
                                    objectName: "depositionTapHandler"
                                    acceptedButtons: Qt.LeftButton
                                    gesturePolicy: TapHandler.DragThreshold
                                    enabled: bridge.hasResult
                                    onTapped: function(eventPoint, button) {
                                        const point = eventPoint.position
                                        const topLeft = fieldImage.mapToItem(chartPlot, Qt.point(
                                            (fieldImage.width - fieldImage.paintedWidth) / 2,
                                            (fieldImage.height - fieldImage.paintedHeight) / 2))
                                        const bottomRight = fieldImage.mapToItem(chartPlot, Qt.point(
                                            (fieldImage.width + fieldImage.paintedWidth) / 2,
                                            (fieldImage.height + fieldImage.paintedHeight) / 2))
                                        chartPlot.selectedThickness = bridge.sampleThicknessAt(
                                            point.x, point.y,
                                            topLeft.x, topLeft.y,
                                            bottomRight.x - topLeft.x,
                                            bottomRight.y - topLeft.y)
                                    }
                                }
                                Rectangle {
                                    visible: chartPlot.selectedThickness !== null
                                             && chartPlot.selectedThickness.inside_image
                                    x: chartPlot.selectedThickness
                                       ? chartPlot.selectedThickness.marker_x - width / 2 : 0
                                    y: chartPlot.selectedThickness
                                       ? chartPlot.selectedThickness.marker_y - height / 2 : 0
                                    width: 12
                                    height: 12
                                    radius: 6
                                    color: chartPlot.selectedThickness
                                           && chartPlot.selectedThickness.has_data
                                            ? window.themeColors.colorSelection : window.themeColors.colorSurface
                                    border.color: window.themeColors.colorSurface
                                    border.width: 2
                                    z: 2
                                }
                                Rectangle {
                                    id: thicknessSelectionInfo
                                    objectName: "depositionSelectionInfo"
                                    visible: chartPlot.selectedThickness !== null
                                    x: 8
                                    y: 8
                                    width: Math.max(0, Math.min(320, chartPlot.width - 16))
                                    height: selectionInfoText.implicitHeight + 12
                                    color: window.themeColors.colorSurfaceRaised
                                    border.color: window.themeColors.colorBorder
                                    radius: 4
                                    z: 3
                                    Text {
                                        id: selectionInfoText
                                        objectName: "depositionSelectionText"
                                        anchors.fill: parent
                                        anchors.margins: 6
                                        color: window.themeColors.colorText
                                        font.pointSize: Math.max(8, pagePane.font.pointSize - 2)
                                        wrapMode: Text.Wrap
                                        text: {
                                            const sample = chartPlot.selectedThickness
                                            if (!sample)
                                                return ""
                                            if (!sample.inside_image)
                                                return "图像留白：无数据"
                                            const location = "最近网格 · X " + Number(sample.x_mm).toFixed(2)
                                                + " mm · Y " + Number(sample.y_mm).toFixed(2) + " mm"
                                            const value = sample.has_data
                                                ? "厚度 " + Number(sample.value).toFixed(2)
                                                  + " " + sample.unit
                                                : "厚度：无数据"
                                            return location + " · " + value
                                        }
                                    }
                                }
                            }
                            Repeater {
                                model: bridge.hasResult ? 5 : 0
                                delegate: Item {
                                    x: chartPlot.x + index * chartPlot.width / 4
                                    y: chartPlot.y + chartPlot.height
                                    Rectangle {
                                        width: 1
                                        height: 6
                                        color: window.themeColors.colorBorderStrong
                                    }
                                    ThemeControls.ThemeLabel {
                                        x: -implicitWidth / 2
                                        y: 7
                                        text: (Number(bridge.axisBounds.x_min)
                                               + index * (Number(bridge.axisBounds.x_max)
                                                          - Number(bridge.axisBounds.x_min)) / 4).toFixed(0)
                                        font.pointSize: Math.max(8, pagePane.font.pointSize - 2)
                                    }
                                }
                            }
                            Repeater {
                                model: bridge.hasResult ? 5 : 0
                                delegate: Item {
                                    x: chartPlot.x
                                    y: chartPlot.y + chartPlot.height - index * chartPlot.height / 4
                                    Rectangle {
                                        x: -6
                                        width: 6
                                        height: 1
                                        color: window.themeColors.colorBorderStrong
                                    }
                                    ThemeControls.ThemeLabel {
                                        x: -implicitWidth - 9
                                        y: -implicitHeight / 2
                                        text: (Number(bridge.axisBounds.y_min)
                                               + index * (Number(bridge.axisBounds.y_max)
                                                          - Number(bridge.axisBounds.y_min)) / 4).toFixed(0)
                                        font.pointSize: Math.max(8, pagePane.font.pointSize - 2)
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel {
                                visible: bridge.hasResult
                                text: "X / mm"
                                x: chartPlot.x + (chartPlot.width - width) / 2
                                y: chartPlot.y + chartPlot.height + 34
                            }
                            ThemeControls.ThemeLabel {
                                visible: bridge.hasResult
                                text: "Y / mm"
                                x: chartPlot.x - 61
                                y: chartPlot.y + (chartPlot.height - height) / 2
                                rotation: -90
                            }
                        }
                        Item {
                            objectName: "depositionColorbar"
                            Layout.preferredWidth: 91
                            Layout.fillHeight: true
                            visible: bridge.hasResult
                            Image {
                                x: 0
                                y: chartPlot.y
                                width: 22
                                height: chartPlot.height
                                source: "image://thickness/legend?rev=" + bridge.imageRevision
                                cache: false
                                fillMode: Image.Stretch
                            }
                            Repeater {
                                model: 7
                                delegate: Item {
                                    y: chartPlot.y + index * chartPlot.height / 6
                                    Rectangle {
                                        x: 22
                                        y: -0.5
                                        width: 6
                                        height: 1
                                        color: window.themeColors.colorBorderStrong
                                    }
                                    ThemeControls.ThemeLabel {
                                        x: 30
                                        y: -implicitHeight / 2
                                        text: (bridge.imageMaximum
                                               - index * (bridge.imageMaximum - bridge.imageMinimum) / 6).toFixed(1)
                                        font.pointSize: Math.max(8, pagePane.font.pointSize - 2)
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel {
                                text: bridge.imageUnit
                                x: 0
                                y: chartPlot.y + chartPlot.height + 34
                            }
                        }
                    }
                }

                Rectangle {
                    id: metricsPanel
                    objectName: "resultMetricsSidebar"
                    color: window.themeColors.colorSurface
                    border.color: window.themeColors.colorBorder
                    radius: window.themeColors.radiusSm
                    Layout.preferredWidth: window.uniformitySidebarExpanded ? 340 : 64
                    Layout.minimumWidth: window.uniformitySidebarExpanded ? 320 : 64
                    Layout.maximumWidth: window.uniformitySidebarExpanded ? 360 : 64
                    Layout.fillHeight: true

                    RowLayout {
                        anchors.fill: parent
                        spacing: 0

                        ColumnLayout {
                            id: uniformitySidebarRail
                            objectName: "uniformitySidebarRail"
                            Layout.preferredWidth: 64
                            Layout.minimumWidth: 64
                            Layout.maximumWidth: 64
                            Layout.fillHeight: true
                            spacing: window.themeColors.spaceXs

                            ToolButton {
                                objectName: "uniformitySidebarButton"
                                Layout.fillWidth: true
                                Layout.preferredHeight: 64
                                property bool selected: window.uniformitySidebarExpanded
                                Accessible.name: "均匀性分析"
                                Accessible.description: selected
                                                          ? "均匀性分析侧栏已展开"
                                                          : "均匀性分析侧栏已收起"
                                ToolTip.visible: hovered
                                ToolTip.text: "均匀性分析"
                                contentItem: Label {
                                    text: "均匀性"
                                    wrapMode: Text.WordWrap
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                    color: parent.selected ? window.themeColors.colorTextOnAccent
                                                           : window.themeColors.colorText
                                    font.family: window.themeColors.fontFamily
                                    font.pointSize: window.themeColors.fontSizeBody
                                }
                                background: Rectangle {
                                    color: parent.selected ? window.themeColors.colorAccent
                                                           : "transparent"
                                    radius: window.themeColors.radiusSm
                                }
                                onClicked: window.openUniformitySidebar()
                            }

                            ThemeControls.ThemeDoubleChevron {
                                id: uniformitySidebarRailToggle
                                objectName: "uniformitySidebarToggle"
                                Layout.alignment: Qt.AlignHCenter
                                Layout.preferredWidth: 24
                                Layout.minimumWidth: 24
                                Layout.maximumWidth: 24
                                Layout.preferredHeight: window.themeColors.controlHeightLg
                                Layout.minimumHeight: window.themeColors.controlHeightLg
                                Layout.maximumHeight: window.themeColors.controlHeightLg
                                text: window.uniformitySidebarExpanded ? "折叠均匀性侧栏" : "展开均匀性侧栏"
                                direction: window.uniformitySidebarExpanded ? "right" : "left"
                                Accessible.name: window.uniformitySidebarExpanded
                                                ? "折叠均匀性侧栏（F4）"
                                                : "展开均匀性侧栏（F4）"
                                ToolTip.visible: hovered
                                ToolTip.text: Accessible.name
                                onClicked: window.toggleUniformitySidebar()
                            }

                            Item { Layout.fillHeight: true }
                        }

                        ThemeControls.ThemeSidebar {
                            id: uniformitySidebarContent
                            objectName: "uniformitySidebarContent"
                            palette: window.palette
                            title: "均匀性分析"
                            sidebarWidth: 276
                            visible: window.uniformitySidebarExpanded
                            Layout.fillWidth: window.uniformitySidebarExpanded
                            Layout.minimumWidth: 0
                            Layout.preferredWidth: window.uniformitySidebarExpanded ? 276 : 0
                            Layout.maximumWidth: window.uniformitySidebarExpanded ? 296 : 0
                            Layout.fillHeight: true
                            background: Item { }

                            ThemeControls.ThemeLabel {
                                objectName: "uniformityState"
                                text: bridge.uniformityStateText
                                visible: text.length > 0
                                wrapMode: Text.Wrap
                                Layout.fillWidth: true
                                color: bridge.uniformityStateSeverity === "error"
                                       ? window.themeColors.colorError
                                       : bridge.uniformityStateSeverity === "warning"
                                         ? window.themeColors.colorWarning
                                         : bridge.uniformityStateSeverity === "success"
                                           ? window.themeColors.colorSuccess
                                           : bridge.uniformityStateSeverity === "busy"
                                             ? window.themeColors.colorAccent
                                             : window.themeColors.colorTextMuted
                                Accessible.name: text
                            }

                            ColumnLayout {
                                id: uniformitySidebarRows
                                objectName: "uniformityRowsGrid"
                                visible: bridge.uniformityRows.length > 0
                                Layout.fillWidth: true
                                spacing: window.themeColors.spaceXs
                                Repeater {
                                    model: bridge.uniformityRows
                                    delegate: RowLayout {
                                        required property var modelData
                                        objectName: "uniformityRow_" + modelData.key
                                        Layout.fillWidth: true
                                        spacing: window.themeColors.spaceSm
                                        Accessible.name: modelData.label + " " + modelData.value
                                        ThemeControls.ThemeLabel {
                                            objectName: "uniformityLabel_" + modelData.key
                                            text: modelData.label
                                            font.pointSize: window.themeColors.fontSizeSmall
                                            wrapMode: Text.WordWrap
                                            Layout.preferredWidth: uniformitySidebarContent.width * 0.68
                                            Layout.maximumWidth: uniformitySidebarContent.width * 0.68
                                            Layout.alignment: Qt.AlignTop
                                        }
                                        ThemeControls.ThemeLabel {
                                            objectName: "uniformityValue_" + modelData.key
                                            text: modelData.value
                                            font.pointSize: window.themeColors.fontSizeSmall
                                            wrapMode: Text.WordWrap
                                            Layout.fillWidth: true
                                            Layout.alignment: Qt.AlignTop
                                        }
                                    }
                                }
                            }

                            ThemeControls.ThemeLabel {
                                objectName: "uniformityDefinitions"
                                muted: true
                                font.pointSize: window.themeColors.fontSizeSmall
                                Layout.fillWidth: true
                                wrapMode: Text.WrapAnywhere
                                text: "PV 为最大−最小；百分比均以平均膜厚为基准。\nRMS 为相对平均值的均方根偏差；相对 RMS 即 CV。"
                            }
                        }
                    }
                }
            }
        }
    }
}
