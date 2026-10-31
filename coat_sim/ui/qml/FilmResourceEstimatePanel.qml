pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ColumnLayout {
    id: panel
    objectName: "filmResourceEstimatePanel"
    required property var estimator
    required property var themeTokens
    spacing: themeTokens.spaceSm

    Label {
        Layout.fillWidth: true
        text: "资源评估 · 使用当前目标与候选材料的冻结快照；不会修改设计或优化约束。"
        wrapMode: Text.Wrap
        color: panel.themeTokens.colorTextMuted
    }
    Label {
        Layout.fillWidth: true
        text: "候选膜料与光学数据集（本页独立草稿；仅在当前设计会话保留）。基片选择也是本页独立草稿；目标、选面、未知 k 策略及工艺参数来自当前优化设置。"
        wrapMode: Text.Wrap
        color: panel.themeTokens.colorTextMuted
    }
    Label { text: "基片材料与光学数据集"; color: panel.themeTokens.colorText }
    ThemeControls.ThemeComboBox {
        objectName: "resourceSubstrateMaterial"
        Accessible.name: "基片材料"
        Layout.fillWidth: true
        enabled: !panel.estimator.busy
        model: panel.estimator.substrateSelection.material_options
        textRole: "name"
        currentIndex: model.findIndex(item => item.id === panel.estimator.substrateSelection.material_id)
        displayText: currentIndex < 0 ? "选择基片材料" : currentText
        onActivated: { const item = model[currentIndex]; if (item) panel.estimator.setSubstrateMaterial(item.id) }
    }
    ThemeControls.ThemeComboBox {
        objectName: "resourceSubstrateOpticalSet"
        Accessible.name: "基片光学数据集"
        Layout.fillWidth: true
        enabled: !panel.estimator.busy
        model: panel.estimator.substrateSelection.optical_set_options
        textRole: "name"
        currentIndex: model.findIndex(item => item.id === panel.estimator.substrateSelection.optical_set_id)
        displayText: currentIndex < 0 ? "选择基片数据集" : currentText
        onActivated: { const item = model[currentIndex]; if (item) panel.estimator.setSubstrateOpticalSet(item.id) }
    }
    Label { Layout.fillWidth: true; text: panel.estimator.substrateSelection.error; visible: text.length > 0; color: panel.themeTokens.colorWarning; wrapMode: Text.Wrap }
    Label { text: "候选膜料"; color: panel.themeTokens.colorText }
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
            Label { Layout.fillWidth: true; text: materialRow.modelData.error; visible: text.length > 0; color: panel.themeTokens.colorWarning; wrapMode: Text.Wrap }
        }
    }
    RowLayout {
        ThemeControls.ThemeButton { objectName: "resourceAddMaterial"; text: "添加候选材料"; enabled: !panel.estimator.busy; onClicked: { panel.estimator.refreshMaterials(); panel.estimator.addMaterialRow() } }
        ThemeControls.ThemeButton { text: "刷新材料目录"; enabled: !panel.estimator.busy; onClicked: panel.estimator.refreshMaterials() }
    }
    Label { Layout.fillWidth: true; text: panel.estimator.materialError; visible: text.length > 0; color: panel.themeTokens.colorWarning; wrapMode: Text.Wrap }
    Label {
        objectName: "filmResourceEstimateSource"
        Layout.fillWidth: true
        text: panel.estimator.sourceSummary
        wrapMode: Text.Wrap
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
            function commitDraft() { return panel.estimator.setLayerLimit(text) }
            onEditingFinished: commitDraft()
        }
    }
    Label {
        Layout.fillWidth: true
        text: "仅控制评估枚举范围，不是设计或优化限制；超出范围为 unknown。"
        wrapMode: Text.Wrap
        color: panel.themeTokens.colorTextMuted
    }
    CheckBox {
        objectName: "resourceVerificationEnabled"
        text: "进一步验证（合成探测）"
        checked: panel.estimator.verificationEnabled
        enabled: !panel.estimator.busy
        onToggled: panel.estimator.setVerificationEnabled(checked)
    }
    Label {
        Layout.fillWidth: true
        text: "默认快速评估不运行合成探测或独立候选审计；进一步验证可能接近自动合成的计算量。"
        wrapMode: Text.Wrap
        color: panel.themeTokens.colorTextMuted
    }
    RowLayout {
        Layout.fillWidth: true
        visible: panel.estimator.verificationEnabled
        Label { text: "每种子逻辑评价上限（含审计）"; color: panel.themeTokens.colorText }
        ThemeControls.ThemeField {
            objectName: "resourceProbeMaxEvaluations"
            Accessible.name: "每种子逻辑评价上限（含审计）"
            Layout.fillWidth: true
            enabled: !panel.estimator.busy && panel.estimator.verificationEnabled
            text: String(panel.estimator.probeMaxEvaluations)
            function commitDraft() { return panel.estimator.setProbeMaxEvaluations(text) }
            onEditingFinished: commitDraft()
        }
    }
    RowLayout {
        Layout.fillWidth: true
        visible: panel.estimator.verificationEnabled
        Label { text: "随机种子列表"; color: panel.themeTokens.colorText }
        ThemeControls.ThemeField {
            objectName: "resourceProbeSeeds"
            Accessible.name: "随机种子列表，逗号分隔"
            Layout.fillWidth: true
            enabled: !panel.estimator.busy && panel.estimator.verificationEnabled
            text: panel.estimator.probeSeeds
            function commitDraft() { return panel.estimator.setProbeSeeds(text) }
            onEditingFinished: commitDraft()
        }
    }
    Label {
        Layout.fillWidth: true
        visible: panel.estimator.verificationEnabled
        text: "每种子各自使用上述逻辑评价上限（含审计）；0 表示只做预检和当前结构审计，不进行搜索；开启搜索至少需 2 次。种子范围 0–4294967295，以逗号分隔；不限制层数或总厚度。"
        wrapMode: Text.Wrap
        color: panel.themeTokens.colorTextMuted
    }
    Label {
        Layout.fillWidth: true
        visible: panel.estimator.verificationEnabled && !panel.estimator.probeSettingsValid
        text: panel.estimator.probeSettingsError
        wrapMode: Text.Wrap
        color: panel.themeTokens.colorWarning
    }
    Label {
        objectName: "filmResourceEstimateStatus"
        Layout.fillWidth: true
        text: panel.estimator.status
        color: panel.estimator.stale ? panel.themeTokens.colorError : panel.themeTokens.colorText
        wrapMode: Text.Wrap
    }
    Label {
        objectName: "filmResourceEstimateResult"
        Layout.fillWidth: true
        text: panel.estimator.resultSummary
        color: panel.themeTokens.colorText
        wrapMode: Text.Wrap
    }
    CheckBox { id: detailsToggle; text: "显示技术详情"; visible: panel.estimator.technicalDetails.length > 0 }
    Label {
        objectName: "filmResourceEstimateTechnicalDetails"
        Layout.fillWidth: true
        visible: detailsToggle.checked
        text: panel.estimator.technicalDetails
        color: panel.themeTokens.colorTextMuted
        wrapMode: Text.Wrap
    }
}
