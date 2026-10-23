pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ColumnLayout {
    id: panel
    required property var controller
    required property var themeTokens
    signal applyRequested()
    signal saveRequested()
    spacing: themeTokens.spaceSm

    ScrollView {
        objectName: "filmSensitivityConfigurationScroll"
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.minimumHeight: 110
        contentWidth: availableWidth
        clip: true
        ColumnLayout {
            width: parent.width
            spacing: panel.themeTokens.spaceSm
            Label {
                Layout.fillWidth: true
                text: "已有单面前表面相干 R/T 膜系，固定层序膜厚工艺误差；本地 CPU 队列。原名义目标、完整采样网格不变，不自动加余量。"
                color: panel.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            ThemeControls.ThemeComboBox {
                objectName: "filmSensitivityPolicyBox"
                Layout.fillWidth: true
                enabled: !panel.controller.busy
                model: [{id: "nominal_preserving", label: "保名义优化：仅在原名义可行域内改善有限场景"},
                        {id: "robustness_only", label: "纯降敏（显式）：名义指标可能恶化，不保名义"}]
                textRole: "label"
                currentIndex: model.findIndex(item => item.id === panel.controller.optimizationPolicy)
                onActivated: panel.controller.setChoice("policy", model[currentIndex].id)
            }
            Label {
                objectName: "filmSensitivityPolicyWarning"
                Layout.fillWidth: true
                text: panel.controller.optimizationPolicy === "robustness_only"
                      ? "注意：已显式选择纯降敏，名义指标可能恶化。不会自动应用候选。"
                      : "保名义优化要求初态先满足原名义约束；不满足时请先恢复名义可行性或更换初态，不会暗改目标。"
                color: panel.controller.optimizationPolicy === "robustness_only"
                       ? panel.themeTokens.colorError : panel.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            ThemeControls.ThemeComboBox {
                objectName: "filmSensitivityObjectiveBox"
                Layout.fillWidth: true
                enabled: !panel.controller.busy
                model: [{id: "finite_scenario_worst", label: "目标：明确有限场景最坏违规"},
                        {id: "finite_scenario_expected", label: "目标：明确有限场景加权 RMS 违规"},
                        {id: "linear_sensitivity_norm", label: "一阶敏感度范数（独立分析 / 显式纯降敏，非保名义优化）"}]
                textRole: "label"
                currentIndex: model.findIndex(item => item.id === panel.controller.robustnessObjective)
                onActivated: panel.controller.setChoice("objective", model[currentIndex].id)
            }
            RowLayout {
                Layout.fillWidth: true
                enabled: !panel.controller.busy
                ThemeControls.ThemeComboBox {
                    objectName: "filmSensitivitySemanticsBox"
                    Layout.fillWidth: true
                    model: [{id: "", label: "请选择：误差尺度语义"},
                            {id: "bounded_amplitude", label: "有界误差幅度"},
                            {id: "standard_deviation", label: "标准差 1σ（非界限）"}]
                    textRole: "label"
                    currentIndex: model.findIndex(item => item.id === panel.controller.amplitudeSemantics)
                    onActivated: panel.controller.setChoice("semantics", model[currentIndex].id)
                }
                ThemeControls.ThemeComboBox {
                    objectName: "filmSensitivityModelBox"
                    Layout.fillWidth: true
                    model: [{id: "", label: "请选择：工艺误差模型"},
                            {id: "independent", label: "逐层独立"},
                            {id: "common_mode", label: "全部共模"},
                            {id: "material_common_mode", label: "同材料共模"},
                            {id: "linear_drift", label: "沉积顺序线性漂移"}]
                    textRole: "label"
                    currentIndex: model.findIndex(item => item.id === panel.controller.errorModel)
                    onActivated: panel.controller.setChoice("model", model[currentIndex].id)
                }
            }
            // Preserve the legacy unit entry without presenting an uncalibrated
            // single-amplitude default in the new additive process editor.
            ThemeControls.ThemeComboBox {
                objectName: "filmSensitivityUnitBox"
                visible: false
                model: [{id: "absolute_nm", label: "兼容绝对幅度 nm"},
                        {id: "relative_fraction", label: "兼容相对幅度 %"}]
                textRole: "label"
                currentIndex: panel.controller.errorUnit === "absolute_nm" ? 0 : 1
                onActivated: panel.controller.setChoice("unit", model[currentIndex].id)
            }
            Label {
                Layout.fillWidth: true
                text: "每层尺度 = 绝对 nm + 相对 % × 当前厚度，两项共用一个系数，直接相加而非 RSS。至少填一项，未填另一项按零；没有工艺校准默认值。"
                color: panel.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            Label {
                objectName: "filmSensitivityEmptyState"
                Layout.fillWidth: true
                visible: panel.controller.layerErrors.length === 0
                text: "暂无可分析膜层。请先添加真实膜系或完成自动合成。"
                color: panel.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            Repeater {
                model: panel.controller.layerErrors
                delegate: RowLayout {
                    id: errorRow
                    required property var modelData
                    Layout.fillWidth: true
                    enabled: !panel.controller.busy
                    CheckBox {
                        checked: errorRow.modelData.enabled
                        onToggled: panel.controller.setLayerEnabled(errorRow.modelData.layer_id, checked)
                        Accessible.name: "选择误差膜层 " + errorRow.modelData.label
                    }
                    Label {
                        Layout.fillWidth: true
                        text: errorRow.modelData.label + " · " + errorRow.modelData.nominal
                        color: panel.themeTokens.colorText
                        elide: Text.ElideMiddle
                    }
                    ThemeControls.ThemeField {
                        objectName: "filmSensitivityAbsolute_" + errorRow.modelData.layer_id
                        Layout.preferredWidth: 130
                        text: errorRow.modelData.absolute_nm
                        placeholderText: "绝对 nm（需校准）"
                        accessibleLabel: "绝对 nm 误差 " + errorRow.modelData.label
                        onTextEdited: panel.controller.setErrorDraft(errorRow.modelData.layer_id, "absolute_nm", text)
                    }
                    ThemeControls.ThemeField {
                        objectName: "filmSensitivityRelative_" + errorRow.modelData.layer_id
                        Layout.preferredWidth: 130
                        text: errorRow.modelData.relative_percent
                        placeholderText: "相对 %（需校准）"
                        accessibleLabel: "相对 % 误差 " + errorRow.modelData.label
                        onTextEdited: panel.controller.setErrorDraft(errorRow.modelData.layer_id, "relative_percent", text)
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                enabled: !panel.controller.busy
                ThemeControls.ThemeComboBox {
                    objectName: "filmSensitivityDistributionBox"
                    Layout.fillWidth: true
                    model: [{id: "", label: "请选择：场景分布假设"},
                            {id: "bounded_uniform", label: "有界均匀（±幅度内）"},
                            {id: "gaussian", label: "高斯（标准差1σ，不裁剪）"}]
                    textRole: "label"
                    currentIndex: model.findIndex(item => item.id === panel.controller.distribution)
                    onActivated: panel.controller.setChoice("distribution", model[currentIndex].id)
                }
                ThemeControls.ThemeField {
                    objectName: "filmSensitivitySampleCountField"
                    Layout.preferredWidth: 120
                    text: panel.controller.sampling.sample_count
                    placeholderText: "样本数（必填）"
                    accessibleLabel: "显式场景样本数"
                    onTextEdited: panel.controller.setSamplingOption("sample_count", text)
                }
                ThemeControls.ThemeField {
                    objectName: "filmSensitivitySeedField"
                    Layout.preferredWidth: 120
                    text: panel.controller.sampling.seed
                    placeholderText: "seed（必填）"
                    accessibleLabel: "场景随机 seed"
                    onTextEdited: panel.controller.setSamplingOption("seed", text)
                }
            }
            Label {
                Layout.fillWidth: true
                text: "分布仅为你指定的工艺假设。高斯系数可能超过 ±1，标准差不是最大误差；有限场景不是制造良率。"
                color: panel.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            ThemeControls.ThemeButton {
                objectName: "filmSensitivityGenerateScenariosButton"
                text: "生成并冻结明确工艺场景"
                enabled: !panel.controller.busy && panel.controller.layerErrors.length > 0
                onClicked: panel.controller.generateProcessScenarios()
            }
            Label {
                objectName: "filmSensitivityScenarioDescription"
                Layout.fillWidth: true
                text: panel.controller.scenarioDescription
                color: panel.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            RowLayout {
                Layout.fillWidth: true
                enabled: !panel.controller.busy
                Label { text: "最大候选评价"; color: panel.themeTokens.colorText }
                ThemeControls.ThemeField {
                    objectName: "filmSensitivityMaxEvaluationsField"
                    Layout.fillWidth: true
                    text: panel.controller.options.max_evaluations
                    placeholderText: "留空不设限"
                    onTextEdited: panel.controller.setOption("max_evaluations", text)
                }
                Label { text: "最大迭代"; color: panel.themeTokens.colorText }
                ThemeControls.ThemeField {
                    objectName: "filmSensitivityMaxIterationsField"
                    Layout.fillWidth: true
                    text: panel.controller.options.max_iterations
                    placeholderText: "留空不设限"
                    onTextEdited: panel.controller.setOption("max_iterations", text)
                }
            }
            Label {
                Layout.fillWidth: true
                text: "优化需先在原约束页给所选厚度变量设置明确有限上下界。搜索为局部方法，不保证全局最优。配置改变后必须重新生成场景。"
                color: panel.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            Label {
                objectName: "filmSensitivityResultSummary"
                Layout.fillWidth: true
                visible: panel.controller.hasResult
                text: panel.controller.resultSummary
                color: panel.themeTokens.colorText
                wrapMode: Text.WordWrap
            }
        }
    }
    RowLayout {
        Layout.fillWidth: true
        ThemeControls.ThemeButton {
            objectName: "filmSensitivityAnalyzeButton"
            text: "分析当前处方"
            enabled: !panel.controller.busy && panel.controller.layerErrors.length > 0
            onClicked: panel.controller.start("analyze")
        }
        ThemeControls.ThemeButton {
            objectName: "filmSensitivityOptimizeButton"
            text: panel.controller.optimizationPolicy === "nominal_preserving" ? "保名义优化" : "纯降敏优化"
            primary: true
            enabled: !panel.controller.busy && panel.controller.layerErrors.length > 0
            onClicked: panel.controller.start("optimize")
        }
        ThemeControls.ThemeButton {
            objectName: "filmSensitivityCancelButton"
            text: "取消"
            enabled: panel.controller.busy
            onClicked: panel.controller.cancel()
        }
        ThemeControls.ThemeButton {
            objectName: "filmSensitivitySaveReportButton"
            text: "保存完整报告…"
            enabled: panel.controller.hasResult && !panel.controller.busy
            onClicked: panel.saveRequested()
        }
        ThemeControls.ThemeButton {
            objectName: "filmSensitivityApplyButton"
            text: "应用最优候选…"
            enabled: panel.controller.canApply
            onClicked: panel.applyRequested()
        }
    }
    Label {
        objectName: "filmSensitivityStatus"
        Layout.fillWidth: true
        text: panel.controller.status + (panel.controller.jobId.length ? "\n作业 " + panel.controller.jobId : "")
        color: panel.themeTokens.colorTextMuted
        wrapMode: Text.WordWrap
    }
    Label {
        objectName: "filmSensitivityError"
        Layout.fillWidth: true
        visible: panel.controller.error.length > 0
        text: panel.controller.error
        color: panel.themeTokens.colorError
        wrapMode: Text.WordWrap
    }
}
