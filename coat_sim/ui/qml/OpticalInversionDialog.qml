pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs as FileDialogs
import "theme_controls" as ThemeControls

ThemePageDialog {
    id: dialog
    objectName: "opticalInversionDialog"
    required property var controller
    property var measurementRows: [({quantity: "R", wavelength_unit: "nm", value_unit: "fraction", angle_deg: "0", polarization: "unpolarized", text: "", source: ""})]
    property int importRow: -1
    signal savedAsNew()
    title: "从实测 R/T 反演 · 新光学数据集"
    modal: true
    anchors.centerIn: parent
    width: Math.max(280, Math.min(1040, parent.width - 28))
    height: Math.max(260, Math.min(860, parent.height - 28))
    standardButtons: Dialog.NoButton
    closePolicy: Popup.CloseOnEscape
    onOpened: controller.beginSession()
    onClosed: controller.invalidate()

    function changeInput() { if (visible) controller.inputsChanged() }
    function setMeasurement(index, field, value) {
        if (index >= 0 && index < measurementRows.length) measurementRows[index][field] = value
        changeInput()
    }
    function addMeasurement(quantity) {
        if (measurementRows.length >= 8) return
        let next = measurementRows.slice()
        next.push({quantity: quantity, wavelength_unit: "nm", value_unit: "fraction", angle_deg: "0", polarization: "unpolarized", text: "", source: ""})
        measurementRows = next
        changeInput()
    }
    function formPayload() {
        return JSON.stringify({model: modelChoice.currentIndex === 0 ? "cauchy" : "lorentz",
            transparent_region: transparent.checked, absolute_specular: absolute.checked,
            film_thickness_nm: filmThickness.text, substrate_n: substrateN.text,
            substrate_k: substrateK.text, substrate_lossless: lossless.checked,
            substrate_thickness_nm: substrateThickness.text,
            backside: ["", "incoherent", "coherent", "semi_infinite"][backside.currentIndex],
            into_substrate_confirmed: intoSubstrate.checked, spectra: measurementRows})
    }
    Connections {
        target: dialog.controller
        function onApplied() { dialog.savedAsNew(); dialog.close() }
    }

    FileDialogs.FileDialog {
        id: measurementPicker
        title: "导入波长、测量值两列 UTF-8 文本"
        nameFilters: ["光谱表 (*.csv *.tsv *.txt)"]
        onAccepted: {
            const result = JSON.parse(dialog.controller.readMeasurementFile(selectedFile.toString()))
            if (!result.ok) { importStatus.text = result.message; return }
            let next = dialog.measurementRows.slice()
            next[dialog.importRow] = Object.assign({}, next[dialog.importRow], {text: result.text, source: result.source})
            dialog.measurementRows = next
            importStatus.text = "已载入文本；仍须明确单位、角度、偏振和绝对镜面测量条件。"
            dialog.changeInput()
        }
    }
    FileDialogs.FileDialog {
        id: reportPicker
        objectName: "opticalInversionReportPicker"
        title: "导出完整拟合报告 · 请选择新文件名"
        fileMode: FileDialogs.FileDialog.SaveFile
        nameFilters: ["JSON 报告 (*.json)"]
        defaultSuffix: "json"
        onAccepted: dialog.controller.exportReport(selectedFile.toString())
    }

    // Lightweight, passive curves share the page's tokens. No web view or script output.
    component PreviewPlot: Rectangle {
        id: plot
        property var waves: []
        property var curves: []
        property string caption: ""
        required property var themeColors
        Layout.fillWidth: true
        Layout.preferredHeight: 190
        color: plot.themeColors.colorSurfaceRaised
        border.color: plot.themeColors.colorBorder
        radius: 6
        onWavesChanged: canvas.requestPaint()
        onCurvesChanged: canvas.requestPaint()
        onThemeColorsChanged: canvas.requestPaint()
        ThemeControls.ThemeLabel { anchors.top: parent.top; anchors.left: parent.left; anchors.margins: 8; text: plot.caption; font.bold: true }
        Canvas {
            id: canvas
            anchors.fill: parent
            anchors.topMargin: 30
            anchors.margins: 8
            onWidthChanged: canvas.requestPaint()
            onHeightChanged: canvas.requestPaint()
            onPaint: {
                let ctx = canvas.getContext("2d")
                ctx.reset()
                if (plot.waves.length < 2) return
                let lo = Infinity, hi = -Infinity
                for (let values of plot.curves) for (let value of values) {
                    if (Number.isFinite(value)) { lo = Math.min(lo, value); hi = Math.max(hi, value) }
                }
                if (!Number.isFinite(lo) || !Number.isFinite(hi)) return
                if (hi - lo < 1e-8) { lo -= 0.01; hi += 0.01 }
                const x0 = plot.waves[0], x1 = plot.waves[plot.waves.length - 1]
                const left = 52, right = canvas.width - 12, top = 9, bottom = canvas.height - 24
                ctx.strokeStyle = plot.themeColors.colorBorder.toString(); ctx.lineWidth = 1
                ctx.strokeRect(left, top, right - left, bottom - top)
                ctx.fillStyle = plot.themeColors.colorTextMuted.toString(); ctx.font = '11px "' + plot.themeColors.fontFamily + '"'
                ctx.fillText(hi.toPrecision(4), 0, top + 9); ctx.fillText(lo.toPrecision(4), 0, bottom)
                ctx.fillText(x0.toPrecision(5) + " nm", left, canvas.height - 5)
                ctx.fillText(x1.toPrecision(5) + " nm", Math.max(left, right - 72), canvas.height - 5)
                for (let c = 0; c < plot.curves.length; ++c) {
                    ctx.strokeStyle = c === 0 ? plot.themeColors.colorAccent.toString() : plot.themeColors.colorText.toString()
                    ctx.lineWidth = c === 0 ? 2 : 1.4; ctx.beginPath()
                    let values = plot.curves[c]
                    for (let i = 0; i < Math.min(values.length, plot.waves.length); ++i) {
                        let x = left + (plot.waves[i] - x0) / (x1 - x0) * (right - left)
                        let y = bottom - (values[i] - lo) / (hi - lo) * (bottom - top)
                        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
                    }
                    ctx.stroke()
                }
            }
        }
    }

    contentItem: ScrollView {
        id: scroll
        objectName: "opticalInversionScroll"
        clip: true
        contentWidth: availableWidth
        ColumnLayout {
            width: scroll.availableWidth
            spacing: 12
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true; wrapMode: Text.Wrap
                text: "已知膜厚的单层、均匀、各向同性薄膜。空气入射/出射；基片常数 n/k 须已知。拟合不识别膜厚、粗糙散射或渐变结构；低残差不保证唯一解。"
            }
            ColumnLayout {
                Layout.fillWidth: true
                enabled: !dialog.controller.busy
                CheckBox { id: absolute; objectName: "inversionAbsoluteSpecular"; text: "确认输入为已校准的绝对镜面 R/T"; onCheckedChanged: dialog.changeInput(); Layout.fillWidth: true }
                ThemeControls.ThemeLabel { Layout.fillWidth: true; wrapMode: Text.Wrap; text: "不能使用相对强度、漫反射/积分球总量或未经校准的测量。" }
                GridLayout {
                    Layout.fillWidth: true
                    columns: width < 650 ? 2 : 4
                    ThemeControls.ThemeLabel { text: "已知膜厚 / nm" }
                    ThemeControls.ThemeField { id: filmThickness; objectName: "inversionFilmThickness"; Layout.fillWidth: true; accessibleLabel: "已知膜厚，nm"; placeholderText: "实测膜厚"; onTextChanged: dialog.changeInput() }
                    ThemeControls.ThemeLabel { text: "基片厚度 / nm" }
                    ThemeControls.ThemeField { id: substrateThickness; objectName: "inversionSubstrateThickness"; Layout.fillWidth: true; accessibleLabel: "基片厚度，nm"; placeholderText: "例如 1 mm = 1000000 nm"; onTextChanged: dialog.changeInput() }
                    ThemeControls.ThemeLabel { text: "已知基片 n" }
                    ThemeControls.ThemeField { id: substrateN; objectName: "inversionSubstrateN"; Layout.fillWidth: true; accessibleLabel: "已知基片n"; placeholderText: "本波段常数"; onTextChanged: dialog.changeInput() }
                    ThemeControls.ThemeLabel { text: "已知基片 k" }
                    ThemeControls.ThemeField { id: substrateK; objectName: "inversionSubstrateK"; Layout.fillWidth: true; accessibleLabel: "已知基片k"; placeholderText: "未知保持空白"; onTextChanged: dialog.changeInput() }
                }
                CheckBox { id: lossless; objectName: "inversionSubstrateLossless"; Layout.fillWidth: true; text: "明确采用基片无损假设（k=0）"; onCheckedChanged: dialog.changeInput() }
                ThemeControls.ThemeLabel { Layout.fillWidth: true; wrapMode: Text.Wrap; text: "未勾选且基片k未知时不能拟合，不会自动填零；本页基片n/k按已知常数处理。" }
                ThemeControls.ThemeComboBox {
                    id: backside; objectName: "inversionBackside"; Layout.fillWidth: true
                    accessibleLabel: "基片背面及相干模型"
                    model: ["请选择基片背面/相干模型", "有限基片 + 非相干背面反射（全片外部T）", "有限基片 + 全相干背面（保留条纹）", "半无限基片（不含背面；T为进入基片通量）"]
                    onCurrentIndexChanged: dialog.changeInput()
                }
                CheckBox { id: intoSubstrate; objectName: "inversionIntoSubstrate"; visible: backside.currentIndex === 3; text: "确认T为进入基片的通量，非全片外部T"; onCheckedChanged: dialog.changeInput(); Layout.fillWidth: true }
                ThemeControls.ThemeComboBox {
                    id: modelChoice; objectName: "inversionModel"; Layout.fillWidth: true
                    accessibleLabel: "反演低维色散模型"
                    model: ["透明区 Cauchy：n=A+B/λ²+C/λ⁴，固定k=0", "单 Lorentz + ε∞：被动吸收模型（能量eV）"]
                    onCurrentIndexChanged: dialog.changeInput()
                }
                CheckBox { id: transparent; objectName: "inversionTransparentRegion"; visible: modelChoice.currentIndex === 0; Layout.fillWidth: true; text: "明确采用透明波段假设（固定k=0）"; onCheckedChanged: dialog.changeInput() }
                ThemeControls.ThemeLabel { visible: modelChoice.currentIndex === 0; Layout.fillWidth: true; wrapMode: Text.Wrap; text: "固定k=0是模型假设，不能据此证明材料无吸收。" }
                ThemeControls.ThemeLabel {
                    Layout.fillWidth: true; wrapMode: Text.Wrap
                    text: modelChoice.currentIndex === 0
                        ? "参数域：A∈[1,4]，B∈[0,0.3] μm²，C∈[0,0.03] μm⁴；λ以μm代入。"
                        : "参数域：ε∞∈[1,16]，强度∈[0,100] eV²，共振能∈[0.2,12] eV，阻尼∈[0.01,5] eV；强吸收与有限波段可能欠约束。"
                }
                Repeater {
                    model: dialog.measurementRows
                    delegate: ColumnLayout {
                        id: measurement
                        required property int index
                        required property var modelData
                        Layout.fillWidth: true
                        spacing: 5
                        ThemeControls.ThemeLabel { text: "测量谱 " + (measurement.index + 1) + " · 两列：波长、测量值"; font.bold: true }
                        GridLayout {
                            Layout.fillWidth: true
                            columns: width < 650 ? 2 : 5
                            ThemeControls.ThemeComboBox { objectName: "inversionQuantity_" + measurement.index; Layout.fillWidth: true; model: ["R", "T"]; currentIndex: measurement.modelData.quantity === "T" ? 1 : 0; accessibleLabel: "测量物理量"; onCurrentTextChanged: dialog.setMeasurement(measurement.index, "quantity", currentText) }
                            ThemeControls.ThemeComboBox { objectName: "inversionWaveUnit_" + measurement.index; Layout.fillWidth: true; model: ["nm", "um"]; currentIndex: measurement.modelData.wavelength_unit === "um" ? 1 : 0; accessibleLabel: "波长单位"; onCurrentTextChanged: dialog.setMeasurement(measurement.index, "wavelength_unit", currentText) }
                            ThemeControls.ThemeComboBox { objectName: "inversionValueUnit_" + measurement.index; Layout.fillWidth: true; model: ["fraction", "%"]; currentIndex: measurement.modelData.value_unit === "%" ? 1 : 0; accessibleLabel: "测量数值单位"; onCurrentTextChanged: dialog.setMeasurement(measurement.index, "value_unit", currentText) }
                            ThemeControls.ThemeField { objectName: "inversionAngle_" + measurement.index; Layout.fillWidth: true; text: measurement.modelData.angle_deg; placeholderText: "入射角/°"; accessibleLabel: "入射角，度"; onTextChanged: dialog.setMeasurement(measurement.index, "angle_deg", text) }
                            ThemeControls.ThemeComboBox { id: polarizationChoice; objectName: "inversionPolarization_" + measurement.index; Layout.fillWidth: true; model: ["unpolarized", "s", "p"]; currentIndex: Math.max(0, polarizationChoice.model.indexOf(measurement.modelData.polarization)); accessibleLabel: "偏振"; onCurrentTextChanged: dialog.setMeasurement(measurement.index, "polarization", currentText) }
                        }
                        ThemeControls.ThemeField { objectName: "inversionSource_" + measurement.index; Layout.fillWidth: true; text: measurement.modelData.source; placeholderText: "必填：测量来源/文件名、校准与仪器说明"; accessibleLabel: "测量来源"; onTextChanged: dialog.setMeasurement(measurement.index, "source", text) }
                        TextArea { objectName: "inversionTable_" + measurement.index; Layout.fillWidth: true; Layout.preferredHeight: 115; text: measurement.modelData.text; placeholderText: "wavelength,value\n请粘贴自己的实测数据；不自动填充演示谱"; wrapMode: TextEdit.NoWrap; selectByMouse: true; Accessible.name: "波长和测量值两列表"; onTextChanged: dialog.setMeasurement(measurement.index, "text", text) }
                        RowLayout {
                            ThemeControls.ThemeButton { objectName: "inversionImport_" + measurement.index; text: "导入本地 TXT / CSV / TSV"; onClicked: { dialog.importRow = measurement.index; measurementPicker.open() } }
                            ThemeControls.ThemeButton { text: "移除此谱"; enabled: dialog.measurementRows.length > 1; onClicked: { let next = dialog.measurementRows.slice(); next.splice(measurement.index, 1); dialog.measurementRows = next; dialog.changeInput() } }
                        }
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    ThemeControls.ThemeButton { objectName: "inversionAddR"; text: "＋ R谱"; enabled: dialog.measurementRows.length < 8; onClicked: dialog.addMeasurement("R") }
                    ThemeControls.ThemeButton { objectName: "inversionAddT"; text: "＋ T谱"; enabled: dialog.measurementRows.length < 8; onClicked: dialog.addMeasurement("T") }
                }
                ThemeControls.ThemeLabel { id: importStatus; Layout.fillWidth: true; wrapMode: Text.Wrap }
            }
            ThemeControls.ThemeLabel { Layout.fillWidth: true; wrapMode: Text.Wrap; text: "每条谱按原始波长、角度和偏振前向计算，不做隐式插值对齐。仅R也可估计，但通常更欠约束；R+T同样不保证唯一。" }
            RowLayout {
                ThemeControls.ThemeButton { objectName: "inversionFitButton"; text: "拟合并预览"; enabled: !dialog.controller.busy; onClicked: dialog.controller.startFit(dialog.formPayload()) }
                BusyIndicator { running: dialog.controller.busy; visible: running; Layout.preferredWidth: 30; Layout.preferredHeight: 30 }
                ThemeControls.ThemeButton { objectName: "inversionCancelFit"; text: "取消拟合"; visible: dialog.controller.busy; onClicked: { dialog.controller.invalidate(); dialog.controller.beginSession() } }
            }
            ThemeControls.ThemeLabel { objectName: "inversionStatus"; Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText; text: dialog.controller.status }
            ColumnLayout {
                Layout.fillWidth: true
                visible: dialog.controller.canApply
                ThemeControls.ThemeLabel { text: "预览 · 尚未写入材料"; font.bold: true }
                ScrollView {
                    Layout.fillWidth: true; Layout.preferredHeight: 190; clip: true
                    TextArea { objectName: "inversionQuality"; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; text: dialog.controller.qualityText; Accessible.name: "RMSE、参数、边界、多起点和欠约束质量报告" }
                }
                Repeater {
                    model: dialog.controller.preview.spectra || []
                    delegate: ColumnLayout {
                        id: spectrumPreview
                        required property var modelData
                        Layout.fillWidth: true
                        PreviewPlot { themeColors: dialog.themeTokens; waves: spectrumPreview.modelData.wavelength_nm || []; curves: [spectrumPreview.modelData.measured_fraction || [], spectrumPreview.modelData.predicted_fraction || []]; caption: String(spectrumPreview.modelData.quantity || "") + " 实测（强调色）/ 回代 · 比例值" }
                        PreviewPlot { themeColors: dialog.themeTokens; waves: spectrumPreview.modelData.wavelength_nm || []; curves: [spectrumPreview.modelData.residual_fraction || []]; caption: "回代 − 实测残差 · 比例值"; Layout.preferredHeight: 145 }
                    }
                }
                PreviewPlot { objectName: "inversionNkPlot"; themeColors: dialog.themeTokens; waves: (dialog.controller.preview.nk || {}).wavelength_nm || []; curves: [(dialog.controller.preview.nk || {}).n || [], (dialog.controller.preview.nk || {}).k || []]; caption: "反演 n（强调色）/ k · 仅覆盖测量波段，不外推" }
                ThemeControls.ThemeField { id: outputName; objectName: "inversionOutputName"; Layout.fillWidth: true; accessibleLabel: "新光学数据集名称"; placeholderText: "必填：新数据集名称（不会覆盖原集）" }
                ThemeControls.ThemeField { id: outputNotes; objectName: "inversionOutputNotes"; Layout.fillWidth: true; accessibleLabel: "新数据集备注"; placeholderText: "可选：使用限制/样品说明" }
                RowLayout {
                    ThemeControls.ThemeButton { objectName: "inversionCopyReport"; text: "复制完整JSON"; onClicked: dialog.controller.copyReport() }
                    ThemeControls.ThemeButton { objectName: "inversionExportReport"; text: "导出完整JSON…"; onClicked: reportPicker.open() }
                }
                ThemeControls.ThemeLabel { Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText; text: dialog.controller.reportReference.length ? "报告：" + dialog.controller.reportReference + "\n外部报告须手动随材料保留；不会自动上传或复制到其它工程。" : "小谱完整来源/残差内嵌。来源或备注超过20000字符时，须先显式导出完整报告才能添加，不截断原谱。" }
            }
        }
    }
    footer: DialogButtonBox {
        ThemeControls.ThemeButton { objectName: "inversionApplyButton"; text: "另存为新数据集（材料草稿）"; enabled: dialog.controller.canApply && outputName.text.trim().length > 0; onClicked: dialog.controller.applyAsNew(outputName.text, outputNotes.text) }
        ThemeControls.ThemeButton { objectName: "inversionCloseButton"; text: "关闭 / 不添加"; onClicked: dialog.close() }
    }
}
