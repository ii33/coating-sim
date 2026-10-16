import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "theme_controls" as ThemeControls

Dialog {
    id: dialog
    objectName: "filmStackDialog"
    required property var editor
    property var controller: editor.filmStack
    property url importUrl
    modal: true
    title: "批量膜层"
    width: Math.min(980, parent ? parent.width - 32 : 980)
    height: Math.min(720, parent ? parent.height - 32 : 720)
    x: parent ? (parent.width - width) / 2 : 0
    y: parent ? (parent.height - height) / 2 : 0
    standardButtons: Dialog.NoButton
    property bool importing: tabs.currentIndex === 0
    ListModel { id: mappings }
    ListModel { id: generatorMappings }
    property var activeMappings: importing ? mappings : generatorMappings
    function sourceOption(slot) {
        return (editor.sourcePreview.sources || []).find(source => source.slot === slot) || null
    }
    function defaultRow(material, slot) {
        return {material: material, sourceSlot: slot, rate: "", indexValue: "", maskChoice: 0, thickness: "100"}
    }
    function syncMaterials(names) {
        names = names.map(name => String(name).trim()).filter((name, index, list) => name.length && list.indexOf(name) === index)
        if (names.length === mappings.count) {
            let same = true
            for (let i = 0; i < names.length; ++i) if (mappings.get(i).material !== names[i]) same = false
            if (same) return
        }
        const old = {}
        for (let i = 0; i < mappings.count; ++i) {
            const m = mappings.get(i)
            old[m.material] = {material: m.material, sourceSlot: m.sourceSlot, rate: m.rate,
                               indexValue: m.indexValue, maskChoice: m.maskChoice, thickness: m.thickness}
        }
        mappings.clear()
        for (let i = 0; i < names.length; ++i) mappings.append(old[names[i]] || defaultRow(names[i], 0))
    }
    function refreshGeneratorMaterials() {
        if (!generatorMappings.count) {
            const sources = editor.sourcePreview.sources || []
            for (let i = 0; i < Math.min(2, sources.length); ++i)
                generatorMappings.append(defaultRow("", sources[i].slot))
        }
    }
    function addGeneratorSource() {
        const sources = editor.sourcePreview.sources || []
        if (sources.length && generatorMappings.count < 256)
            generatorMappings.append(defaultRow("", sources[generatorMappings.count % sources.length].slot))
    }
    onOpened: refreshGeneratorMaterials()
    function readImport() {
        if (importUrl.toString().length)
            controller.inspectFile(importUrl, {header_row: headerRow.value, has_header: hasHeader.checked,
                sheet: sheetBox.currentText || null, delimiter: [null, "\t", ",", ";", "whitespace"][delimiterBox.currentIndex], encoding: encodingBox.currentIndex === 0 ? "utf-8-sig" : "gb18030"})
    }
    function buildParameters() {
        const spec = {sources: []}
        const rows = activeMappings
        const sequence = []
        const layers = []
        for (let i = 0; i < rows.count; ++i) {
            const m = rows.get(i)
            const source = sourceOption(m.sourceSlot)
            if (!source || !source.valid) throw Error("第 " + (i + 1) + " 项请选择有效的已保存蒸发源")
            const alias = importing ? m.material : "source_choice_" + (i + 1)
            const value = {alias: alias, source_slot: m.sourceSlot,
                           target_mode: ["mean", "center", "monitor"][basis.currentIndex]}
            if (m.rate.trim().length) value.rate_angstrom_s = Number(m.rate)
            if (basis.currentIndex === 2) value.monitor_slot = monitor.value
            const boardChoice = m.maskChoice === 0 ? boards.currentIndex : m.maskChoice - 1
            if (boardChoice > 0) value.mask_slots = [[], [0], [1], [0, 1]][boardChoice - 1]
            if (m.indexValue.length) value.refractive_index = Number(m.indexValue)
            spec.sources.push(value)
            sequence.push(alias)
            layers.push({material: alias, thickness_nm: Number(m.thickness)})
        }
        if (importing) {
            if (units.currentIndex < 1 || order.currentIndex < 1) throw Error("请确认厚度单位和层序")
            spec.mode = "import"
            spec.columns = {material: materialColumn.currentIndex, thickness: thicknessColumn.currentIndex}
            spec.thickness_unit = ["", "nm", "um", "angstrom"][units.currentIndex]
            spec.order = ["", "substrate_to_air", "air_to_substrate"][order.currentIndex]
            spec.decimal_separator = decimalBox.currentIndex === 1 ? "," : "."
        } else {
            spec.mode = ["equal", "periodic", "quarter_wave", "explicit"][method.currentIndex]
            if (spec.mode === "periodic") { spec.period_layers = layers; spec.repeat_count = repetitions.value }
            else if (spec.mode === "explicit") spec.layers = layers
            else {
                spec.sequence = sequence
                spec.layer_count = layerCount.value
                if (spec.mode === "equal") spec.total_thickness_nm = Number(totalThickness.numericDisplayValue)
                else spec.reference_wavelength_nm = Number(wavelength.numericDisplayValue)
            }
        }
        return spec
    }
    Connections {
        target: controller
        function onChanged() {
            if (dialog.importing) dialog.syncMaterials(controller.state.materials || [])
        }
    }
    FileDialog {
        id: fileDialog
        title: "导入物理膜厚层表"
        nameFilters: ["层表 (*.csv *.tsv *.txt *.xlsx *.json)", "所有文件 (*)"]
        onAccepted: { dialog.importUrl = selectedFile; sheetBox.currentIndex = -1; hasHeader.checked = true; headerRow.value = 1; delimiterBox.currentIndex = 0; dialog.readImport() }
    }
    ColumnLayout {
        anchors.fill: parent
        spacing: 8
        TabBar {
            id: tabs; objectName: "filmStackTabs"; Layout.fillWidth: true
            TabButton { text: "导入层表" }
            TabButton { text: "参数化生成" }
            onCurrentIndexChanged: {
                if (dialog.importing && materialColumn) controller.selectMaterialColumn(materialColumn.currentIndex)
                else dialog.refreshGeneratorMaterials()
            }
        }
        ScrollView {
            Layout.fillWidth: true; Layout.fillHeight: true
            contentWidth: availableWidth
            ColumnLayout {
                width: parent.width
                spacing: 10
                ColumnLayout {
                    visible: dialog.importing; Layout.fillWidth: true
                    RowLayout {
                        Layout.fillWidth: true
        Button { text: controller.state.busy ? "读取中" : "选择层表"; enabled: !controller.state.busy; onClicked: fileDialog.open() }
                        Label { Layout.fillWidth: true; elide: Text.ElideMiddle; text: dialog.importUrl.toString() }
                    }
                    Flow {
                        Layout.fillWidth: true; spacing: 8
                        Row { spacing: 5; Label { text: "工作表"; anchors.verticalCenter: parent.verticalCenter }
                            ComboBox { id: sheetBox; width: 160; model: controller.state.sheets || []; onActivated: dialog.readImport() } }
                        Row { spacing: 5; Label { text: "表头行"; anchors.verticalCenter: parent.verticalCenter }
                            SpinBox { id: headerRow; from: 1; to: 100; value: 1; onValueModified: dialog.readImport() } }
                        CheckBox { id: hasHeader; text: "含表头"; checked: true; onClicked: dialog.readImport() }
                        ComboBox { id: delimiterBox; width: 130; model: ["自动分隔符", "制表符", "逗号", "分号", "空白"]; onActivated: dialog.readImport() }
                        ComboBox { id: encodingBox; width: 120; model: ["UTF-8", "GB18030"]; onActivated: dialog.readImport() }
                    }
                    Flow {
                        Layout.fillWidth: true; spacing: 8
                        Row { spacing: 5; Label { text: "材料列"; anchors.verticalCenter: parent.verticalCenter }
                            ComboBox { id: materialColumn; width: 150; model: controller.state.columns || []
                                onCurrentIndexChanged: controller.selectMaterialColumn(currentIndex) } }
                        Row { spacing: 5; Label { text: "厚度列"; anchors.verticalCenter: parent.verticalCenter }
                            ComboBox { id: thicknessColumn; width: 150; model: controller.state.columns || []; currentIndex: count > 1 ? 1 : 0 } }
                        ComboBox { id: units; width: 140; model: ["确认厚度单位", "nm", "µm", "Å"] }
                        ComboBox { id: order; width: 170; model: ["确认沉积层序", "基片 → 空气", "空气 → 基片"] }
                        ComboBox { id: decimalBox; width: 120; model: ["小数点 .", "小数逗号 ,"] }
                    }
                    Label { text: "共 " + (controller.state.row_count || 0) + " 行，预览前 8 行" }
                    ScrollView {
                        Layout.fillWidth: true; Layout.preferredHeight: 140
                        TextArea { readOnly: true; selectByMouse: true; textFormat: TextEdit.PlainText; wrapMode: TextEdit.NoWrap
                            text: (controller.state.columns || []).join("\t") + "\n" + (controller.state.preview || []).map(row => Array.isArray(row) ? row.join("\t") : JSON.stringify(row)).join("\n") }
                    }
                    Label { Layout.fillWidth: true; wrapMode: Text.WordWrap
                        text: "TFCalc / Macleod 请使用物理膜厚层表的文本、CSV 或 Excel 导出。专有原生文件需核对版本格式。图片/PDF 可交给支持视觉的助手读取，再调用层表生成器。" }
                }
                ColumnLayout {
                    visible: !dialog.importing; Layout.fillWidth: true
                    Flow {
                        Layout.fillWidth: true; spacing: 10
                        ComboBox { id: method; width: 180; model: ["总厚度等分", "重复周期", "四分之一波候选", "显式厚度层表"]; onCurrentIndexChanged: dialog.refreshGeneratorMaterials() }
                        Row { visible: method.currentIndex === 0 || method.currentIndex === 2; spacing: 5
                            Label { text: "层数"; anchors.verticalCenter: parent.verticalCenter }
                            SpinBox { id: layerCount; from: 1; to: 10000; value: 60; editable: true } }
                        Row { visible: method.currentIndex === 1; spacing: 5
                            Label { text: "重复次数"; anchors.verticalCenter: parent.verticalCenter }
                            SpinBox { id: repetitions; from: 1; to: 10000; value: 30; editable: true } }
                        Row { visible: method.currentIndex === 0; spacing: 5
                            Label { text: "总厚度 / nm"; anchors.verticalCenter: parent.verticalCenter }
                            ThemeControls.ThemeField { id: totalThickness; width: 110; numericDisplayValue: "7000"; onTextEdited: numericDisplayValue = text } }
                        Row { visible: method.currentIndex === 2; spacing: 5
                            Label { text: "参考波长 / nm"; anchors.verticalCenter: parent.verticalCenter }
                            ThemeControls.ThemeField { id: wavelength; width: 100; numericDisplayValue: "550"; onTextEdited: numericDisplayValue = text } }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Label { Layout.fillWidth: true; wrapMode: Text.WordWrap; text: "按下方源选择顺序生成膜层，材料由各蒸发源设计带入。重复周期和显式层表可逐行设置物理厚度。" }
                        Button { text: "添加源 / 层"; enabled: (editor.sourcePreview.sources || []).length > 0 && generatorMappings.count < 256; onClicked: dialog.addGeneratorSource() }
                    }
                    Label { Layout.fillWidth: true; wrapMode: Text.WordWrap
                        text: method.currentIndex === 2 ? "候选采用 d = λ / (4n)，每种材料需填参考波长下的折射率。此工具只生成物理层表，不验证反射率、带宽、入射角或色散指标。"
                            : "生成顺序为基片 → 空气。替换当前工艺的层表，机器和基片引用保留；保存后才写入文件。" }
                }
                Label { text: dialog.importing ? "层表别名 → 已保存蒸发源" : "已保存蒸发源顺序"; font.bold: true }
                Flow {
                    Layout.fillWidth: true; spacing: 8
                    ComboBox { id: basis; width: 150; model: ["整片平均", "基片中心", "指定监测器"] }
                    SpinBox { id: monitor; visible: basis.currentIndex === 2; from: 1; to: 64; value: 1 }
                    ComboBox { id: boards; width: 180; model: ["统一：沿用机器的板", "统一：不启用板", "统一：仅板 1", "统一：仅板 2", "统一：板 1 + 板 2"] }
                    Button { text: dialog.importing ? "刷新层表别名" : "载入已安装源"; onClicked: dialog.importing ? controller.selectMaterialColumn(materialColumn.currentIndex) : dialog.refreshGeneratorMaterials() }
                }
                Repeater {
                    id: mappingRows
                    model: dialog.activeMappings
                    delegate: ColumnLayout {
                        id: mappingRow
                        required property int index
                        required property string material
                        required property int sourceSlot
                        required property string rate
                        required property string indexValue
                        required property int maskChoice
                        required property string thickness
                        property var selectedSource: dialog.sourceOption(sourceSlot)
                        Layout.fillWidth: true
                        Flow {
                            Layout.fillWidth: true
                            spacing: 8
                            Label { text: dialog.importing ? material : String(mappingRow.index + 1); width: dialog.importing ? 96 : 26; height: 36; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight }
                            ComboBox {
                                objectName: "filmSourceChoice" + mappingRow.index
                                width: 300; model: editor.sourcePreview.sources || []; textRole: "label"
                                currentIndex: sourceSlot - 1
                                displayText: currentIndex >= 0 && currentIndex < count ? currentText : "请选择已保存蒸发源"
                                onActivated: function(choiceIndex) {
                                    dialog.activeMappings.setProperty(mappingRow.index, "sourceSlot", model[choiceIndex].slot)
                                    dialog.activeMappings.setProperty(mappingRow.index, "rate", "")
                                }
                                ToolTip.visible: hovered
                                ToolTip.text: currentText
                            }
                            Row { visible: !dialog.importing && (method.currentIndex === 1 || method.currentIndex === 3); spacing: 5
                                Label { text: "厚度 / nm"; anchors.verticalCenter: parent.verticalCenter }
                                ThemeControls.ThemeField { width: 86; numericDisplayValue: thickness; onTextEdited: dialog.activeMappings.setProperty(mappingRow.index, "thickness", text) }
                            }
                            Row { spacing: 5
                                Label { text: "速率 / Å/s"; anchors.verticalCenter: parent.verticalCenter }
                                ThemeControls.ThemeField { width: 86; numericDisplayValue: rate; placeholderText: mappingRow.selectedSource ? String(mappingRow.selectedSource.default_rate_angstrom_s) : "选择源"
                                    onTextEdited: dialog.activeMappings.setProperty(mappingRow.index, "rate", text)
                                    ToolTip.visible: hovered; ToolTip.text: "留空使用源设计参考速率；此值不是预测或实测沉积速率" }
                            }
                            ComboBox {
                                objectName: "materialMaskChoice" + mappingRow.index
                                width: 175
                                model: ["修正板：沿用统一设置", "修正板：沿用机器", "不启用修正板", "仅板 1", "仅板 2", "板 1 + 板 2"]
                                currentIndex: maskChoice
                                onActivated: function(choiceIndex) { dialog.activeMappings.setProperty(mappingRow.index, "maskChoice", choiceIndex) }
                                Accessible.name: (dialog.importing ? material : "源选择 " + (mappingRow.index + 1)) + "各层使用的修正板"
                            }
                            Row { visible: !dialog.importing; spacing: 2
                                ToolButton { text: "↑"; enabled: mappingRow.index > 0; onClicked: generatorMappings.move(mappingRow.index, mappingRow.index - 1, 1) }
                                ToolButton { text: "↓"; enabled: mappingRow.index + 1 < generatorMappings.count; onClicked: generatorMappings.move(mappingRow.index, mappingRow.index + 1, 1) }
                                ToolButton { text: "×"; onClicked: generatorMappings.remove(mappingRow.index) }
                            }
                            Row { visible: !dialog.importing && method.currentIndex === 2; spacing: 5
                                Label { text: "折射率 n"; anchors.verticalCenter: parent.verticalCenter }
                                ThemeControls.ThemeField { width: 86; numericDisplayValue: indexValue; onTextEdited: dialog.activeMappings.setProperty(mappingRow.index, "indexValue", text) }
                            }
                        }
                    }
                }
                Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: "材料来自所选源设计，只读显示；层表材料列仅作匹配别名。修正板按源选择应用到各层，可覆盖统一设置。板 1 / 板 2 对应机器安装位次。源默认速率可覆写，界面使用 Å/s。"
                    opacity: 0.7
                }
            }
        }
        Label { id: localStatus; Layout.fillWidth: true; wrapMode: Text.WordWrap; text: controller.state.message || "" }
        RowLayout {
            Layout.alignment: Qt.AlignRight
            Button { text: "关闭"; onClicked: dialog.close() }
            Button { text: "替换当前层表"; enabled: !dialog.controller.state.busy && dialog.activeMappings.count > 0
                onClicked: {
                    try { if (controller.build(dialog.buildParameters(), dialog.importing)) dialog.close() }
                    catch (error) { localStatus.text = String(error) }
                } }
        }
    }
}
