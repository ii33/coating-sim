pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ThemeControls.ThemeDialogBase {
    id: dialog
    objectName: "maskOptimizationDialog"
    property var hostWindow: null
    property var optimizer: null
    property var projectResources: null
    property bool optimizationParametersExpanded: true
    palette: dialog.hostWindow ? dialog.hostWindow.palette : undefined
    title: "修正板优化"
    modal: true
    anchors.centerIn: parent
    width: Math.max(320, Math.min(760, parent.width - 32))
    height: Math.max(280, Math.min(760, parent.height - 40))
    standardButtons: Dialog.NoButton
    onAboutToShow: {
        if (dialog.optimizer && !dialog.optimizer.optimization.busy)
            dialog.optimizer.refreshOptimizationDefault()
    }

    function configuredParameter(key, fallback) {
        const parameters = dialog.optimizer ? (dialog.optimizer.optimizationParameters || ({})) : ({})
        return Object.prototype.hasOwnProperty.call(parameters, key) ? parameters[key] : fallback
    }
    function configuredSetting(key, fallback) {
        const parameters = dialog.optimizer ? (dialog.optimizer.optimizationParameters || ({})) : ({})
        const settings = parameters.settings || ({})
        return Object.prototype.hasOwnProperty.call(settings, key) ? settings[key] : fallback
    }
    function optimizationArtifacts() {
        const state = dialog.optimizer ? dialog.optimizer.optimization : null
        return state && state.artifacts ? state.artifacts : ({})
    }
    property var parameterOverrides: ({})
    function effectiveSetting(key, fallback) {
        return Object.prototype.hasOwnProperty.call(parameterOverrides, key)
                ? parameterOverrides[key] : configuredSetting(key, fallback)
    }
    function effectiveParameter(key, fallback) {
        return Object.prototype.hasOwnProperty.call(parameterOverrides, key)
                ? parameterOverrides[key] : configuredParameter(key, fallback)
    }
    function rememberOverride(key, value) {
        const updated = Object.assign({}, parameterOverrides)
        updated[key] = value
        parameterOverrides = updated
    }
    function resetConfiguredParameterOverrides() {
        parameterOverrides = ({})
        timeStepEdited = false
    }
    property bool timeStepEdited: false

    component BoardChoice: CheckBox {
        id: boardChoice
        required property var modelData
        objectName: "optimizationBoardChoice" + boardChoice.modelData.slot
        text: String(boardChoice.modelData.slot + 1)
        checked: boardChoice.modelData.slot === 0
        enabled: boardChoice.modelData.installed || optMode.currentIndex === 0
    }

    component BoundsEditor: GridLayout {
        id: boundsRow
        required property var modelData
        property int slot: modelData.slot
        property var bounds: ({slot: boundsRow.slot,
            z_mm: Number(boundZ.numericValueEdited ? boundZ.numericEditText : boundZ.numericDisplayValue),
            x_min_mm: Number(boundXMin.numericValueEdited ? boundXMin.numericEditText : boundXMin.numericDisplayValue),
            x_max_mm: Number(boundXMax.numericValueEdited ? boundXMax.numericEditText : boundXMax.numericDisplayValue),
            y_min_mm: Number(boundYMin.numericValueEdited ? boundYMin.numericEditText : boundYMin.numericDisplayValue),
            y_max_mm: Number(boundYMax.numericValueEdited ? boundYMax.numericEditText : boundYMax.numericDisplayValue)})
        columns: 4
        visible: dialog.optimizationParametersExpanded
        Layout.fillWidth: true
        ThemeControls.ThemeLabel { text: "板 " + (boundsRow.slot + 1) + (boundsRow.modelData.installed ? " 已载入 / mm" : " 新设计 / mm"); Layout.columnSpan: 4 }
        ThemeControls.ThemeLabel { text: "Z" }
        ThemeControls.ThemeField { id: boundZ; objectName: "optimizationBoundZField" + boundsRow.slot; numericDisplayValue: boundsRow.modelData.z_mm; enabled: !boundsRow.modelData.installed; Layout.fillWidth: true; onTextEdited: numericDisplayValue = text }
        ThemeControls.ThemeLabel { text: "X 最小" }
        ThemeControls.ThemeField { id: boundXMin; objectName: "optimizationBoundXMinField" + boundsRow.slot; numericDisplayValue: boundsRow.modelData.x_min_mm; Layout.fillWidth: true; onTextEdited: numericDisplayValue = text }
        ThemeControls.ThemeLabel { text: "X 最大" }
        ThemeControls.ThemeField { id: boundXMax; numericDisplayValue: boundsRow.modelData.x_max_mm; Layout.fillWidth: true; onTextEdited: numericDisplayValue = text }
        ThemeControls.ThemeLabel { text: "Y 最小" }
        ThemeControls.ThemeField { id: boundYMin; numericDisplayValue: boundsRow.modelData.y_min_mm; Layout.fillWidth: true; onTextEdited: numericDisplayValue = text }
        ThemeControls.ThemeLabel { text: "Y 最大" }
        ThemeControls.ThemeField { id: boundYMax; numericDisplayValue: boundsRow.modelData.y_max_mm; Layout.fillWidth: true; onTextEdited: numericDisplayValue = text }
    }

    contentItem: ScrollView {
        objectName: "maskOptimizationScroll"
        clip: true
        contentWidth: availableWidth
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        ColumnLayout {
            id: content
            width: parent.width
            spacing: 8

            ThemeControls.ThemeLabel {
                objectName: "optimizationDefaultSummary"
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: dialog.optimizer ? dialog.optimizer.optimizationDefaultSummary : ""
                font.bold: true
            }
            ThemeControls.ThemeButton {
                objectName: "refreshOptimizationDefaultButton"
                text: "重新解析默认工艺与机器"
                enabled: dialog.optimizer && !dialog.optimizer.optimization.busy
                onClicked: dialog.optimizer.refreshOptimizationDefault()
            }
            ThemeControls.ThemeButton {
                objectName: "resetOptimizationParametersButton"
                text: "从默认配置重置参数"
                enabled: dialog.optimizer && !dialog.optimizer.optimization.busy
                onClicked: dialog.resetConfiguredParameterOverrides()
            }
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: "未手动编辑的字段会跟随重新解析后的默认配置；手动修改会作为本次优化覆盖项保留。点“从默认配置重置参数”可清除这些覆盖。"
            }
            ThemeControls.ThemeButton {
                id: optimizationToggle
                objectName: "optimizationToggle"
                text: checked ? "优化参数（收起）" : "优化参数（展开）"
                checkable: true
                checked: dialog.optimizationParametersExpanded
                onToggled: dialog.optimizationParametersExpanded = checked
                Layout.fillWidth: true
            }
            GridLayout {
                objectName: "optimizationParametersGrid"
                visible: dialog.optimizationParametersExpanded
                columns: dialog.width < 530 ? 2 : 4
                columnSpacing: 6
                rowSpacing: 4
                Layout.fillWidth: true
                ThemeControls.ThemeLabel { text: "优化模式" }
                ThemeControls.ThemeComboBox { id: optMode; objectName: "optimizationModeSelector"; model: ["从零自动设计", "优化已载入轮廓", "固定板 + 可变板"]; Layout.fillWidth: true }
                ThemeControls.ThemeLabel { text: "目标源位次" }
                ThemeControls.ThemeSpinBox { id: optSource; from: 1; to: Math.max(1, dialog.optimizer ? dialog.optimizer.optimizationSourceCount : 1); value: 1; Layout.fillWidth: true }
                ThemeControls.ThemeLabel { text: "可变板初形"; visible: optMode.currentIndex === 2 }
                ThemeControls.ThemeComboBox { id: optVariableMode; objectName: "optimizationVariableModeSelector"; visible: optMode.currentIndex === 2; model: ["从零自动设计", "优化已载入轮廓"]; Layout.fillWidth: true }
                ThemeControls.ThemeLabel { text: "固定板"; visible: optMode.currentIndex === 2 }
                ThemeControls.ThemeComboBox {
                    id: optFixedBoard
                    objectName: "optimizationFixedBoardSelector"
                    visible: optMode.currentIndex === 2
                    model: dialog.optimizer ? Array.from({length: dialog.optimizer.optimizationMaskCount}, (_, index) => "板 " + (index + 1)) : []
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeLabel { text: "大盘 / rpm" }
                ThemeControls.ThemeField {
                    id: optRpm
                    objectName: "optimizationCarrierRpmField"
                    text: String(dialog.effectiveSetting("carrier_rpm", 15))
                    Layout.fillWidth: true
                    onTextEdited: dialog.rememberOverride("carrier_rpm", text)
                }
                ThemeControls.ThemeLabel { text: "时间步长 / s" }
                ThemeControls.ThemeField {
                    id: optSteps
                    objectName: "optimizationTimeStepField"
                    text: String(dialog.effectiveParameter("_optimization_time_step_s", 0.025))
                    Layout.fillWidth: true
                    onTextEdited: {
                        dialog.timeStepEdited = true
                        dialog.rememberOverride("_optimization_time_step_s", text)
                    }
                }
                ThemeControls.ThemeLabel { text: "复算网格" }
                ThemeControls.ThemeField {
                    id: optGrid
                    objectName: "optimizationGridSizeField"
                    text: String(dialog.effectiveSetting("grid_size", 181))
                    Layout.fillWidth: true
                    onTextEdited: dialog.rememberOverride("grid_size", text)
                }
                ThemeControls.ThemeLabel { text: "大盘初相 / °" }
                ThemeControls.ThemeField {
                    id: optCarrierPhase
                    objectName: "optimizationCarrierPhaseField"
                    text: String(dialog.effectiveSetting("carrier_phase_deg", 0))
                    Layout.fillWidth: true
                    onTextEdited: dialog.rememberOverride("carrier_phase_deg", text)
                }
                ThemeControls.ThemeLabel { text: "行星初相 / °" }
                ThemeControls.ThemeField {
                    id: optPlanetPhase
                    objectName: "optimizationPlanetPhaseField"
                    text: String(dialog.effectiveSetting("planet_phase_deg", 0))
                    Layout.fillWidth: true
                    onTextEdited: dialog.rememberOverride("planet_phase_deg", text)
                }
                ThemeControls.ThemeLabel { text: "计算设备" }
                ThemeControls.ThemeComboBox {
                    id: optBackend
                    objectName: "optimizationBackendSelector"
                    model: ["自动", "CPU", "GPU", "DirectML"]
                    currentIndex: model.indexOf(String(dialog.effectiveParameter("backend", "DirectML")))
                    Layout.fillWidth: true
                    onActivated: index => dialog.rememberOverride("backend", model[index])
                }
                ThemeControls.ThemeLabel { text: "搜索网格" }
                ThemeControls.ThemeField {
                    id: optSearchGrid
                    text: String(Math.min(81, Number(dialog.configuredSetting("grid_size", 181))))
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeLabel { text: "评分预算" }
                ThemeControls.ThemeField { id: optBudget; text: "48"; Layout.fillWidth: true }
                ThemeControls.ThemeLabel { text: "控制点数" }
                ThemeControls.ThemeField { id: optControls; text: "8"; Layout.fillWidth: true }
                ThemeControls.ThemeLabel { text: "最小径向 / mm" }
                ThemeControls.ThemeField { id: optRadius; text: "0"; Layout.fillWidth: true }
                ThemeControls.ThemeLabel { text: "最小边长 / mm" }
                ThemeControls.ThemeField { id: optEdge; text: "0"; Layout.fillWidth: true }
                ThemeControls.ThemeLabel { text: "面积上限 / %" }
                ThemeControls.ThemeField { id: optArea; text: "100"; Layout.fillWidth: true }
                ThemeControls.ThemeLabel { text: "优化评分方式" }
                ThemeControls.ThemeComboBox {
                    id: optAlgorithm
                    objectName: "optimizationAlgorithmSelector"
                    model: ["精确", "快速"]
                    currentIndex: dialog.effectiveParameter("fast_cycle", true) ? 1 : 0
                    Layout.fillWidth: true
                    onActivated: index => dialog.rememberOverride("fast_cycle", index === 1)
                }
                ThemeControls.ThemeLabel { text: "残余阈值 / %" }
                ThemeControls.ThemeField {
                    id: optThreshold
                    objectName: "optimizationThresholdField"
                    text: String(dialog.effectiveParameter("fast_cycle_residual_percent", 100))
                    Layout.fillWidth: true
                    onTextEdited: dialog.rememberOverride("fast_cycle_residual_percent", text)
                }
                ThemeControls.ThemeLabel {
                    objectName: "optimizationScoringNotice"
                    Layout.columnSpan: dialog.width < 530 ? 2 : 4
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: {
                        const algorithm = String(dialog.configuredParameter("algorithm", ""))
                        return algorithm.length
                                ? "默认沉积算法「" + algorithm + "」及 algorithm_options 不传入修正板评分器；当前评分仅使用上方精确/周期快评开关。"
                                : "修正板评分器使用上方精确/周期快评开关；不映射主沉积 algorithm 或 algorithm_options。"
                    }
                }
                ThemeControls.ThemeLabel { text: optMode.currentIndex === 2 ? "可变板" : "优化板" }
                Flow {
                    Layout.fillWidth: true
                    spacing: 6
                    visible: optMode.currentIndex !== 2
                    Repeater {
                        id: optBoards
                        model: dialog.optimizer ? dialog.optimizer.optimizationMasks : []
                        delegate: BoardChoice { }
                    }
                }
                ThemeControls.ThemeComboBox {
                    id: optVariableBoard
                    objectName: "optimizationVariableBoardSelector"
                    visible: optMode.currentIndex === 2
                    model: dialog.optimizer ? dialog.optimizer.optimizationMasks.filter(board => board.slot !== optFixedBoard.currentIndex && (board.installed || optVariableMode.currentIndex === 0)) : []
                    valueRole: "slot"
                    displayText: currentIndex >= 0 ? "板 " + (Number(currentValue) + 1) : "请选择可变板"
                    delegate: ItemDelegate {
                        required property var modelData
                        width: optVariableBoard.width
                        text: "板 " + (modelData.slot + 1)
                    }
                    Layout.fillWidth: true
                }
            }
            Repeater {
                id: optBounds
                model: dialog.optimizer ? dialog.optimizer.optimizationMasks : []
                delegate: BoundsEditor { }
            }
            ThemeControls.ThemeLabel {
                visible: dialog.optimizationParametersExpanded
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: "从零设计无需预画板；已载入模式从原顶点轮廓开始调整。固定板模式保留原板及孔洞，仅改变另一块板，两块板都参与目标源膜层的遮挡计算。新板默认高度取源与基片中心之间的中点，包络按源、盘心和轨道给出初值（本项目约定），请按设备确认；启动时检查源和基片最低点间隙。完整物理模型评分，原网格复算，结果另存完整副本；自动设计不承诺优于已有板或满足制造条件。"
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 6
                ThemeControls.ThemeField { id: maskName; objectName: "optimizedMaskNameField"; Layout.fillWidth: true; text: "优化修正板"; placeholderText: "新修正板名称" }
                ThemeControls.ThemeButton {
                    id: optStart
                    objectName: "startOptimizationButton"
                    text: "开始优化"
                    enabled: dialog.optimizer && dialog.optimizer.optimizationReady && !dialog.optimizer.optimization.busy
                    onClicked: {
                        const slots = []
                        if (optMode.currentIndex === 2) {
                            if (optVariableBoard.currentIndex >= 0) slots.push(Number(optVariableBoard.currentValue))
                        } else {
                            for (let i = 0; i < optBoards.count; ++i) {
                                const choice = optBoards.itemAt(i) as BoardChoice
                                if (choice && choice.checked && choice.enabled) slots.push(choice.modelData.slot)
                            }
                        }
                        const bounds = []
                        for (let i = 0; i < optBounds.count; ++i) {
                            const item = optBounds.itemAt(i) as BoundsEditor
                            if (slots.indexOf(item.slot) >= 0) bounds.push(item.bounds)
                        }
                        dialog.optimizer.startOptimization({carrier_rpm: Number(optRpm.text), time_step_s: Number(optSteps.text),
                            grid_size: Number(optGrid.text), carrier_phase_deg: Number(optCarrierPhase.text),
                            planet_phase_deg: Number(optPlanetPhase.text)},
                            {mode: ["auto_design", "optimize_loaded", "fixed_plus_variable"][optMode.currentIndex],
                             variable_mode: ["auto_design", "optimize_loaded"][optVariableMode.currentIndex],
                             fixed_mask_slots: optMode.currentIndex === 2 ? [optFixedBoard.currentIndex] : [],
                             source_slot: optSource.value, allow_two_masks: slots.length > 1,
                             mask_slots: slots, mask_bounds: bounds, backend: optBackend.currentText,
                             _time_step_edited: dialog.timeStepEdited,
                             _default_configuration_id: dialog.optimizer.optimizationDefaultConfigurationId,
                             _default_project_revision: dialog.optimizer.optimizationDefaultRevision,
                             fast_cycle: optAlgorithm.currentIndex === 1, fast_cycle_residual_percent: Number(optThreshold.text),
                             search_grid_size: Number(optSearchGrid.text), max_evaluations: Number(optBudget.text),
                             control_points: Number(optControls.text), min_radial_extent_mm: Number(optRadius.text),
                             min_edge_length_mm: Number(optEdge.text), max_area_fraction: Number(optArea.text) / 100})
                    }
                }
                ThemeControls.ThemeButton { objectName: "cancelOptimizationButton"; text: "取消优化"; enabled: dialog.optimizer && dialog.optimizer.optimization.busy; onClicked: dialog.optimizer.cancelOptimization() }
                BusyIndicator { running: dialog.optimizer && dialog.optimizer.optimization.busy; visible: running; implicitWidth: 28; implicitHeight: 28 }
            }
            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeComboBox {
                    id: pendingMaskChoice
                    objectName: "pendingMaskChoice"
                    visible: dialog.optimizer && (dialog.optimizer.pendingMasks || []).length > 1
                    model: dialog.optimizer ? (dialog.optimizer.pendingMasks || []).map(function(mask) { return "板 " + (mask.slot + 1) }) : []
                }
                ThemeControls.ThemeButton {
                    objectName: "savePendingMaskButton"
                    text: "保存到当前项目"
                    enabled: dialog.optimizer && !dialog.optimizer.optimization.busy && (dialog.optimizer.pendingMasks || []).length > 0
                    onClicked: dialog.optimizer.saveOptimizedMaskNamed(maskName.text, Math.max(0, pendingMaskChoice.currentIndex))
                }
                ThemeControls.ThemeButton {
                    objectName: "openOptimizedProcessButton"
                    text: "打开优化工艺"
                    enabled: dialog.projectResources && dialog.optimizer && !dialog.optimizer.optimization.busy && Boolean(dialog.optimizationArtifacts().process_path)
                    onClicked: dialog.projectResources.openOptimizedProcess(dialog.optimizationArtifacts().process_path)
                }
            }
            ThemeControls.ThemeLabel {
                objectName: "optimizationAvailabilityLabel"
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: dialog.optimizer ? dialog.optimizer.optimizationAvailability : "修正板优化需要项目模式。"
                color: dialog.hostWindow ? dialog.hostWindow.themeColors.colorTextMuted : ""
            }
            ProgressBar {
                Layout.fillWidth: true
                visible: dialog.optimizer && dialog.optimizer.optimization.busy
                from: 0; to: Number(dialog.optimizer ? dialog.optimizer.optimization.max_evaluations || 48 : 48)
                value: Number(dialog.optimizer ? dialog.optimizer.optimization.evaluations || 0 : 0)
            }
            ThemeControls.ThemeLabel {
                objectName: "optimizationStatusLabel"
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: dialog.optimizer ? (dialog.optimizer.optimization.message || "") +
                    (dialog.optimizer.optimization.busy ? " · " + (dialog.optimizer.optimization.evaluations || 0) + "/" + (dialog.optimizer.optimization.max_evaluations || 48) + " 次评分" : "") +
                    (dialog.optimizer.optimization.busy && dialog.optimizer.optimization.estimated_remaining_s !== null && dialog.optimizer.optimization.estimated_remaining_s !== undefined
                     ? " · 预计剩余 " + Math.ceil(dialog.optimizer.optimization.estimated_remaining_s) + " s" : "") : ""
            }
            ThemeControls.ThemeLabel {
                visible: dialog.optimizer && Boolean(dialog.optimizationArtifacts().process_path)
                Layout.fillWidth: true; wrapMode: Text.WrapAnywhere
                text: dialog.optimizer ? dialog.optimizationArtifacts().process_path || "" : ""
            }
        }
    }
}
