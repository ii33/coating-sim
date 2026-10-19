import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs as FileDialogs
import Qt.labs.qmlmodels
import "theme_controls" as ThemeControls

ThemePageDialog {
    id: editor
    required property var library
    property string editingId: ""
    property string nameError: ""
    readonly property var processColumnWidths: [176, 232]
    property int loadedModelIndex: 0
    property string datasetSource: "用户输入，未独立验证"
    property var coefficientValues: []
    property var parameterValues: ({})
    property var existingKData: ({})
    property var processRowOriginals: []
    property int activeNRow: -1
    property int activeNColumn: 0
    property int activeKRow: -1
    property int activeKColumn: 0
    property int activeProcessRow: -1
    property bool nGridFocused: false
    property bool kGridFocused: false
    property string tableNotice: ""
    readonly property bool blocksIndependentKLoss: editingId.length > 0 && selectedSpec() &&
        selectedSpec().kind === "tabulated" && Object.keys(existingKData).length > 0
    signal referenceRequested()
    property var inversionInstance: null
    function openInversion() {
        if (editingId.length || library.readOnly || library.loading) return
        if (!inversionInstance) {
            inversionInstance = inversionComponent.createObject(editor, {
                parent: editor.parent, controller: editor.library.opticalInversion })
            if (inversionInstance)
                inversionInstance.savedAsNew.connect(function() { editor.close() })
        }
        if (inversionInstance) inversionInstance.open()
    }
    onClosed: {
        if (inversionInstance) inversionInstance.close()
        library.cancelOpticalInversion()
    }
    Component {
        id: inversionComponent
        OpticalInversionDialog {}
    }


    function formulaSvgTextColor() {
        if (typeof Theme !== "undefined" && Theme.snapshot && Theme.snapshot.colorText)
            return Theme.snapshot.colorText.toString()
        return editor.palette.text.toString()
    }

    function scrollTableWithWheel(table, event) {
        let delta = event.pixelDelta.y
        if (!Number.isFinite(delta) || delta === 0)
            delta = event.angleDelta.y / 120 * 36
        const maximum = Math.max(0, table.contentHeight - table.height)
        table.contentY = Math.max(0, Math.min(maximum, table.contentY - delta))
        // Consume even at either edge so the outer form does not jump while
        // the pointer remains over an inner editing table.
        event.accepted = true
    }
    function scrollOuterFormWithWheel(event) {
        let delta = event.pixelDelta.y
        if (!Number.isFinite(delta) || delta === 0)
            delta = event.angleDelta.y / 120 * 36
        if (!Number.isFinite(delta) || delta === 0) return
        let flickable = materialEditorScroll.contentItem
        const maximum = Math.max(0, flickable.contentHeight - flickable.height)
        flickable.contentY = Math.max(0, Math.min(maximum, flickable.contentY - delta))
        event.accepted = true
    }

    TableModel {
        id: nTableModel
        objectName: "opticalEditorTableModel"
        TableModelColumn { display: "wavelength" }
        TableModelColumn { display: "n" }
        TableModelColumn { display: "k" }
    }
    Shortcut {
        sequence: "Ctrl+V"
        context: Qt.WindowShortcut
        enabled: editor.visible && nTable.visible && editor.nGridFocused
        onActivated: editor.pasteGrid(nTableModel, editor.activeNRow, editor.activeNColumn, false)
    }
    Shortcut {
        sequence: "Ctrl+V"
        context: Qt.WindowShortcut
        enabled: editor.visible && kTable.visible && editor.kGridFocused
        onActivated: editor.pasteGrid(kTableModel, editor.activeKRow, editor.activeKColumn, true)
    }
    TableModel {
        id: kTableModel
        objectName: "opticalEditorKTableModel"
        TableModelColumn { display: "wavelength" }
        TableModelColumn { display: "k" }
    }
    TableModel {
        id: processTableModel
        objectName: "opticalEditorProcessTableModel"
        TableModelColumn { display: "name" }
        TableModelColumn { display: "value" }
    }

    title: editingId.length ? "编辑光学数据集" : "新增光学数据集"
    modal: true
    anchors.centerIn: parent
    width: Math.min(900, parent.width - 36)
    height: Math.min(740, parent.height - 32)
    standardButtons: Dialog.NoButton

    function selectedSpec() {
        let rows = library.opticalModelCatalog
        return modelType.currentIndex >= 0 && modelType.currentIndex < rows.length ? rows[modelType.currentIndex] : null
    }
    function findSpecIndex(record) {
        let rows = library.opticalModelCatalog
        for (let i = 0; i < rows.length; ++i) {
            if (rows[i].kind === record.kind &&
                    (record.kind !== "rii_formula" || rows[i].formula_number === record.formula_number)) return i
        }
        return 0
    }
    function tableText(record) {
        let cols = [record.wavelengths, record.n]
        if (record.k !== null && record.k !== undefined) cols.push(record.k)
        let lines = []
        for (let i = 0; i < record.wavelengths.length; ++i) {
            let row = []
            for (let j = 0; j < cols.length; ++j) row.push(String(cols[j][i]))
            lines.push(row.join("\t"))
        }
        return lines.join("\n")
    }
    function kTableText(record) {
        if (!record || !record.wavelengths) return ""
        let lines = []
        for (let i = 0; i < record.wavelengths.length; ++i)
            lines.push(String(record.wavelengths[i]) + "\t" + String(record.k[i]))
        return lines.join("\n")
    }
    function gridRow(model, index, kOnly) {
        let row = model.getRow(index)
        return kOnly
                ? { wavelength: String(row.wavelength ?? ""), k: String(row.k ?? "") }
                : { wavelength: String(row.wavelength ?? ""), n: String(row.n ?? ""), k: String(row.k ?? "") }
    }
    function fillNRows(record) {
        nTableModel.clear()
        if (record && record.wavelengths && record.wavelengths.length !== undefined) {
            for (let i = 0; i < record.wavelengths.length; ++i) {
                nTableModel.appendRow({
                    wavelength: String(record.wavelengths[i]),
                    n: String(record.n[i]),
                    k: record.k === null || record.k === undefined ? "" : String(record.k[i])
                })
            }
        }
        if (nTableModel.rowCount === 0) {
            nTableModel.appendRow({ wavelength: "", n: "", k: "" })
            nTableModel.appendRow({ wavelength: "", n: "", k: "" })
        }
        activeNRow = nTableModel.rowCount > 0 ? 0 : -1
    }
    function fillKRows(record) {
        kTableModel.clear()
        if (record && record.wavelengths) {
            for (let i = 0; i < record.wavelengths.length; ++i)
                kTableModel.appendRow({ wavelength: String(record.wavelengths[i]), k: String(record.k[i]) })
        }
        if (kTableModel.rowCount === 0) {
            kTableModel.appendRow({ wavelength: "", k: "" })
            kTableModel.appendRow({ wavelength: "", k: "" })
        }
        activeKRow = kTableModel.rowCount > 0 ? 0 : -1
    }
    function fillProcessRows(rows) {
        processTableModel.clear()
        let state = editor.library.projectOpticalProcessParameterRows(rows || [])
        for (let row of state.rows) processTableModel.appendRow({ name: row.name, value: row.value })
        processRowOriginals = state.originals
        if (processTableModel.rowCount === 0) processTableModel.appendRow({ name: "", value: "" })
        activeProcessRow = processTableModel.rowCount > 0 ? 0 : -1
    }
    function processRows() {
        let rows = []
        for (let i = 0; i < processTableModel.rowCount; ++i) rows.push(processTableModel.getRow(i))
        return editor.library.serializeOpticalProcessParameterRows(rows, processRowOriginals)
    }
    function setProcessCell(rowIndex, columnIndex, value) {
        let row = processTableModel.getRow(rowIndex)
        row[["name", "value"][columnIndex]] = String(value)
        processTableModel.setRow(rowIndex, row)
        activeProcessRow = rowIndex
    }
    function appendProcessRow() {
        processTableModel.appendRow({ name: "", value: "" })
        let originals = processRowOriginals.slice(0)
        originals.push(null)
        processRowOriginals = originals
        activeProcessRow = processTableModel.rowCount - 1
    }
    function removeProcessRow() {
        let index = activeProcessRow < 0 ? processTableModel.rowCount - 1 : activeProcessRow
        if (index >= 0 && index < processTableModel.rowCount) {
            processTableModel.removeRow(index)
            let originals = processRowOriginals.slice(0)
            if (index < originals.length) originals.splice(index, 1)
            processRowOriginals = originals
        }
        if (processTableModel.rowCount === 0) processTableModel.appendRow({ name: "", value: "" })
        activeProcessRow = Math.min(index, processTableModel.rowCount - 1)
    }
    function loadImportedNTable(text) {
        nTableModel.clear()
        let lines = String(text || "").split(/\r?\n/)
        for (let line of lines) {
            if (!line.trim()) continue
            let cells = line.trim().split(/[\t,\s]+/)
            if (cells.length && /^(wavelength|wave|lambda|波长)/i.test(cells[0])) continue
            if (cells.length < 2 || cells.length > 3) continue
            nTableModel.appendRow({ wavelength: cells[0], n: cells[1], k: cells.length > 2 ? cells[2] : "" })
        }
        if (nTableModel.rowCount === 0) {
            nTableModel.appendRow({ wavelength: "", n: "", k: "" })
            nTableModel.appendRow({ wavelength: "", n: "", k: "" })
        }
        activeNRow = 0
    }
    function gridRows(model, kOnly) {
        let rows = []
        for (let i = 0; i < model.rowCount; ++i) rows.push(gridRow(model, i, kOnly))
        return rows
    }
    function setGridCell(model, rowIndex, columnIndex, value, kOnly) {
        let row = model.getRow(rowIndex)
        let keys = kOnly ? ["wavelength", "k"] : ["wavelength", "n", "k"]
        row[keys[columnIndex]] = String(value)
        model.setRow(rowIndex, row)
        if (kOnly) activeKRow = rowIndex
        else activeNRow = rowIndex
    }
    function appendGridRow(model, kOnly) {
        model.appendRow(kOnly ? { wavelength: "", k: "" } : { wavelength: "", n: "", k: "" })
        if (kOnly) activeKRow = model.rowCount - 1
        else activeNRow = model.rowCount - 1
    }
    function removeGridRow(model, kOnly) {
        let index = kOnly ? activeKRow : activeNRow
        if (index < 0 || index >= model.rowCount) index = model.rowCount - 1
        if (index >= 0) model.removeRow(index)
        if (kOnly) activeKRow = Math.min(index, model.rowCount - 1)
        else activeNRow = Math.min(index, model.rowCount - 1)
    }
    function pasteGrid(model, startRow, startColumn, kOnly) {
        let contents = editor.library.clipboardText()
        if (!contents) { tableNotice = "剪贴板没有可粘贴的表格文本。"; return false }
        let lines = contents.replace(/\r/g, "").split("\n")
        while (lines.length && !lines[lines.length - 1].length) lines.pop()
        let cells = lines.map(line => line.split("\t"))
        if (cells.length && cells[0].length && /^(wavelength|wave|lambda|波长)/i.test(cells[0][0].trim())) cells.shift()
        let width = kOnly ? 2 : 3
        if (!cells.length || cells.some(line => line.length === 0 || startColumn + line.length > width)) {
            tableNotice = "粘贴区域超出表格列数；未应用本次粘贴。"
            return false
        }
        let keys = kOnly ? ["wavelength", "k"] : ["wavelength", "n", "k"]
        for (let r = 0; r < cells.length; ++r) {
            let targetRow = startRow + r
            while (model.rowCount <= targetRow)
                model.appendRow(kOnly ? { wavelength: "", k: "" } : { wavelength: "", n: "", k: "" })
            let data = model.getRow(targetRow)
            for (let c = 0; c < cells[r].length; ++c) data[keys[startColumn + c]] = cells[r][c].trim()
            model.setRow(targetRow, data)
        }
        if (kOnly) activeKRow = Math.min(model.rowCount - 1, startRow + cells.length - 1)
        else activeNRow = Math.min(model.rowCount - 1, startRow + cells.length - 1)
        tableNotice = "已粘贴 " + cells.length + " 行到当前材料表格；保存材料前可继续修改。"
        return true
    }
    function beginNew() {
        editingId = ""
        nameError = ""
        datasetSource = "用户输入，未独立验证"
        existingKData = ({})
        datasetName.text = ""
        datasetNotes.text = ""
        fillProcessRows([])
        modelType.currentIndex = 0
        loadedModelIndex = 0
        unit.currentIndex = 1
        wavelengthLow.text = ""
        wavelengthHigh.text = ""
        fillNRows(null)
        coefficientValues = []
        parameterValues = ({})
        independentK.checked = false
        kUnit.currentIndex = 1
        kLow.text = ""
        kHigh.text = ""
        fillKRows(null)
        kNotes.text = ""
        editorStatus.text = ""
        tableNotice = ""
        open()
    }
    function beginEdit(identity) {
        let record = library.opticalSetDetails(identity)
        if (!record || !record.id) return
        editingId = identity
        nameError = ""
        datasetSource = record.source || ""
        existingKData = record.k_data || ({})
        datasetName.text = record.name
        datasetNotes.text = record.notes || ""
        fillProcessRows(record.process_parameters || [])
        modelType.currentIndex = findSpecIndex(record)
        loadedModelIndex = modelType.currentIndex
        unit.currentIndex = record.wavelength_unit === "nm" ? 1 : 2
        wavelengthLow.text = String(record.wavelength_min)
        wavelengthHigh.text = String(record.wavelength_max)
        fillNRows(record.kind === "tabulated" ? record : null)
        coefficientValues = record.coefficients || []
        parameterValues = record.parameters || ({})
        independentK.checked = !!record.k_data
        if (record.k_data) {
            kUnit.currentIndex = record.k_data.wavelength_unit === "nm" ? 1 : 2
            kLow.text = String(record.k_data.wavelength_min)
            kHigh.text = String(record.k_data.wavelength_max)
            fillKRows(record.k_data)
            kNotes.text = record.k_data.notes || ""
        } else {
            kUnit.currentIndex = 1
            kLow.text = ""
            kHigh.text = ""
            fillKRows(null)
            kNotes.text = ""
        }
        editorStatus.text = ""
        tableNotice = ""
        open()
    }
    function coefficientValue(index) {
        return index < coefficientValues.length ? String(coefficientValues[index]) : ""
    }
    function setCoefficient(index, value) {
        let copy = coefficientValues.slice(0)
        while (copy.length <= index) copy.push("")
        copy[index] = value
        coefficientValues = copy
    }
    function parameterValue(name) {
        return parameterValues[name] === undefined ? "" : String(parameterValues[name])
    }
    function setParameter(name, value) {
        let copy = Object.assign({}, parameterValues)
        copy[name] = value
        parameterValues = copy
    }
    function formPayload() {
        let spec = selectedSpec()
        let value = {
            id: editingId,
            name: datasetName.text,
            notes: datasetNotes.text,
            process_parameters: processRows(),
            source: datasetSource,
            kind: spec.kind,
            wavelength_unit: unit.currentText,
            wavelength_min: wavelengthLow.text,
            wavelength_max: wavelengthHigh.text
        }
        if (spec.kind === "tabulated") {
            value.table_rows = gridRows(nTableModel, false)
        } else if (spec.kind === "rii_formula") {
            value.formula_number = spec.formula_number
            value.coefficients = coefficientValues.slice(0)
            value.has_independent_k = independentK.checked
            if (independentK.checked) {
                value.k_rows = gridRows(kTableModel, true)
                value.k_wavelength_unit = kUnit.currentText
                value.k_wavelength_min = kLow.text
                value.k_wavelength_max = kHigh.text
                value.k_notes = kNotes.text
            }
        } else {
            value.parameters = Object.assign({}, parameterValues)
            value.has_independent_k = independentK.checked
            if (independentK.checked) {
                value.k_rows = gridRows(kTableModel, true)
                value.k_wavelength_unit = kUnit.currentText
                value.k_wavelength_min = kLow.text
                value.k_wavelength_max = kHigh.text
                value.k_notes = kNotes.text
            }
        }
        return JSON.stringify(value)
    }

    FileDialogs.FileDialog {
        id: tableFilePicker
        title: "导入 UTF-8 光学文本"
        nameFilters: ["光学数据 (*.txt *.csv *.tsv *.nk)", "所有文件 (*)"]
        onAccepted: {
            let parsed = JSON.parse(editor.library.previewOpticalFile(selectedFile.toString(), datasetName.text, unit.currentText))
            if (parsed.error) {
                editorStatus.text = "导入失败：" + parsed.error
            } else {
                datasetName.text = parsed.name
                datasetSource = parsed.source
                wavelengthLow.text = String(parsed.wavelength_min)
                wavelengthHigh.text = String(parsed.wavelength_max)
                editor.loadImportedNTable(parsed.wavelengths_text)
                modelType.currentIndex = 0
                editorStatus.text = "已载入文件预览；点击“应用到当前材料”后才会修改材料内容。"
            }
        }
    }

    contentItem: ScrollView {
        id: materialEditorScroll
        clip: true
        objectName: "materialEditorScrollView"
        contentWidth: availableWidth
        ColumnLayout {
            width: parent.width
            spacing: 10
            ThemeControls.ThemeButton {
                objectName: "opticalEditorInversionButton"
                visible: editor.editingId.length === 0
                text: "从实测 R/T 反演 n、k…"
                enabled: !editor.library.readOnly && !editor.library.loading
                onClicked: editor.openInversion()
            }
            ThemeControls.ThemeField {
                id: datasetName
                objectName: "opticalEditorName"
                Layout.fillWidth: true
                placeholderText: "数据集名称"
                onTextEdited: editor.nameError = ""
            }
            ThemeControls.ThemeLabel {
                objectName: "opticalEditorNameError"
                Layout.fillWidth: true
                visible: editor.nameError.length > 0
                text: editor.nameError
                color: editor.palette.brightText
                wrapMode: Text.Wrap
            }
            ThemeControls.ThemeLabel { text: "数据集备注 / 样品说明" }
            TextArea {
                id: datasetNotes
                objectName: "opticalEditorNotes"
                Layout.fillWidth: true
                Layout.preferredHeight: 72
                wrapMode: TextEdit.Wrap
                placeholderText: "可写样品、相态、来源条件与使用限制；保留未提供 / 不适用的原始状态。"
            }
            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeLabel { text: "光学模型" }
            ThemeControls.ThemeComboBox {
                    id: modelType
                    objectName: "opticalEditorModel"
                    Layout.fillWidth: true
                    model: editor.library.opticalModelCatalog
                    textRole: "label"
                    onActivated: {
                        if (currentIndex !== editor.loadedModelIndex) {
                            editor.coefficientValues = []
                            editor.parameterValues = ({})
                            editor.loadedModelIndex = currentIndex
                        }
                    }
                }
            }
            ThemeControls.ThemeLabel {
                visible: editor.blocksIndependentKLoss
                Layout.fillWidth: true
                text: "此数据集含独立 k 表，不能切换为离散模型并丢弃该表。请保留色散模型，或先显式取消独立 k。"
                color: Theme.snapshot.colorWarning
                wrapMode: Text.Wrap
            }
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                text: {
                    let spec = editor.selectedSpec()
                    if (!spec) return ""
                    if (spec.kind === "rii_formula") return spec.label + "；系数波长单位固定为 µm，数据集有效域单位单独选择。"
                    if (spec.kind === "sellmeier") return "Sellmeier 参数按当前波长单位填写；Ci 是共振波长的平方。"
                    if (spec.kind === "cauchy") return "Cauchy 参数按当前波长单位填写。"
                    return "每行 wavelength,n 或 wavelength,n,k；CSV、TSV、空白分列均可。两列保持 k 未知，不作为 0。"
                }
                opacity: 0.8
            }
            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeLabel { text: "有效波段" }
                ThemeControls.ThemeField { id: wavelengthLow; objectName: "opticalEditorRangeMin"; Layout.fillWidth: true; placeholderText: "下限" }
                ThemeControls.ThemeField { id: wavelengthHigh; objectName: "opticalEditorRangeMax"; Layout.fillWidth: true; placeholderText: "上限" }
                ThemeControls.ThemeComboBox { id: unit; objectName: "opticalEditorUnit"; model: ["选择单位", "nm", "um"]; Layout.preferredWidth: 130 }
            }
            ThemeControls.ThemeLabel { Layout.fillWidth: true; wrapMode: Text.Wrap; text: "波段不会自动换算。RII 系数按 µm 解读；表格波长和有效波段按上方单位解读。"; opacity: 0.75 }
            RowLayout {
                objectName: "opticalEditorNTableHeader"
                visible: editor.selectedSpec() && editor.selectedSpec().kind === "tabulated"
                Layout.fillWidth: true
                ThemeControls.ThemeLabel { text: "离散 n/k 表"; font.bold: true }
                Item { Layout.fillWidth: true }
                ThemeControls.ThemeButton { objectName: "opticalEditorImportButton"; text: "导入 TXT / CSV / TSV"; enabled: unit.currentIndex > 0; onClicked: tableFilePicker.open() }
            }
            RowLayout {
                visible: editor.selectedSpec() && editor.selectedSpec().kind === "tabulated"
                Layout.fillWidth: true
                spacing: 1
                ThemeControls.ThemeLabel { objectName: "opticalEditorNHeaderCell_0"; text: "波长（" + unit.currentText + "）"; Layout.preferredWidth: 148; Layout.minimumWidth: 148; Layout.maximumWidth: 148; horizontalAlignment: Text.AlignHCenter }
                ThemeControls.ThemeLabel { objectName: "opticalEditorNHeaderCell_1"; text: "n"; Layout.preferredWidth: 118; Layout.minimumWidth: 118; Layout.maximumWidth: 118; horizontalAlignment: Text.AlignHCenter }
                ThemeControls.ThemeLabel { objectName: "opticalEditorNHeaderCell_2"; text: "k（未知可留空）"; Layout.preferredWidth: 118; Layout.minimumWidth: 118; Layout.maximumWidth: 118; horizontalAlignment: Text.AlignHCenter }
            }
            RowLayout {
                objectName: "opticalEditorNTableRow"
                visible: editor.selectedSpec() && editor.selectedSpec().kind === "tabulated"
                Layout.fillWidth: true
                spacing: 0
                TableView {
                    id: nTable
                    objectName: "opticalEditorTable"
                    visible: editor.selectedSpec() && editor.selectedSpec().kind === "tabulated"
                    Layout.alignment: Qt.AlignLeft
                    Layout.preferredWidth: 386
                    Layout.minimumWidth: 386
                    Layout.maximumWidth: 386
                    Layout.preferredHeight: Math.min(216, Math.max(76, nTableModel.rowCount * 36 + 2))
                    Layout.maximumHeight: 216
                    clip: true
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                    WheelHandler {
                        objectName: "opticalEditorTableWheelHandler"
                        acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                        orientation: Qt.Vertical
                        blocking: true
                        target: null
                        onWheel: event => editor.scrollTableWithWheel(nTable, event)
                    }
                    model: nTableModel
                    columnSpacing: 1
                    rowSpacing: 1
                    columnWidthProvider: function(column) { return [148, 118, 118][column] }
                    delegate: Rectangle {
                        id: nCellBackground
                        required property int row
                        required property int column
                        required property var display
                        implicitWidth: [148, 118, 118][nCellBackground.column]
                        implicitHeight: 36
                        color: palette.base
                        border.color: palette.mid
                        WheelHandler {
                            objectName: "opticalEditorNCellWheelHandler"
                            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                            orientation: Qt.Vertical
                            blocking: true
                            target: null
                            onWheel: event => editor.scrollTableWithWheel(nTable, event)
                        }
                        ThemeControls.ThemeField {
                            id: nCell
                            accessibleLabel: "n/k 数据第 " + (nCellBackground.row + 1) + " 行"
                            objectName: "opticalEditorCell_" + nCellBackground.row + "_" + nCellBackground.column
                            anchors.fill: parent
                            anchors.margins: 1
                            numericDisplayValue: nCellBackground.display
                            selectByMouse: true
                            horizontalAlignment: Text.AlignHCenter
                            onActiveFocusChanged: {
                                editor.nGridFocused = activeFocus
                                if (activeFocus) {
                                    editor.activeNRow = nCellBackground.row
                                    editor.activeNColumn = nCellBackground.column
                                }
                            }
                            onTextEdited: editor.setGridCell(nTableModel, nCellBackground.row, nCellBackground.column, text, false)
                            Keys.priority: Keys.BeforeItem
                            Keys.onPressed: function(event) {
                                if ((event.modifiers & Qt.ControlModifier) && event.key === Qt.Key_V) {
                                    event.accepted = editor.pasteGrid(nTableModel, nCellBackground.row, nCellBackground.column, false)
                                }
                            }
                        }
                    }
                }
                    Item {
                    objectName: "opticalEditorNTableBlankArea"
                    Layout.fillWidth: true
                    Layout.preferredHeight: nTable.height
                    WheelHandler {
                        objectName: "opticalEditorNTableBlankWheelHandler"
                        acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                        orientation: Qt.Vertical
                        blocking: false
                        target: null
                        onWheel: event => editor.scrollOuterFormWithWheel(event)
                    }
                }
            }
            RowLayout {
                visible: editor.selectedSpec() && editor.selectedSpec().kind === "tabulated"
                Layout.fillWidth: true
                ThemeControls.ThemeButton { objectName: "opticalEditorAddRow"; text: "添加行"; onClicked: editor.appendGridRow(nTableModel, false) }
                ThemeControls.ThemeButton { objectName: "opticalEditorDeleteRow"; text: "删除选中行"; enabled: nTableModel.rowCount > 0; onClicked: editor.removeGridRow(nTableModel, false) }
                ThemeControls.ThemeLabel { Layout.fillWidth: true; wrapMode: Text.Wrap; text: editor.tableNotice; color: editor.tableNotice.startsWith("已粘贴") ? Theme.snapshot.colorSuccess : Theme.snapshot.colorError }
            }
            GridLayout {
                id: opticalParameterGrid
                objectName: "opticalEditorParameterGrid"
                visible: editor.selectedSpec() && editor.selectedSpec().kind !== "tabulated"
                columns: 3
                columnSpacing: 8
                rowSpacing: 6
                Layout.fillWidth: true
                Repeater {
                    id: coefficientRepeater
                    model: editor.selectedSpec() && editor.selectedSpec().kind === "rii_formula" ? editor.selectedSpec().coefficient_names : []
                    delegate: RowLayout {
                        required property var modelData
                        required property int index
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        spacing: 6
                        ThemeControls.ThemeLabel { objectName: "opticalEditorParameterLabel_" + modelData; Layout.preferredWidth: 26; Layout.minimumWidth: 20; text: modelData }
                        ThemeControls.ThemeField {
                            id: coefficientInput
                            objectName: "opticalEditorCoefficient_" + index
                            accessibleLabel: modelData
                            Layout.fillWidth: true
                            Layout.minimumWidth: 64
                            numericDisplayValue: editor.coefficientValue(index)
                            placeholderText: "系数"
                            onTextEdited: editor.setCoefficient(index, text)
                        }
                    }
                }
                Repeater {
                    id: opticalParameterRepeater
                    objectName: "opticalEditorParameterRepeater"
                    model: {
                        let spec = editor.selectedSpec()
                        return spec && (spec.kind === "cauchy" || spec.kind === "sellmeier") ? spec.coefficient_names : []
                    }
                    delegate: RowLayout {
                        required property var modelData
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        spacing: 6
                        ThemeControls.ThemeLabel { objectName: "opticalEditorParameterLabel_" + modelData; Layout.preferredWidth: 26; Layout.minimumWidth: 20; text: modelData }
                        ThemeControls.ThemeField {
                            id: parameterInput
                            objectName: "opticalEditorParameter_" + modelData
                            accessibleLabel: modelData
                            Layout.fillWidth: true
                            Layout.minimumWidth: 64
                            numericDisplayValue: editor.parameterValue(modelData)
                            placeholderText: "系数"
                            onTextEdited: editor.setParameter(modelData, text)
                        }
                    }
                }
            }
            ColumnLayout {
                objectName: "opticalEditorEquationPreview"
                visible: editor.selectedSpec() && editor.selectedSpec().kind !== "tabulated"
                Layout.fillWidth: true
                spacing: 4
                Image {
                    id: equationImage
                    objectName: "opticalEditorEquationImage"
                    property string formulaColorDebug: editor.formulaSvgTextColor()
                    property string renderState: status === Image.Ready ? "ready"
                        : status === Image.Error ? "error"
                        : status === Image.Loading ? "loading" : "null"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 78
                    fillMode: Image.PreserveAspectFit
                    source: {
                        let spec = editor.selectedSpec()
                        return spec ? editor.library.opticalFormulaSvg(
                            spec.kind, spec.formula_number || 0, editor.formulaSvgTextColor()) : ""
                    }
                }
                ThemeControls.ThemeLabel {
                    visible: equationImage.status === Image.Null || equationImage.status === Image.Error
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: "公式预览暂不可用：核心模型目录尚未提供可渲染的 formula_latex。"
                    opacity: 0.8
                }
                ThemeControls.ThemeLabel {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: {
                        let spec = editor.selectedSpec()
                        if (!spec) return ""
                        let names = spec.coefficient_names || []
                        let values = []
                        for (let i = 0; i < names.length; ++i) {
                            let current = spec.kind === "rii_formula" ? editor.coefficientValue(i) : editor.parameterValue(names[i])
                            values.push(names[i] + " = " + (current.trim() || "—"))
                        }
                        return values.join("；")
                    }
                    textFormat: Text.PlainText
                }
                ThemeControls.ThemeLabel {
                    objectName: "opticalEditorFormulaWavelengthUnit"
                    Layout.fillWidth: true
                    text: {
                        let spec = editor.selectedSpec()
                        if (!spec) return ""
                        let waveUnit = spec.formula_wavelength_unit || spec.coefficient_wavelength_unit
                        if (waveUnit === "wavelength_unit") waveUnit = unit.currentText
                        return "公式中的 λ 单位：" + waveUnit + (spec.quantity === "n2" ? "；输出 n²" : "；输出 n")
                    }
                }
            }
            ThemeControls.ThemeLabel { text: "工艺参数（无记录的字段留空，不从备注推断）"; font.bold: true }
            RowLayout {
                objectName: "opticalEditorProcessTableHeader"
                Layout.fillWidth: true
                spacing: 1
                ThemeControls.ThemeLabel { objectName: "opticalEditorProcessHeaderCell_0"; text: "项目"; Layout.preferredWidth: editor.processColumnWidths[0]; Layout.minimumWidth: editor.processColumnWidths[0]; Layout.maximumWidth: editor.processColumnWidths[0]; horizontalAlignment: Text.AlignLeft }
                ThemeControls.ThemeLabel { objectName: "opticalEditorProcessHeaderCell_1"; text: "值（含单位）"; Layout.preferredWidth: editor.processColumnWidths[1]; Layout.minimumWidth: editor.processColumnWidths[1]; Layout.maximumWidth: editor.processColumnWidths[1]; horizontalAlignment: Text.AlignLeft }
            }
            RowLayout {
                objectName: "opticalEditorProcessTableRow"
                Layout.fillWidth: true
                spacing: 0
                TableView {
                    id: processTable
                    objectName: "opticalEditorProcessTable"
                    Layout.alignment: Qt.AlignLeft
                    Layout.preferredWidth: editor.processColumnWidths[0] + editor.processColumnWidths[1] + 1
                    Layout.minimumWidth: editor.processColumnWidths[0] + editor.processColumnWidths[1] + 1
                    Layout.maximumWidth: editor.processColumnWidths[0] + editor.processColumnWidths[1] + 1
                    Layout.preferredHeight: Math.min(216, Math.max(76, processTableModel.rowCount * 36 + 2))
                    Layout.maximumHeight: 216
                    clip: true
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                    WheelHandler {
                        objectName: "opticalEditorProcessTableWheelHandler"
                        acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                        orientation: Qt.Vertical
                        blocking: true
                        target: null
                        onWheel: event => editor.scrollTableWithWheel(processTable, event)
                    }
                    model: processTableModel
                    columnSpacing: 1
                    rowSpacing: 1
                    columnWidthProvider: function(column) { return editor.processColumnWidths[column] }
                    delegate: Rectangle {
                        id: processCellBackground
                        required property int row
                        required property int column
                        required property var display
                        implicitWidth: editor.processColumnWidths[processCellBackground.column]
                        implicitHeight: 36
                        color: palette.base
                        border.color: palette.mid
                        WheelHandler {
                            objectName: "opticalEditorProcessCellWheelHandler"
                            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                            orientation: Qt.Vertical
                            blocking: true
                            target: null
                            onWheel: event => editor.scrollTableWithWheel(processTable, event)
                        }
                        ThemeControls.ThemeField {
                            objectName: "opticalEditorProcessCell_" + processCellBackground.row + "_" + processCellBackground.column
                            anchors.fill: parent
                            anchors.margins: 1
                            text: String(processCellBackground.display ?? "")
                            selectByMouse: true
                            accessibleLabel: ["项目", "值（含单位）"][processCellBackground.column] + "，第 " + (processCellBackground.row + 1) + " 行"
                            onActiveFocusChanged: if (activeFocus) editor.activeProcessRow = processCellBackground.row
                            onTextEdited: editor.setProcessCell(processCellBackground.row, processCellBackground.column, text)
                        }
                    }
                }
                    Item {
                    objectName: "opticalEditorProcessTableBlankArea"
                    Layout.fillWidth: true
                    Layout.preferredHeight: processTable.height
                    WheelHandler {
                        objectName: "opticalEditorProcessTableBlankWheelHandler"
                        acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                        orientation: Qt.Vertical
                        blocking: false
                        target: null
                        onWheel: event => editor.scrollOuterFormWithWheel(event)
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeButton { objectName: "opticalEditorAddProcessRow"; text: "添加参数行"; onClicked: editor.appendProcessRow() }
                ThemeControls.ThemeButton { objectName: "opticalEditorDeleteProcessRow"; text: "删除当前行"; enabled: processTableModel.rowCount > 0; onClicked: editor.removeProcessRow() }
            }
            CheckBox {
                id: independentK
                objectName: "opticalEditorIndependentK"
                visible: editor.selectedSpec() && editor.selectedSpec().kind !== "tabulated"
                text: "另有独立 k 数据表"
            }
            ColumnLayout {
                visible: independentK.visible && independentK.checked
                Layout.fillWidth: true
                ThemeControls.ThemeLabel { text: "独立 k 的波段、单位与数据来源各自保留；数据域以外的 k 仍为未知。"; wrapMode: Text.Wrap; Layout.fillWidth: true }
                RowLayout {
                    objectName: "opticalEditorKTableHeader"
                    Layout.fillWidth: true
                    ThemeControls.ThemeLabel { text: "k 有效波段" }
                    ThemeControls.ThemeField { id: kLow; objectName: "opticalEditorKRangeMin"; Layout.fillWidth: true; placeholderText: "下限" }
                    ThemeControls.ThemeField { id: kHigh; objectName: "opticalEditorKRangeMax"; Layout.fillWidth: true; placeholderText: "上限" }
                    ThemeControls.ThemeComboBox { id: kUnit; objectName: "opticalEditorKUnit"; model: ["选择单位", "nm", "um"]; Layout.preferredWidth: 130 }
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 1
                    ThemeControls.ThemeLabel { objectName: "opticalEditorKHeaderCell_0"; text: "波长（" + kUnit.currentText + "）"; Layout.preferredWidth: 160; Layout.minimumWidth: 160; Layout.maximumWidth: 160; horizontalAlignment: Text.AlignHCenter }
                    ThemeControls.ThemeLabel { objectName: "opticalEditorKHeaderCell_1"; text: "k"; Layout.preferredWidth: 120; Layout.minimumWidth: 120; Layout.maximumWidth: 120; horizontalAlignment: Text.AlignHCenter }
                }
                RowLayout {
                    objectName: "opticalEditorKTableRow"
                    Layout.fillWidth: true
                    spacing: 0
                    TableView {
                        id: kTable
                        objectName: "opticalEditorKTable"
                        Layout.alignment: Qt.AlignLeft
                        Layout.preferredWidth: 281
                        Layout.minimumWidth: 281
                        Layout.maximumWidth: 281
                        Layout.preferredHeight: Math.min(216, Math.max(76, kTableModel.rowCount * 36 + 2))
                        Layout.maximumHeight: 216
                        clip: true
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                        WheelHandler {
                            objectName: "opticalEditorKTableWheelHandler"
                            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                            orientation: Qt.Vertical
                            blocking: true
                            target: null
                            onWheel: event => editor.scrollTableWithWheel(kTable, event)
                        }
                        model: kTableModel
                        columnSpacing: 1
                        rowSpacing: 1
                        columnWidthProvider: function(column) { return [160, 120][column] }
                        delegate: Rectangle {
                            id: kCellBackground
                            required property int row
                            required property int column
                            required property var display
                            implicitWidth: [160, 120][kCellBackground.column]
                            implicitHeight: 36
                            color: palette.base
                            border.color: palette.mid
                            WheelHandler {
                                objectName: "opticalEditorKCellWheelHandler"
                                acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                                orientation: Qt.Vertical
                                blocking: true
                                target: null
                                onWheel: event => editor.scrollTableWithWheel(kTable, event)
                            }
                            ThemeControls.ThemeField {
                                id: kCell
                                accessibleLabel: "独立 k 数据第 " + (kCellBackground.row + 1) + " 行"
                                objectName: "opticalEditorKCell_" + kCellBackground.row + "_" + kCellBackground.column
                                anchors.fill: parent
                                anchors.margins: 1
                                numericDisplayValue: kCellBackground.display
                                selectByMouse: true
                                horizontalAlignment: Text.AlignHCenter
                                onActiveFocusChanged: {
                                    editor.kGridFocused = activeFocus
                                    if (activeFocus) {
                                        editor.activeKRow = kCellBackground.row
                                        editor.activeKColumn = kCellBackground.column
                                    }
                                }
                                onTextEdited: editor.setGridCell(kTableModel, kCellBackground.row, kCellBackground.column, text, true)
                                Keys.priority: Keys.BeforeItem
                                Keys.onPressed: function(event) {
                                    if ((event.modifiers & Qt.ControlModifier) && event.key === Qt.Key_V) {
                                        event.accepted = editor.pasteGrid(kTableModel, kCellBackground.row, kCellBackground.column, true)
                                    }
                                }
                            }
                        }
                    }
                        Item {
                        objectName: "opticalEditorKTableBlankArea"
                        Layout.fillWidth: true
                        Layout.preferredHeight: kTable.height
                        WheelHandler {
                            objectName: "opticalEditorKTableBlankWheelHandler"
                            acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                            orientation: Qt.Vertical
                            blocking: false
                            target: null
                            onWheel: event => editor.scrollOuterFormWithWheel(event)
                        }
                    }
                }
                RowLayout {
                    Layout.fillWidth: true
                    ThemeControls.ThemeButton { objectName: "opticalEditorKAddRow"; text: "添加 k 行"; onClicked: editor.appendGridRow(kTableModel, true) }
                    ThemeControls.ThemeButton { objectName: "opticalEditorKDeleteRow"; text: "删除选中行"; enabled: kTableModel.rowCount > 0; onClicked: editor.removeGridRow(kTableModel, true) }
                    ThemeControls.ThemeLabel { Layout.fillWidth: true; wrapMode: Text.Wrap; text: editor.tableNotice; color: editor.tableNotice.startsWith("已粘贴") ? Theme.snapshot.colorSuccess : Theme.snapshot.colorError }
                }
                ThemeControls.ThemeLabel { text: "独立 k 备注" }
                TextArea { id: kNotes; objectName: "opticalEditorKNotes"; Layout.fillWidth: true; Layout.preferredHeight: 54; wrapMode: TextEdit.Wrap }
            }
            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeLabel { Layout.fillWidth: true; wrapMode: Text.Wrap; text: "数据集来源及原始引用收纳在“材料参考详情”。材料保存只更新用户材料文件；内置参考保持只读。"; opacity: 0.8 }
                ThemeControls.ThemeButton { text: "材料参考详情"; enabled: editor.library.hasReferenceData; onClicked: editor.referenceRequested() }
            }
            ThemeControls.ThemeLabel { id: editorStatus; objectName: "opticalEditorStatus"; Layout.fillWidth: true; wrapMode: Text.Wrap; color: editorStatus.text.startsWith("导入失败：") ? Theme.snapshot.colorError : Theme.snapshot.colorTextMuted }
        }
    }
    footer: DialogButtonBox {
        ThemeControls.ThemeButton {
            objectName: "opticalEditorSaveButton"
            text: "应用到当前材料"
            enabled: !editor.library.readOnly && !editor.library.loading
                && !editor.blocksIndependentKLoss
            DialogButtonBox.buttonRole: DialogButtonBox.ApplyRole
            onClicked: {
                if (!datasetName.text.trim().length) {
                    editor.nameError = "请填写光学数据集名称"
                    editorStatus.text = editor.nameError
                    const flickable = materialEditorScroll.contentItem as Flickable
                    if (flickable)
                        flickable.contentY = 0
                    datasetName.forceActiveFocus()
                    return
                }
                editor.nameError = ""
                let json = editor.formPayload()
                let ok = editor.editingId.length ? editor.library.updateOpticalSet(editor.editingId, json) : editor.library.addOpticalSet(json)
                if (ok) editor.close()
                else editorStatus.text = editor.library.status
            }
        }
        ThemeControls.ThemeButton { objectName: "opticalEditorCancelButton"; text: "取消"; DialogButtonBox.buttonRole: DialogButtonBox.RejectRole; onClicked: editor.close() }
    }
}
