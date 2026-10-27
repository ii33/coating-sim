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
    signal saveRequested()
    spacing: themeTokens.spaceSm

    component ParameterFields: GridLayout {
        id: fields
        required property var controller
        required property var themeTokens
        required property var values
        property string layerId: ""
        readonly property bool common: fields.layerId.length === 0
        columns: width >= 640 ? 5 : 2
        columnSpacing: fields.themeTokens.spaceSm
        rowSpacing: fields.themeTokens.spaceXs
        enabled: !fields.controller.busy
        function setValue(name: string, value: string) {
            if (fields.common) fields.controller.setTableDefault(name, value)
            else if (name === "bias_text" || name === "uncertainty_text")
                fields.controller.setTableDraft(fields.layerId, name === "bias_text" ? "bias" : "uncertainty_magnitude", value)
            else if (name === "uncertainty_kind") fields.controller.setTableUncertaintyKind(fields.layerId, value)
            else fields.controller.setTableUnit(fields.layerId, name === "bias_unit" ? "bias" : "uncertainty", value)
        }
        ColumnLayout {
            Layout.fillWidth: true
            Label { text: "偏差"; color: fields.themeTokens.colorTextMuted }
            ThemeControls.ThemeField {
                objectName: fields.common ? "filmThicknessCommonBias" : "filmThicknessBias_" + fields.layerId
                Layout.fillWidth: true
                Layout.minimumWidth: 70
                text: fields.values.bias_text
                placeholderText: "有符号值"
                accessibleLabel: fields.common ? "公共厚度偏差" : "本层厚度偏差"
                onTextEdited: fields.setValue("bias_text", text)
            }
        }
        ColumnLayout {
            Layout.fillWidth: true
            Label { text: "偏差单位"; color: fields.themeTokens.colorTextMuted }
            ThemeControls.ThemeComboBox {
                objectName: fields.common ? "filmThicknessCommonBiasUnit" : "filmThicknessBiasUnit_" + fields.layerId
                Layout.fillWidth: true
                model: [{id: "", label: "单位"}, {id: "absolute_nm", label: "nm"}, {id: "relative_percent", label: "%"}]
                textRole: "label"
                currentIndex: Math.max(0, model.findIndex(item => item.id === fields.values.bias_unit))
                onActivated: fields.setValue("bias_unit", model[currentIndex].id)
                Accessible.name: "偏差单位"
            }
        }
        ColumnLayout {
            Layout.fillWidth: true
            Label { text: "不确定度类型"; color: fields.themeTokens.colorTextMuted }
            ThemeControls.ThemeComboBox {
                objectName: fields.common ? "filmThicknessCommonUncertaintyKind" : "filmThicknessUncertaintyKind_" + fields.layerId
                Layout.fillWidth: true
                model: [{id: "", label: "类型未设"}, {id: "bounded", label: "有界"}, {id: "stddev", label: "标准差"}]
                textRole: "label"
                currentIndex: Math.max(0, model.findIndex(item => item.id === fields.values.uncertainty_kind))
                onActivated: fields.setValue("uncertainty_kind", model[currentIndex].id)
                Accessible.name: "不确定度类型"
            }
        }
        ColumnLayout {
            Layout.fillWidth: true
            Label { text: "不确定度"; color: fields.themeTokens.colorTextMuted }
            ThemeControls.ThemeField {
                objectName: fields.common ? "filmThicknessCommonUncertainty" : "filmThicknessUncertainty_" + fields.layerId
                Layout.fillWidth: true
                Layout.minimumWidth: 70
                text: fields.values.uncertainty_text
                placeholderText: "非负值"
                accessibleLabel: fields.common ? "公共厚度不确定度" : "本层厚度不确定度"
                onTextEdited: fields.setValue("uncertainty_text", text)
            }
        }
        ColumnLayout {
            Layout.fillWidth: true
            Label { text: "不确定度单位"; color: fields.themeTokens.colorTextMuted }
            ThemeControls.ThemeComboBox {
                objectName: fields.common ? "filmThicknessCommonUncertaintyUnit" : "filmThicknessUncertaintyUnit_" + fields.layerId
                Layout.fillWidth: true
                model: [{id: "", label: "单位"}, {id: "absolute_nm", label: "nm"}, {id: "relative_percent", label: "%"}]
                textRole: "label"
                currentIndex: Math.max(0, model.findIndex(item => item.id === fields.values.uncertainty_unit))
                onActivated: fields.setValue("uncertainty_unit", model[currentIndex].id)
                Accessible.name: "不确定度单位"
            }
        }
    }

    ScrollView {
        objectName: "filmThicknessErrorTableScroll"
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
                text: "一次填写公共偏差与不确定度；空白表示未定义，不是零。厚度单位仅支持 nm 或真实百分比。禁用层及缺项会显示在覆盖范围中，不会伪称覆盖全膜。"
                color: panel.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            Label {
                Layout.fillWidth: true
                text: "公共参数：所有层默认继承，逐层例外与取消选择不会被公共修改覆盖。"
                color: panel.themeTokens.colorText
                wrapMode: Text.WordWrap
            }
            ParameterFields {
                Layout.fillWidth: true
                controller: panel.controller
                themeTokens: panel.themeTokens
                values: panel.controller.tableDefaults
            }
            Label {
                Layout.fillWidth: true
                text: "已纳入 " + panel.controller.thicknessErrorRows.filter(row => row.enabled).length
                      + " / " + panel.controller.thicknessErrorRows.length + " 层"
                color: panel.themeTokens.colorTextMuted
            }
            ThemeControls.ThemeSection {
                objectName: "filmThicknessLayerOverridesSection"
                Layout.fillWidth: true
                title: "逐层选择与例外"
                expanded: false
                Label {
                    objectName: "filmThicknessErrorTableEmptyState"
                    Layout.fillWidth: true
                    visible: panel.controller.thicknessErrorRows.length === 0
                    text: "暂无可分析膜层。请先添加真实膜系。"
                    color: panel.themeTokens.colorTextMuted
                }
                Repeater {
                    objectName: "filmThicknessErrorTableRows"
                    model: panel.controller.thicknessErrorRows
                    delegate: ColumnLayout {
                        id: errorRow
                        objectName: "filmThicknessErrorRow_" + errorRow.modelData.layer_id
                        required property var modelData
                        Layout.fillWidth: true
                        enabled: !panel.controller.busy
                        RowLayout {
                            Layout.fillWidth: true
                            CheckBox {
                                objectName: "filmThicknessErrorEnabled_" + errorRow.modelData.layer_id
                                text: "纳入"
                                checked: errorRow.modelData.enabled
                                onToggled: panel.controller.setTableLayerEnabled(errorRow.modelData.layer_id, checked)
                                Accessible.name: "纳入参数表分析 " + errorRow.modelData.layer_label
                            }
                            Label {
                                Layout.fillWidth: true
                                text: errorRow.modelData.layer_label + " · " + errorRow.modelData.nominal
                                color: panel.themeTokens.colorText
                                elide: Text.ElideMiddle
                            }
                            CheckBox {
                                objectName: "filmThicknessOverride_" + errorRow.modelData.layer_id
                                text: "本层例外"
                                checked: errorRow.modelData.override
                                onToggled: panel.controller.setTableLayerOverride(errorRow.modelData.layer_id, checked)
                                Accessible.description: "取消本层例外会恢复公共参数，不改变是否纳入分析。"
                            }
                        }
                        ParameterFields {
                            Layout.fillWidth: true
                            visible: errorRow.modelData.override
                            controller: panel.controller
                            themeTokens: panel.themeTokens
                            values: errorRow.modelData
                            layerId: errorRow.modelData.layer_id
                        }
                    }
                }
            }
            CheckBox {
                objectName: "filmThicknessExplicitIndependentBox"
                text: "我明确指定：所有已填标准差行互不相关"
                checked: panel.controller.tableUncorrelated
                enabled: !panel.controller.busy
                onToggled: panel.controller.setTableUncorrelated(checked)
                Accessible.description: "仅在勾选时，为标准差行显式生成单位阵相关矩阵。默认相关性未知。"
            }
            Label {
                Layout.fillWidth: true
                text: "或手动输入标准差行的 rho 矩阵 JSON（行列顺序按下方 ID）；留空表示未知而非独立。对角线必须为 1，非法矩阵会拒绝运行。"
                color: panel.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            Label {
                objectName: "filmThicknessCorrelationLayerIds"
                Layout.fillWidth: true
                text: "标准差行 ID 顺序：" + panel.controller.tableStandardDeviationLayerIds.join(", ")
                color: panel.themeTokens.colorTextMuted
                wrapMode: Text.WrapAnywhere
            }
            ThemeControls.ThemeField {
                objectName: "filmThicknessCorrelationMatrixField"
                Layout.fillWidth: true
                text: panel.controller.tableCorrelationJson
                placeholderText: "例如 [[1,0],[0,1]]，仅当你明确给定时填写"
                accessibleLabel: "显式相关系数矩阵 JSON"
                enabled: !panel.controller.busy && !panel.controller.tableUncorrelated
                onTextEdited: panel.controller.setTableCorrelationDraft(text)
            }
            CheckBox {
                objectName: "filmThicknessFiniteBiasRecheckBox"
                text: "显式进行有限偏差复算（未选时只分析局部表格误差）"
                checked: panel.controller.tableIncludeFiniteBias
                enabled: !panel.controller.busy
                onToggled: panel.controller.setTableIncludeFiniteBias(checked)
            }
            FilmSensitivityWorkflowResultPanel {
                Layout.fillWidth: true
                controller: panel.controller
                themeTokens: panel.themeTokens
                workflowKind: "table"
            }
        }
    }
    RowLayout {
        Layout.fillWidth: true
        ThemeControls.ThemeButton {
            objectName: "filmThicknessErrorTableAnalyzeButton"
            text: "计算预期误差"
            primary: true
            enabled: !panel.controller.busy && panel.controller.thicknessErrorRows.length > 0
            onClicked: panel.controller.startThicknessErrorTableAnalysis()
        }
        ThemeControls.ThemeButton {
            objectName: "filmThicknessErrorTableCancelButton"
            text: "取消"
            enabled: panel.controller.busy
            onClicked: panel.controller.cancel()
        }
        ThemeControls.ThemeButton {
            objectName: "filmThicknessErrorTableSaveReportButton"
            text: "保存报告…"
            enabled: panel.controller.tableHasResult && !panel.controller.busy
            onClicked: panel.saveRequested()
        }
    }
}
