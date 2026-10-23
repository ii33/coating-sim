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
                text: "为每层填写偏差与不确定度参数；空白表示未定义，不是零。厚度单位仅支持 nm 或真实百分比。禁用层及缺项会显示在覆盖范围中，不会伪称覆盖全膜。"
                color: panel.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: panel.themeTokens.spaceXs
                Label { Layout.preferredWidth: 185; text: "膜层 / 名义厚度"; color: panel.themeTokens.colorTextMuted }
                Label { Layout.fillWidth: true; text: "偏差（可留空）"; color: panel.themeTokens.colorTextMuted }
                Label { Layout.fillWidth: true; text: "不确定度（可留空）"; color: panel.themeTokens.colorTextMuted }
            }
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
                delegate: RowLayout {
                    id: errorRow
                    objectName: "filmThicknessErrorRow_" + errorRow.modelData.layer_id
                    required property var modelData
                    Layout.fillWidth: true
                    enabled: !panel.controller.busy
                    CheckBox {
                        objectName: "filmThicknessErrorEnabled_" + errorRow.modelData.layer_id
                        checked: errorRow.modelData.enabled
                        onToggled: panel.controller.setTableLayerEnabled(errorRow.modelData.layer_id, checked)
                        Accessible.name: "纳入参数表分析 " + errorRow.modelData.layer_label
                    }
                    Label {
                        Layout.preferredWidth: 165
                        text: errorRow.modelData.layer_label + "\n" + errorRow.modelData.nominal
                        color: panel.themeTokens.colorText
                        elide: Text.ElideMiddle
                        wrapMode: Text.WordWrap
                        Accessible.name: text
                    }
                    ThemeControls.ThemeField {
                        objectName: "filmThicknessBias_" + errorRow.modelData.layer_id
                        Layout.fillWidth: true
                        Layout.minimumWidth: 78
                        text: errorRow.modelData.bias_text
                        placeholderText: "有符号值"
                        accessibleLabel: "厚度偏差值 " + errorRow.modelData.layer_label
                        onTextEdited: panel.controller.setTableDraft(errorRow.modelData.layer_id, "bias", text)
                    }
                    ThemeControls.ThemeComboBox {
                        objectName: "filmThicknessBiasUnit_" + errorRow.modelData.layer_id
                        Layout.preferredWidth: 86
                        model: [{id: "", label: "单位"}, {id: "absolute_nm", label: "nm"}, {id: "relative_percent", label: "%"}]
                        textRole: "label"
                        currentIndex: Math.max(0, model.findIndex(item => item.id === errorRow.modelData.bias_unit))
                        onActivated: panel.controller.setTableUnit(errorRow.modelData.layer_id, "bias", model[currentIndex].id)
                        Accessible.name: "偏差单位 " + errorRow.modelData.layer_label
                    }
                    ThemeControls.ThemeComboBox {
                        objectName: "filmThicknessUncertaintyKind_" + errorRow.modelData.layer_id
                        Layout.preferredWidth: 118
                        model: [{id: "", label: "类型未设"}, {id: "bounded", label: "有界"}, {id: "stddev", label: "标准差"}]
                        textRole: "label"
                        currentIndex: Math.max(0, model.findIndex(item => item.id === errorRow.modelData.uncertainty_kind))
                        onActivated: panel.controller.setTableUncertaintyKind(errorRow.modelData.layer_id, model[currentIndex].id)
                        Accessible.name: "不确定度类型 " + errorRow.modelData.layer_label
                    }
                    ThemeControls.ThemeField {
                        objectName: "filmThicknessUncertainty_" + errorRow.modelData.layer_id
                        Layout.fillWidth: true
                        Layout.minimumWidth: 78
                        text: errorRow.modelData.uncertainty_text
                        placeholderText: "非负值"
                        accessibleLabel: "厚度不确定度 " + errorRow.modelData.layer_label
                        onTextEdited: panel.controller.setTableDraft(errorRow.modelData.layer_id, "uncertainty_magnitude", text)
                    }
                    ThemeControls.ThemeComboBox {
                        objectName: "filmThicknessUncertaintyUnit_" + errorRow.modelData.layer_id
                        Layout.preferredWidth: 86
                        model: [{id: "", label: "单位"}, {id: "absolute_nm", label: "nm"}, {id: "relative_percent", label: "%"}]
                        textRole: "label"
                        currentIndex: Math.max(0, model.findIndex(item => item.id === errorRow.modelData.uncertainty_unit))
                        onActivated: panel.controller.setTableUnit(errorRow.modelData.layer_id, "uncertainty", model[currentIndex].id)
                        Accessible.name: "不确定度单位 " + errorRow.modelData.layer_label
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
