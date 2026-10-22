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

    Label {
        Layout.fillWidth: true
        text: "独立分析当前已有膜系。误差幅度只用于本工作流，不改变普通名义目标或自动加余量。首版：单面前表面相干 R/T、固定层序膜厚误差；本地 CPU 队列。"
        color: panel.themeTokens.colorTextMuted
        wrapMode: Text.WordWrap
    }
    RowLayout {
        Layout.fillWidth: true
        enabled: !panel.controller.busy
        Label { text: "误差单位"; color: panel.themeTokens.colorText }
        ThemeControls.ThemeComboBox {
            objectName: "filmSensitivityUnitBox"
            Layout.fillWidth: true
            model: [{id: "absolute_nm", label: "绝对误差幅度 nm"}, {id: "relative_fraction", label: "相对厚度误差幅度 %"}]
            textRole: "label"
            currentIndex: panel.controller.errorUnit === "absolute_nm" ? 0 : 1
            onActivated: panel.controller.setChoice("unit", model[currentIndex].id)
        }
        Label { text: "误差模型"; color: panel.themeTokens.colorText }
        ThemeControls.ThemeComboBox {
            objectName: "filmSensitivityModelBox"
            Layout.fillWidth: true
            model: [{id: "independent", label: "逐层独立"}, {id: "common_mode", label: "全部共模"}]
            textRole: "label"
            currentIndex: panel.controller.errorModel === "independent" ? 0 : 1
            onActivated: panel.controller.setChoice("model", model[currentIndex].id)
        }
    }
    ThemeControls.ThemeComboBox {
        objectName: "filmSensitivityObjectiveBox"
        Layout.fillWidth: true
        enabled: !panel.controller.busy
        model: [{id: "linear_sensitivity_norm", label: "优化目标：一阶敏感度范数"},
                {id: "finite_scenario_worst", label: "优化目标：已列有限场景最坏违规"},
                {id: "finite_scenario_expected", label: "优化目标：已列有限场景加权 RMS 违规"}]
        textRole: "label"
        currentIndex: Math.max(0, model.findIndex(item => item.id === panel.controller.robustnessObjective))
        onActivated: panel.controller.setChoice("objective", model[currentIndex].id)
    }
    ScrollView {
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.minimumHeight: 110
        contentWidth: availableWidth
        clip: true
        ColumnLayout {
            width: parent.width
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
                        objectName: "filmSensitivityMagnitude_" + errorRow.modelData.layer_id
                        Layout.preferredWidth: 120
                        text: errorRow.modelData.magnitude
                        accessibleLabel: "膜层误差幅度 " + errorRow.modelData.label
                        onEditingFinished: panel.controller.setMagnitude(errorRow.modelData.layer_id, text)
                    }
                }
            }
        }
    }
    RowLayout {
        Layout.fillWidth: true
        ThemeControls.ThemeButton {
            objectName: "filmSensitivityGenerateScenariosButton"
            text: "生成明确 ± 幅度场景"
            enabled: !panel.controller.busy
            onClicked: panel.controller.generateScenarios()
        }
        Label {
            Layout.fillWidth: true
            text: "已列 " + panel.controller.scenarioCount + " 场景。逐层组仅±单层，非全部联合角点或制造蒙特卡洛。"
            color: panel.themeTokens.colorTextMuted
            wrapMode: Text.WordWrap
        }
    }
    RowLayout {
        Layout.fillWidth: true
        enabled: !panel.controller.busy
        Label { text: "最大候选评价"; color: panel.themeTokens.colorText }
        ThemeControls.ThemeField {
            Layout.fillWidth: true
            text: panel.controller.options.max_evaluations
            placeholderText: "留空不设限"
            onEditingFinished: panel.controller.setOption("max_evaluations", text)
        }
        Label { text: "最大迭代"; color: panel.themeTokens.colorText }
        ThemeControls.ThemeField {
            Layout.fillWidth: true
            text: panel.controller.options.max_iterations
            placeholderText: "留空不设限"
            onEditingFinished: panel.controller.setOption("max_iterations", text)
        }
    }
    Label {
        Layout.fillWidth: true
        text: "敏感性优化需要在原约束页为所选厚度变量设置明确有限上下界。仅优化所选误差目标，名义指标可能恶化；不会自动应用。"
        color: panel.themeTokens.colorTextMuted
        wrapMode: Text.WordWrap
    }
    RowLayout {
        Layout.fillWidth: true
        ThemeControls.ThemeButton {
            objectName: "filmSensitivityAnalyzeButton"
            text: "分析当前处方"
            enabled: !panel.controller.busy
            onClicked: panel.controller.start("analyze")
        }
        ThemeControls.ThemeButton {
            objectName: "filmSensitivityOptimizeButton"
            text: "优化所选误差目标"
            primary: true
            enabled: !panel.controller.busy
            onClicked: panel.controller.start("optimize")
        }
        ThemeControls.ThemeButton {
            text: "取消"
            enabled: panel.controller.busy
            onClicked: panel.controller.cancel()
        }
        ThemeControls.ThemeButton {
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
        Layout.fillWidth: true
        visible: panel.controller.error.length > 0
        text: panel.controller.error
        color: panel.themeTokens.colorError
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
