pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "theme_controls" as ThemeControls

ThemeControls.ThemeDialogBase {
    id: dialog
    objectName: "tfcalcImportDialog"
    required property var controller
    property real hostWidth: 1100
    property real hostHeight: 800
    property var mappings: Object.create(null)
    readonly property var importState: controller.tfcalcImportState
    readonly property var aliases: sourceTable.currentIndex === 0
                                      ? (dialog.importState.primary_aliases || []) : (dialog.importState.secondary_aliases || [])
    property bool resetting: false
    signal inputsReset()
    title: "TFCalc 明文导入 · 显式层表与材料映射"
    modal: true
    width: Math.min(1000, hostWidth - 32)
    height: Math.min(760, hostHeight - 32)
    closePolicy: Popup.CloseOnEscape

    function invalidate() { if (!resetting) controller.invalidateTfcalcPreview() }
    function resetInputs() {
        resetting = true
        mappings = Object.create(null)
        sourceTable.currentIndex = 0
        targetSide.currentIndex = 0
        thicknessField.currentIndex = 1
        thicknessUnit.currentIndex = 0
        sourceOrder.currentIndex = 0
        referenceWave.text = String(dialog.importState.reference_wavelength_nm || "")
        preserveEnvironment.checked = false
        advanced.checked = false
        rawReport.checked = false
        inputsReset()
        resetting = false
        invalidate()
    }
    function setMapping(alias, material, dataset) {
        const copy = Object.assign(Object.create(null), mappings)
        copy[alias] = {material_id: material, optical_set_id: dataset}
        mappings = copy
        invalidate()
    }
    function parameters() {
        return {source_table: sourceTable.currentIndex === 0 ? "primary" : "secondary",
                thickness_field: thicknessField.currentIndex === 0 ? 3 : 4,
                thickness_unit: thicknessUnit.currentIndex === 0 ? "nm" : "um",
                source_order: sourceOrder.currentIndex === 0 ? "substrate_outward" : "incident_first",
                target_side: targetSide.currentIndex === 0 ? "front" : "back",
                reference_wavelength_nm: Number(referenceWave.text),
                preserve_environment_confirmed: preserveEnvironment.checked,
                // Qt's QVariant bridge expects a plain object. JSON parsing
                // creates own data keys even for a literal __proto__ alias.
                mapping: JSON.parse(JSON.stringify(mappings))}
    }
    onOpened: resetInputs()
    onClosed: controller.clearTfcalcImport()

    FileDialog {
        id: fileDialog
        title: "选择 TFCalc VERSION 1 明文文件"
        currentFolder: dialog.controller.defaultDirectory
        fileMode: FileDialog.OpenFile
        nameFilters: ["TFCalc 明文 (*.tfd)"]
        onAccepted: { dialog.controller.inspectTfcalcFile(selectedFile); dialog.resetInputs() }
    }

    contentItem: ScrollView {
        clip: true
        contentWidth: availableWidth
        ColumnLayout {
            width: parent.width
            spacing: dialog.themeTokens.spaceMd
            RowLayout {
                Layout.fillWidth: true
                CheckBox {
                    objectName: "tfcalcPluginEnabledCheckBox"
                    text: "启用内置明文导入插件"
                    checked: dialog.controller.tfcalcPluginEnabled
                    onClicked: dialog.controller.setTfcalcPluginEnabled(checked)
                }
                ThemeControls.ThemeButton {
                    text: "选择文件…"
                    enabled: dialog.controller.tfcalcPluginEnabled
                    onClicked: fileDialog.open()
                }
            }
            ThemeControls.ThemeLabel { textFormat: Text.PlainText;
                Layout.fillWidth: true
                text: dialog.importState.path || "尚未加载可用层表。只支持已确认的 VERSION 1 明文子集。"
                wrapMode: Text.WrapAnywhere
            }
            ThemeControls.ThemeLabel { textFormat: Text.PlainText;
                Layout.fillWidth: true
                text: dialog.importState.error || "只读取原文件。预览和材料映射不会修改当前设计。"
                color: dialog.importState.error ? dialog.themeTokens.colorError : dialog.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            RowLayout {
                Layout.fillWidth: true
                enabled: dialog.importState.loaded || false
                ThemeControls.ThemeLabel { textFormat: Text.PlainText; text: "原始层表" }
                ThemeControls.ThemeComboBox {
                    id: sourceTable
                    objectName: "tfcalcSourceTable"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    model: ["第一层表（" + (dialog.importState.primary_count || 0) + " 层）",
                            "第二层表（" + (dialog.importState.secondary_count || 0) + " 层）"]
                    onActivated: { dialog.mappings = Object.create(null); dialog.inputsReset(); dialog.invalidate() }
                }
                ThemeControls.ThemeLabel { textFormat: Text.PlainText; text: "替换目标面" }
                ThemeControls.ThemeComboBox {
                    id: targetSide
                    objectName: "tfcalcTargetSide"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    model: ["正面 front", "背面 back（需当前基片双面）"]
                    onActivated: dialog.invalidate()
                }
            }
            ThemeControls.ThemeLabel { textFormat: Text.PlainText;
                Layout.fillWidth: true
                text: "当前映射：LAYER " + (thicknessField.currentIndex === 0 ? "第一数值" : "第二数值")
                      + " → 物理 " + (thicknessUnit.currentIndex === 0 ? "nm" : "μm")
                      + "；源层序 " + (sourceOrder.currentIndex === 0 ? "基片 → 外侧" : "外侧 → 基片（导入时反转）")
                      + "。默认来自用户明文样例确认，仅覆盖受限 profile；其他文件应核对预览或高级映射。"
                color: dialog.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            CheckBox { id: advanced; text: "高级映射（未知语义须自行核对）" }
            RowLayout {
                Layout.fillWidth: true
                visible: advanced.checked
                enabled: dialog.importState.loaded || false
                ThemeControls.ThemeComboBox {
                    id: thicknessField
                    objectName: "tfcalcThicknessField"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    model: ["第一数值（字段 3）", "第二数值（字段 4）"]
                    onActivated: dialog.invalidate()
                }
                ThemeControls.ThemeComboBox {
                    id: thicknessUnit
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    model: ["物理 nm", "物理 μm"]
                    onActivated: dialog.invalidate()
                }
                ThemeControls.ThemeComboBox {
                    id: sourceOrder
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    model: ["基片 → 外侧", "外侧 → 基片（导入时反转）"]
                    onActivated: dialog.invalidate()
                }
            }
            ThemeControls.ThemeLabel { textFormat: Text.PlainText;
                Layout.fillWidth: true
                text: "前 8 层原始数值（两列不互相换算）：\n" +
                      (sourceTable.currentIndex === 0 ? (dialog.importState.primary_rows || []) : (dialog.importState.secondary_rows || []))
                        .map(function(row) { return row.number + "  " + row.material + "  " + row.value3 + "  " + row.value4 }).join("\n")
                wrapMode: Text.WordWrap
            }
            ThemeControls.ThemeLabel { textFormat: Text.PlainText; text: "材料别名 → 已有项目材料 / 所属光学数据集（必须显式选择）" }
            Repeater {
                model: dialog.aliases
                delegate: RowLayout {
                    id: mappingRow
                    required property string modelData
                    property string materialId: ""
                    Layout.fillWidth: true
                    ThemeControls.ThemeLabel {
                        objectName: "tfcalcAlias_" + mappingRow.modelData
                        textFormat: Text.PlainText
                        text: mappingRow.modelData
                        Layout.preferredWidth: 125
                        Layout.minimumWidth: 0
                        Layout.maximumWidth: 125
                        elide: Text.ElideRight
                    }
                    ThemeControls.ThemeComboBox {
                        id: materialPicker
                        objectName: "tfcalcMaterial_" + mappingRow.modelData
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        model: dialog.controller.filmOptions
                        textRole: "name"
                        currentIndex: 0
                        onActivated: function(index) {
                            mappingRow.materialId = String(materialPicker.model[index].id || "")
                            dataset.currentIndex = 0
                            dialog.setMapping(mappingRow.modelData, mappingRow.materialId, "")
                        }
                    }
                    ThemeControls.ThemeComboBox {
                        id: dataset
                        objectName: "tfcalcDataset_" + mappingRow.modelData
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        model: dialog.controller.opticalSetsForFilm(mappingRow.materialId)
                        textRole: "name"
                        currentIndex: 0
                        onActivated: function(index) {
                            dialog.setMapping(mappingRow.modelData, mappingRow.materialId, String(dataset.model[index].id || ""))
                        }
                    }
                    Connections {
                        target: dialog
                        function onInputsReset() {
                            mappingRow.materialId = ""
                            materialPicker.currentIndex = 0
                            dataset.currentIndex = 0
                        }
                    }
                }
            }
            RowLayout {
                ThemeControls.ThemeLabel { textFormat: Text.PlainText; text: "确认参考波长 (nm)" }
                ThemeControls.ThemeField {
                    id: referenceWave
                    objectName: "tfcalcReferenceWavelength"
                    onTextEdited: dialog.invalidate()
                }
            }
            ThemeControls.ThemeLabel { textFormat: Text.PlainText;
                Layout.fillWidth: true
                text: "默认沿用当前参考波长。修改此全局值会改变保留面的 QW 层物理厚度；nm/μm 层不变。"
                wrapMode: Text.WordWrap
                color: dialog.themeTokens.colorTextMuted
            }
            CheckBox {
                id: preserveEnvironment
                objectName: "tfcalcPreserveEnvironment"
                Layout.fillWidth: true
                text: "确认沿用当前环境及设置"
                onClicked: dialog.invalidate()
            }
            ThemeControls.ThemeLabel { textFormat: Text.PlainText;
                Layout.fillWidth: true
                text: "当前基片：" + dialog.controller.substrateLabel()
                      + "\n入射：" + dialog.controller.mediumLabel("incident_medium")
                      + "；出射：" + dialog.controller.mediumLabel("exit_medium")
                      + "；入射面：" + (dialog.controller.designMeta.first_surface || "front")
                      + "；use_k：" + dialog.controller.designMeta.use_k
                      + "；assume_lossless：" + dialog.controller.designMeta.assume_lossless
                wrapMode: Text.WordWrap
            }
            ThemeControls.ThemeLabel { textFormat: Text.PlainText;
                Layout.fillWidth: true
                text: "包括基片、介质、入射面、光学开关、另一面层记录及计算设置。参考波长采用上方值，其对 QW 层的全局影响已核对。"
                wrapMode: Text.WordWrap
            }
            CheckBox { id: rawReport; text: "查看未解释字段和原始参考记录" }
            ThemeControls.ThemeLabel { textFormat: Text.PlainText;
                Layout.fillWidth: true
                visible: rawReport.checked
                text: (dialog.importState.report || "") + (dialog.importState.formula ? "\n原始表达式（仅参考）：\n" + dialog.importState.formula : "")
                        + (dialog.importState.environment ? "\n原始环境记录（未解释，不导入）：\n" + dialog.importState.environment : "")
                wrapMode: Text.WrapAnywhere
                color: dialog.themeTokens.colorTextMuted
            }
            ThemeControls.ThemeLabel { textFormat: Text.PlainText;
                Layout.fillWidth: true
                text: dialog.importState.ready ? dialog.importState.summary : "核对映射后先生成预览，再确认替换。导入后须另存为 .cfilm。"
                wrapMode: Text.WordWrap
            }
        }
    }
    footer: RowLayout {
        spacing: dialog.themeTokens.spaceSm
        Item { Layout.fillWidth: true }
        ThemeControls.ThemeButton { text: "取消"; onClicked: dialog.close() }
        ThemeControls.ThemeButton {
            objectName: "tfcalcPreviewButton"
            text: "生成导入预览"
            enabled: dialog.importState.loaded || false
            onClicked: dialog.controller.previewTfcalcImport(dialog.parameters())
        }
        ThemeControls.ThemeButton {
            objectName: "tfcalcApplyButton"
            text: "确认替换…"
            primary: true
            enabled: dialog.importState.ready || false
            onClicked: dialog.controller.requestApplyTfcalcImport(dialog.importState.token || "")
        }
    }
}
