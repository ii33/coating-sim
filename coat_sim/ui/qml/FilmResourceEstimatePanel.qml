pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ColumnLayout {
    id: panel
    objectName: "filmResourceEstimatePanel"
    required property var estimator
    required property var sourceController
    required property var themeTokens
    spacing: themeTokens.spaceSm
    signal startRequested()

    Label {
        Layout.fillWidth: true
        text: "资源评估 · 使用当前目标与候选材料的冻结快照；不会修改设计或优化约束。"
        wrapMode: Text.WordWrap
        color: panel.themeTokens.colorTextMuted
    }
    Label {
        Layout.fillWidth: true
        text: "候选膜料与光学数据集（本页独立草稿；仅在当前设计会话保留）。目标、选面、基片、未知 k 策略及工艺参数仍来自目标与材料设置。"
        wrapMode: Text.WordWrap
        color: panel.themeTokens.colorTextMuted
    }
    Repeater {
        model: panel.estimator.materialRows
        delegate: ColumnLayout {
            id: materialRow
            required property var modelData
            Layout.fillWidth: true
            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeComboBox {
                    objectName: "resourceMaterial_" + materialRow.modelData.id
                    Accessible.name: "候选膜料"
                    Layout.fillWidth: true
                    enabled: !panel.estimator.busy
                    model: materialRow.modelData.material_options
                    textRole: "name"
                    currentIndex: model.findIndex(item => item.id === materialRow.modelData.material_id)
                    displayText: currentIndex < 0 ? "选择材料" : currentText
                    onActivated: { const item = model[currentIndex]; if (item) panel.estimator.setRowMaterial(materialRow.modelData.id, item.id) }
                }
                ThemeControls.ThemeButton {
                    text: "×"; Accessible.name: "删除此候选材料行"
                    enabled: !panel.estimator.busy
                    onClicked: panel.estimator.removeMaterialRow(materialRow.modelData.id)
                }
            }
            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeComboBox {
                    objectName: "resourceOpticalSet_" + materialRow.modelData.id
                    Accessible.name: "光学数据集"
                    Layout.fillWidth: true
                    enabled: !panel.estimator.busy
                    model: materialRow.modelData.optical_set_options
                    textRole: "name"
                    currentIndex: model.findIndex(item => item.id === materialRow.modelData.optical_set_id)
                    displayText: currentIndex < 0 ? "选择数据集" : currentText
                    onActivated: { const item = model[currentIndex]; if (item) panel.estimator.setRowOpticalSet(materialRow.modelData.id, item.id) }
                }
                ThemeControls.ThemeButton { text: "↑"; Accessible.name: "上移此候选材料行"; enabled: !panel.estimator.busy; onClicked: panel.estimator.moveMaterialRow(materialRow.modelData.id, -1) }
                ThemeControls.ThemeButton { text: "↓"; Accessible.name: "下移此候选材料行"; enabled: !panel.estimator.busy; onClicked: panel.estimator.moveMaterialRow(materialRow.modelData.id, 1) }
            }
            Label { Layout.fillWidth: true; text: materialRow.modelData.error; visible: text.length > 0; color: panel.themeTokens.colorWarning; wrapMode: Text.WordWrap }
        }
    }
    RowLayout {
        ThemeControls.ThemeButton { objectName: "resourceAddMaterial"; text: "添加候选材料"; enabled: !panel.estimator.busy; onClicked: { panel.estimator.refreshMaterials(); panel.estimator.addMaterialRow() } }
        ThemeControls.ThemeButton { text: "刷新材料目录"; enabled: !panel.estimator.busy; onClicked: panel.estimator.refreshMaterials() }
    }
    Label { Layout.fillWidth: true; text: panel.estimator.materialError; visible: text.length > 0; color: panel.themeTokens.colorWarning; wrapMode: Text.WordWrap }
    Label {
        objectName: "filmResourceEstimateSource"
        Layout.fillWidth: true
        text: panel.estimator.sourceSummary
        wrapMode: Text.WordWrap
        color: panel.themeTokens.colorText
    }
    RowLayout {
        Layout.fillWidth: true
        Label { text: "点频必要界枚举至层数"; color: panel.themeTokens.colorText }
        ThemeControls.ThemeField {
            objectName: "filmResourceEstimateLayerLimit"
            Layout.preferredWidth: 90
            enabled: !panel.estimator.busy
            text: String(panel.estimator.layerLimit)
            onEditingFinished: panel.estimator.setLayerLimit(text)
        }
    }
    Label {
        Layout.fillWidth: true
        text: "仅控制评估枚举范围，不是设计或优化限制；超出范围为 unknown。"
        wrapMode: Text.WordWrap
        color: panel.themeTokens.colorTextMuted
    }
    RowLayout {
        Layout.fillWidth: true
        ThemeControls.ThemeButton {
            objectName: "filmResourceEstimateStart"
            text: "开始评估"
            primary: true
            enabled: !panel.estimator.busy && panel.estimator.limitValid
            onClicked: panel.startRequested()
        }
        ThemeControls.ThemeButton {
            objectName: "filmResourceEstimateCancel"
            text: "取消"
            enabled: panel.estimator.busy
            onClicked: panel.estimator.cancel()
        }
        ThemeControls.ThemeButton {
            objectName: "filmResourceEstimateTargets"
            text: "目标与材料设置…"
            enabled: !panel.estimator.busy
            onClicked: panel.sourceController.openResourceEstimateSettings()
        }
    }
    Label {
        objectName: "filmResourceEstimateStatus"
        Layout.fillWidth: true
        text: panel.estimator.status
        color: panel.estimator.stale ? panel.themeTokens.colorError : panel.themeTokens.colorText
        wrapMode: Text.WordWrap
    }
    Label {
        objectName: "filmResourceEstimateResult"
        Layout.fillWidth: true
        text: panel.estimator.resultSummary
        color: panel.themeTokens.colorText
        wrapMode: Text.WordWrap
    }
}
