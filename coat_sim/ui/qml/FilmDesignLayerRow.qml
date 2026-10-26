pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

Rectangle {
    id: row
    objectName: "filmDesignLayerRow_" + row.stableId
    property var controller
    property string side: "front"
    required property var columnMetrics
    required property int index
    required property string stableId
    required property string symbol
    required property string materialId
    required property string opticalSetId
    required property var physicalNmValue
    required property string physicalNmText
    required property string qwText
    required property string derivedReason
    required property string note // retained in the model/schema; not shown in the compact editor
    property bool symbolRow: false
    // TextField's padded text content and the theme's medium control size can
    // exceed controlHeightSm at scaled fonts. Keep the delegate and all cells
    // at the tallest child's actual implicit height instead of clipping text.
    readonly property real controlContentHeight: Math.max(
        Theme.snapshot.controlHeightSm, layerNumber.implicitHeight,
        symbolInput.implicitHeight, materialBox.implicitHeight, setBox.implicitHeight,
        opticalThicknessInput.implicitHeight, physicalThicknessInput.implicitHeight,
        insertAfterButton.implicitHeight, removeButton.implicitHeight)
    signal removeRequested(string stableId)
    signal insertRequested(string stableId)
    signal columnFocusRequested(string columnName, int rowDelta)

    implicitHeight: row.controlContentHeight + Theme.snapshot.spaceXs * 2
    radius: Theme.snapshot.radiusSm
    color: Theme.snapshot.colorSurface
    border.color: Theme.snapshot.colorBorder
    border.width: Theme.snapshot.borderWidth

    function indexFor(options, value) {
        for (let i = 0; i < options.length; ++i)
            if (String(options[i].id || "") === String(value || "")) return i
        return 0
    }
    function selectedOptionId(options, index) {
        if (!options || index < 0 || index >= options.length) return ""
        return String(options[index].id || "")
    }
    function wrapTooltipText(value) {
        const characters = Array.from(String(value || ""))
        const lines = []
        for (let offset = 0; offset < characters.length; offset += 32)
            lines.push(characters.slice(offset, offset + 32).join(""))
        return lines.join("\n")
    }
    function datasetTooltip(option) {
        const parts = []
        if (option.wavelength_min !== undefined && option.wavelength_max !== undefined)
            parts.push("波段：" + option.wavelength_min + "–" + option.wavelength_max
                       + " " + (option.wavelength_unit || "nm"))
        if (option.value_range_text)
            parts.push("值范围：" + option.value_range_text)
        const note = String(option.notes || "").replace(/\s+/g, " ").trim()
        if (note) {
            const conciseNote = note.length > 120 ? note.slice(0, 117) + "…" : note
            parts.push(wrapTooltipText(conciseNote))
        }
        return parts.join("\n")
    }
    function focusColumnField(columnName) {
        const target = columnName === "symbol" ? symbolInput
                     : columnName === "qw" ? opticalThicknessInput
                     : columnName === "thickness" ? physicalThicknessInput : null
        if (!target || !target.visible || !target.enabled)
            return false
        target.focusAndSelectAll()
        return true
    }
    function commitMaterialSelection(index) {
        const selectedId = row.selectedOptionId(materialBox.model, index)
        if (selectedId !== row.materialId) {
            return row.controller.setLayerValue(row.side, row.stableId, "material_id", selectedId)
        }
        return true
    }
    function commitDatasetSelection(index) {
        const selectedId = row.selectedOptionId(setBox.model, index)
        if (selectedId !== row.opticalSetId)
            row.controller.setLayerValue(row.side, row.stableId, "optical_set_id", selectedId)
    }
    function syncMaterialIndex() {
        materialBox.currentIndex = row.indexFor(materialBox.model, row.materialId)
    }
    function syncDatasetIndex() {
        setBox.currentIndex = row.indexFor(setBox.model, row.opticalSetId)
    }
    onMaterialIdChanged: {
        // ListView reuses delegates while scrolling and replacing a stack.
        // The material catalog can stay unchanged as this row's material ID
        // changes, so refresh both selectors from their current model roles.
        syncMaterialIndex()
        syncDatasetIndex()
    }
    onOpticalSetIdChanged: syncDatasetIndex()
    function commitOpticalThickness() {
        if (!opticalThicknessInput.edited) return
        const parsed = Number(opticalThicknessInput.text.trim())
        if (opticalThicknessInput.text.trim().length === 0 || !isFinite(parsed) || parsed < 0) {
            opticalThicknessInput.text = row.qwText
            opticalThicknessInput.edited = false
            return
        }
        if (!row.controller.setLayerValue(row.side, row.stableId, "thickness.qw", parsed))
            opticalThicknessInput.text = row.qwText
        opticalThicknessInput.edited = false
    }
    function commitPhysicalThickness() {
        if (!physicalThicknessInput.edited) return
        const parsed = Number(physicalThicknessInput.text.trim())
        if (physicalThicknessInput.text.trim().length === 0 || !isFinite(parsed) || parsed < 0) {
            physicalThicknessInput.text = row.physicalNmText
            physicalThicknessInput.edited = false
            return
        }
        if (!row.controller.setLayerValue(row.side, row.stableId, "thickness.nm", parsed))
            physicalThicknessInput.text = row.physicalNmText
        physicalThicknessInput.edited = false
    }
    RowLayout {
        anchors.fill: parent
        anchors.margins: row.columnMetrics.horizontalPadding
        spacing: row.columnMetrics.columnGap

        ThemeControls.ThemeField {
            id: symbolInput
            objectName: "filmLayerSymbol_" + row.stableId
            visible: row.symbolRow
            Layout.preferredWidth: row.columnMetrics.indexWidth
            Layout.minimumWidth: row.columnMetrics.indexWidth
            Layout.maximumWidth: row.columnMetrics.indexWidth
            Layout.preferredHeight: row.controlContentHeight
            // This single-letter column is intentionally compact. Keep its
            // text area wide enough at increased theme font/density sizes.
            leftPadding: symbolInput.themeTokens.spaceXs
            rightPadding: symbolInput.themeTokens.spaceXs
            accessibleLabel: "表达式符号 A 到 Z"
            text: row.symbol
            maximumLength: 1
            horizontalAlignment: TextInput.AlignHCenter
            selectByMouse: true
            onTextEdited: row.controller.markEditingDirty()
            onEditingFinished: row.controller.setLayerValue(row.side, row.stableId, "symbol", text)
            Keys.onEscapePressed: function(event) { text = row.symbol; event.accepted = true }
            Keys.onPressed: function(event) {
                handleColumnNavigation(event, function(delta) {
                    row.columnFocusRequested("symbol", delta)
                })
            }
        }
        Label {
            id: layerNumber
            objectName: "filmLayerNumber_" + row.stableId
            visible: !row.symbolRow
            Layout.preferredWidth: row.columnMetrics.indexWidth
            Layout.minimumWidth: row.columnMetrics.indexWidth
            Layout.maximumWidth: row.columnMetrics.indexWidth
            Layout.preferredHeight: row.controlContentHeight
            text: String(row.index + 1)
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            color: Theme.snapshot.colorTextMuted
            Accessible.name: "第" + (row.index + 1) + "层"
        }
        ThemeControls.ThemeMaterialPicker {
            id: materialBox
            objectName: "filmLayerMaterial_" + row.stableId
            Layout.preferredWidth: row.columnMetrics.materialWidth
            Layout.minimumWidth: row.columnMetrics.materialWidth
            Layout.maximumWidth: row.columnMetrics.materialWidth
            Layout.preferredHeight: row.controlContentHeight
            model: row.controller ? row.controller.filmOptions : []
            textRole: "name"
            valueRole: "id"
            duplicateContextLabel: "薄膜材料"
            optionObjectNamePrefix: "filmMaterialOption_"
            Component.onCompleted: row.syncMaterialIndex()
            onModelChanged: row.syncMaterialIndex()
            accessibleLabel: "膜材质"
            placeholderText: "请选择材料"
            onActivated: function(index) {
                if (row.commitMaterialSelection(index)) materialBox.currentIndex = index
                else row.syncMaterialIndex()
            }
        }
        ThemeControls.ThemeComboBox {
            id: setBox
            objectName: "filmLayerOpticalSet_" + row.stableId
            Layout.preferredWidth: row.columnMetrics.datasetWidth
            Layout.minimumWidth: row.columnMetrics.datasetWidth
            Layout.maximumWidth: row.columnMetrics.datasetWidth
            Layout.preferredHeight: row.controlContentHeight
            model: row.controller ? row.controller.opticalSetsForFilm(row.materialId) : []
            textRole: "name"
            valueRole: "id"
            Component.onCompleted: row.syncDatasetIndex()
            onModelChanged: row.syncDatasetIndex()
            onActivated: function(index) { row.commitDatasetSelection(index) }
            delegate: ItemDelegate {
                id: datasetOption
                required property var modelData
                required property int index
                objectName: "filmOpticalSetOption_" + String(modelData.id || "empty")
                readonly property string tooltipText: row.datasetTooltip(modelData)
                width: setBox.width
                text: modelData.name || modelData.id || ""
                font: setBox.font
                hoverEnabled: true
                highlighted: index === setBox.highlightedIndex
                readonly property bool currentSelection: index === setBox.currentIndex
                readonly property bool accentState: datasetOption.enabled
                                                    && (datasetOption.highlighted
                                                        || datasetOption.hovered)
                contentItem: Text {
                    text: datasetOption.text
                    textFormat: Text.PlainText
                    font: datasetOption.font
                    color: !datasetOption.enabled ? setBox.themeTokens.colorDisabledText
                           : datasetOption.accentState ? setBox.themeTokens.colorTextOnAccent
                                                       : setBox.themeTokens.colorText
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
                background: Rectangle {
                    radius: Theme.snapshot.radiusSm
                    color: !datasetOption.enabled
                           ? setBox.themeTokens.colorDisabledSurface
                           : datasetOption.accentState ? setBox.themeTokens.colorAccent
                           : datasetOption.currentSelection ? setBox.themeTokens.colorSelection
                           : setBox.themeTokens.colorSurface
                    border.width: datasetOption.visualFocus ? Theme.snapshot.borderWidth : 0
                    border.color: datasetOption.visualFocus
                                  ? Theme.snapshot.colorFocusRing : "transparent"
                }
                ToolTip {
                    id: datasetTooltipPopup
                    objectName: "filmOpticalSetTooltip_"
                                 + String(datasetOption.modelData.id || datasetOption.index)
                    parent: datasetOption
                    width: Math.min(360, Math.max(220, setBox.width))
                    delay: 650
                    timeout: 3500
                    visible: datasetOption.hovered && !datasetOption.pressed
                             && !!datasetOption.tooltipText
                    text: datasetOption.tooltipText
                    contentItem: Text {
                        text: datasetTooltipPopup.text
                        textFormat: Text.PlainText
                        font: setBox.font
                        color: setBox.themeTokens.colorText
                        wrapMode: Text.Wrap
                    }
                    background: Rectangle {
                        color: setBox.themeTokens.colorSurfaceRaised
                        radius: setBox.themeTokens.radiusSm
                        border.width: setBox.themeTokens.borderWidth
                        border.color: setBox.themeTokens.colorBorderStrong
                    }
                }
            }
        }
        ThemeControls.ThemeField {
            id: opticalThicknessInput
            objectName: "filmLayerQw_" + row.stableId
            Layout.preferredWidth: row.columnMetrics.qwWidth
            Layout.minimumWidth: row.columnMetrics.qwWidth
            Layout.maximumWidth: row.columnMetrics.qwWidth
            Layout.preferredHeight: row.controlContentHeight
            accessibleLabel: "光学厚度，单位 QW"
            accessibleDescription: row.derivedReason
            numericDisplayValue: row.qwText
            numericDisplayDecimals: Theme.filmThicknessDecimals
            selectByMouse: true
            inputMethodHints: Qt.ImhFormattedNumbersOnly
            property bool edited: false
            onTextEdited: { edited = true; row.controller.markEditingDirty() }
            onEditingFinished: row.commitOpticalThickness()
            onActiveFocusChanged: if (!activeFocus) row.commitOpticalThickness()
            ToolTip.visible: opticalHover.hovered && !!row.derivedReason
            ToolTip.text: row.derivedReason || "QW = 4 Re[n(参考波长)] d / 参考波长"
            HoverHandler { id: opticalHover }
            Keys.onEscapePressed: function(event) {
                text = row.qwText
                edited = false
                event.accepted = true
            }
            Keys.onPressed: function(event) {
                handleColumnNavigation(event, function(delta) {
                    row.columnFocusRequested("qw", delta)
                })
            }
        }
        ThemeControls.ThemeField {
            id: physicalThicknessInput
            objectName: "filmLayerNm_" + row.stableId
            Layout.preferredWidth: row.columnMetrics.nmWidth
            Layout.minimumWidth: row.columnMetrics.nmWidth
            Layout.maximumWidth: row.columnMetrics.nmWidth
            Layout.preferredHeight: row.controlContentHeight
            accessibleLabel: "物理厚度，单位 nm"
            accessibleDescription: row.derivedReason
            numericDisplayValue: row.physicalNmValue === null
                                 ? row.physicalNmText : row.physicalNmValue
            numericDisplayDecimals: Theme.filmThicknessDecimals
            selectByMouse: true
            inputMethodHints: Qt.ImhFormattedNumbersOnly
            property bool edited: false
            onTextEdited: { edited = true; row.controller.markEditingDirty() }
            onEditingFinished: row.commitPhysicalThickness()
            onActiveFocusChanged: if (!activeFocus) row.commitPhysicalThickness()
            ToolTip.visible: physicalHover.hovered && !!row.derivedReason
            ToolTip.text: row.derivedReason || "按当前材料 n 与参考波长换算"
            HoverHandler { id: physicalHover }
            Keys.onEscapePressed: function(event) {
                text = row.physicalNmText
                edited = false
                event.accepted = true
            }
            Keys.onPressed: function(event) {
                handleColumnNavigation(event, function(delta) {
                    row.columnFocusRequested("thickness", delta)
                })
            }
        }
        ToolButton {
            id: insertAfterButton
            objectName: "filmDesignInsertAfter_" + row.stableId
            visible: !row.symbolRow
            Layout.preferredWidth: row.columnMetrics.insertWidth
            Layout.minimumWidth: row.columnMetrics.insertWidth
            Layout.maximumWidth: row.columnMetrics.insertWidth
            Layout.preferredHeight: row.controlContentHeight
            text: "+"
            Accessible.name: "在此膜层后插入一层"
            ToolTip.visible: hovered
            ToolTip.text: "在此层后插入膜层"
            onClicked: row.insertRequested(row.stableId)
        }
        Item {
            objectName: "filmDesignInsertPlaceholder_" + row.stableId
            visible: row.symbolRow
            Layout.preferredWidth: row.columnMetrics.insertWidth
            Layout.minimumWidth: row.columnMetrics.insertWidth
            Layout.maximumWidth: row.columnMetrics.insertWidth
            Layout.preferredHeight: row.controlContentHeight
        }
        ToolButton {
            id: removeButton
            objectName: "filmDesignDelete_" + row.stableId
            Layout.preferredWidth: row.columnMetrics.deleteWidth
            Layout.minimumWidth: row.columnMetrics.deleteWidth
            Layout.maximumWidth: row.columnMetrics.deleteWidth
            Layout.preferredHeight: row.controlContentHeight
            text: "×"
            Accessible.name: row.symbolRow ? "删除表达式符号" : "删除膜层"
            ToolTip.visible: hovered
            ToolTip.text: row.symbolRow ? "删除符号" : "删除膜层"
            onClicked: row.removeRequested(row.stableId)
        }
    }

}
