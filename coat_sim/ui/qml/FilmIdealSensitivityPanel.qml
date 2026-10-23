pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "theme_controls" as ThemeControls

ColumnLayout {
    id: panel
    required property var controller
    required property var themeTokens
    signal applyRequested()
    signal saveRequested()
    spacing: themeTokens.spaceSm

    ScrollView {
        objectName: "filmIdealSensitivityScroll"
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.minimumHeight: 130
        contentWidth: availableWidth
        clip: true
        ColumnLayout {
            width: parent.width
            spacing: panel.themeTokens.spaceSm
            Label {
                Layout.fillWidth: true
                text: "只在理想假设下优化敏感度；不读取参数表误差、幅度或权重。目标为选中厚度列敏感度平方和的加权均值，沿用原目标 weights/normalization，不是概率或良率。材料 n/k 与 use_k 沿用冻结来源，k 不会默认归零。"
                color: panel.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            ThemeControls.ThemeComboBox {
                objectName: "filmIdealSensitivityModeBox"
                Layout.fillWidth: true
                enabled: !panel.controller.busy
                model: [{id: "", label: "请选择敏感度尺度"},
                        {id: "absolute", label: "绝对：J × 1 nm（单位尺度，不是工艺误差）"},
                        {id: "relative", label: "相对：J × 当前厚度 d（1% 仅展示换算）"}]
                textRole: "label"
                currentIndex: Math.max(0, model.findIndex(item => item.id === panel.controller.idealSensitivityMode))
                onActivated: panel.controller.setIdealSensitivityMode(model[currentIndex].id)
            }
            ThemeControls.ThemeComboBox {
                objectName: "filmIdealSensitivityPolicyBox"
                Layout.fillWidth: true
                enabled: !panel.controller.busy
                model: [{id: "strict_nominal", label: "严格名义约束：沿用旧硬门"},
                        {id: "bounded_baseline_degradation", label: "受控基准退化：新 v2 默认"}]
                textRole: "label"
                currentIndex: Math.max(0, model.findIndex(item => item.id === panel.controller.idealSensitivityPolicy))
                onActivated: panel.controller.setIdealSensitivityPolicy(model[currentIndex].id)
            }
            Label {
                objectName: "filmIdealSensitivityAssumptionSummary"
                Layout.fillWidth: true
                text: "冻结来源的理想假设：" + panel.controller.idealAssumptionSummary
                color: panel.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            Label {
                Layout.fillWidth: true
                text: panel.controller.idealSensitivityPolicy === "strict_nominal"
                      ? "严格策略：初态及候选仍须满足原始名义硬约束，不增加 margin。敏感列、优化变量和原目标上下界彼此独立。"
                      : "受控退化策略：初态可不满足原目标；搜索仅可在首次完整基准光谱 y0 的逐目标额度内退化。原名义达标与 guard 额度通过分别报告，绝不滚动放宽基准。"
                color: panel.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            ColumnLayout {
                objectName: "filmIdealDegradationBudgetList"
                Layout.fillWidth: true
                spacing: panel.themeTokens.spaceXs
                visible: panel.controller.idealSensitivityPolicy === "bounded_baseline_degradation"
                Label {
                    objectName: "filmIdealDegradationUnitsNote"
                    Layout.fillWidth: true
                    text: "每个当前原目标都需填写有限非负退化额度，单位固定为百分点 pp，绝非膜厚相对百分比。空白不会按 0 处理；0 合法，双向行填写 0 时原始完整离散光谱网格的每个波长/角度/偏振条件均冻结。0.1 pp = 0.001 R/T fraction。原目标不平均，重叠目标全部生效。"
                    color: panel.themeTokens.colorTextMuted
                    wrapMode: Text.WordWrap
                }
                Repeater {
                    model: panel.controller.idealDegradationObjectives
                    delegate: RowLayout {
                        id: degradationRow
                        required property var modelData
                        Layout.fillWidth: true
                        enabled: !panel.controller.busy
                        Label {
                            Layout.fillWidth: true
                            text: degradationRow.modelData.display
                            color: panel.themeTokens.colorText
                            wrapMode: Text.WordWrap
                        }
                        ThemeControls.ThemeField {
                            objectName: "filmIdealDegradationBudget_" + degradationRow.modelData.objective_id
                            Layout.preferredWidth: 112
                            text: degradationRow.modelData.budget_text
                            placeholderText: "必填 pp"
                            accessibleLabel: degradationRow.modelData.name + " 光谱退化额度，百分点 pp"
                            onTextEdited: panel.controller.setIdealDegradationBudget(
                                degradationRow.modelData.objective_id, text)
                        }
                        Label {
                            text: "pp"
                            color: panel.themeTokens.colorTextMuted
                        }
                    }
                }
                Label {
                    Layout.fillWidth: true
                    visible: panel.controller.idealDegradationObjectives.length === 0
                    text: "暂无当前目标；请先添加并准备至少一个原目标。"
                    color: panel.themeTokens.colorTextMuted
                }
            }
            Label {
                objectName: "filmIdealSensitivityEmptyState"
                Layout.fillWidth: true
                visible: panel.controller.idealSensitivityLayers.length === 0
                text: "暂无敏感度列。请先添加真实膜层。"
                color: panel.themeTokens.colorTextMuted
            }
            Repeater {
                model: panel.controller.idealSensitivityLayers
                delegate: RowLayout {
                    id: sensitivityRow
                    required property var modelData
                    Layout.fillWidth: true
                    enabled: !panel.controller.busy
                    CheckBox {
                        objectName: "filmIdealSensitiveLayer_" + sensitivityRow.modelData.layer_id
                        checked: sensitivityRow.modelData.selected
                        onToggled: panel.controller.setIdealSensitivityLayer(sensitivityRow.modelData.layer_id, checked)
                        Accessible.name: "纳入敏感度目标 " + sensitivityRow.modelData.layer_label
                    }
                    Label {
                        Layout.fillWidth: true
                        text: sensitivityRow.modelData.layer_label + " · " + sensitivityRow.modelData.nominal
                        color: panel.themeTokens.colorText
                        elide: Text.ElideMiddle
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                enabled: !panel.controller.busy
                Label { text: "最大迭代（空白不额外指定）"; color: panel.themeTokens.colorText }
                ThemeControls.ThemeField {
                    objectName: "filmIdealSensitivityMaxIterationsField"
                    Layout.fillWidth: true
                    text: panel.controller.idealOptions.max_iterations
                    placeholderText: "留空"
                    accessibleLabel: "理想敏感度最大迭代数，可留空"
                    onTextEdited: panel.controller.setIdealOption("max_iterations", text)
                }
                Label { text: "最大评价（空白不额外指定）"; color: panel.themeTokens.colorText }
                ThemeControls.ThemeField {
                    objectName: "filmIdealSensitivityMaxEvaluationsField"
                    Layout.fillWidth: true
                    text: panel.controller.idealOptions.max_evaluations
                    placeholderText: "留空"
                    accessibleLabel: "理想敏感度最大评价数，可留空"
                    onTextEdited: panel.controller.setIdealOption("max_evaluations", text)
                }
            }
            FilmSensitivityWorkflowResultPanel {
                Layout.fillWidth: true
                controller: panel.controller
                themeTokens: panel.themeTokens
                workflowKind: "ideal"
            }
        }
    }
    RowLayout {
        Layout.fillWidth: true
        ThemeControls.ThemeButton {
            objectName: "filmIdealSensitivityOptimizeButton"
            text: "优化理想敏感度"
            primary: true
            enabled: !panel.controller.busy && panel.controller.idealSensitivityLayers.length > 0
            onClicked: panel.controller.startIdealSensitivityOptimization()
        }
        ThemeControls.ThemeButton {
            objectName: "filmIdealSensitivityCancelButton"
            text: "取消"
            enabled: panel.controller.busy
            onClicked: panel.controller.cancel()
        }
        ThemeControls.ThemeButton {
            objectName: "filmIdealSensitivitySaveReportButton"
            text: "保存报告…"
            enabled: panel.controller.idealHasResult && !panel.controller.busy
            onClicked: panel.saveRequested()
        }
        ThemeControls.ThemeButton {
            objectName: "filmIdealSensitivityApplyButton"
            text: "应用最优候选…"
            enabled: panel.controller.idealCanApply
            onClicked: panel.applyRequested()
        }
    }
}
