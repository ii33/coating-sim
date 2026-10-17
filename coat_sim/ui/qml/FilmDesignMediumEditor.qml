import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ColumnLayout {
    id: editor
    objectName: "filmDesignMediumEditor_" + editor.mediumName
    property var controller
    required property var themeTokens
    property string mediumName: "incident_medium"
    property var mediumData: ({kind: "constant", n: 1})
    property string heading: "入射介质"

    function indexFor(options, value) {
        for (let i = 0; i < options.length; ++i)
            if (String(options[i].id || "") === String(value || "")) return i
        return -1
    }
    function datasetTooltip(option) {
        const parts = []
        if (option.wavelength_min !== undefined && option.wavelength_max !== undefined)
            parts.push("波段：" + option.wavelength_min + "–" + option.wavelength_max
                       + " " + (option.wavelength_unit || "nm"))
        if (option.value_range_text)
            parts.push("值范围：" + option.value_range_text)
        const note = String(option.notes || "").replace(/\s+/g, " ").trim()
        if (note)
            parts.push(note.length > 120 ? note.slice(0, 117) + "…" : note)
        return parts.join("\n")
    }
    function legacyConstantDescription() {
        let text = "兼容介质常数 n="
                   + String(editor.mediumData.n === undefined ? 1 : editor.mediumData.n)
        if (editor.mediumData.k !== undefined)
            text += ", k=" + String(editor.mediumData.k)
        return text + "；当前值保留在设计文件中，选择材料后才会替换。"
    }

    Layout.fillWidth: true
    Layout.minimumWidth: 0
    enabled: editor.mediumName !== "exit_medium" ||
             (editor.controller && editor.controller.designMeta.substrate.sides === "double")
    spacing: Theme.snapshot.spaceSm

    ThemeControls.ThemeLabel {
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        wrapMode: Text.WordWrap
        text: editor.heading
        font.family: Theme.snapshot.fontFamily
        font.pointSize: Theme.snapshot.fontSizeHeading
        font.weight: Theme.snapshot.fontWeightEmphasis
        color: Theme.snapshot.colorText
    }
    ThemeControls.ThemeLabel {
        Layout.minimumWidth: 0
        objectName: "filmDesignLegacyMedium_" + editor.mediumName
        visible: editor.mediumData.kind !== "material"
        Layout.fillWidth: true
        text: editor.legacyConstantDescription()
        wrapMode: Text.WordWrap
        color: Theme.snapshot.colorTextMuted
    }
    GridLayout {
        id: mediumPickerGrid
        objectName: "filmDesignMediumPickerGrid_" + editor.mediumName
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        columns: width >= Math.max(640,
            2 * (materialLabel.implicitWidth + 180)
            + 3 * editor.themeTokens.spaceSm) ? 4 : 2
        columnSpacing: Theme.snapshot.spaceSm
        rowSpacing: Theme.snapshot.spaceSm
        ThemeControls.ThemeLabel {
            id: materialLabel
            text: "介质材料"
            objectName: "filmDesignMediumMaterialLabel_" + editor.mediumName
            color: Theme.snapshot.colorText
        }
        ThemeControls.ThemeMaterialPicker {
            id: materialBox
            objectName: "filmDesignMediumMaterial_" + editor.mediumName
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            model: editor.controller ? editor.controller.mediumOptions : []
            textRole: "name"
            valueRole: "id"
            currentIndex: editor.mediumData.kind === "material"
                          ? editor.indexFor(model, editor.mediumData.material_id) : -1
            optionObjectNamePrefix: "filmMediumMaterialOption_"
            accessibleLabel: editor.heading + "材料"
            placeholderText: "输入材料名称搜索"
            onActivated: function(index) {
                editor.controller.setMediumValue(
                    editor.mediumName, "material_id", valueAt(index))
            }
        }
        ThemeControls.ThemeLabel {
            text: "光学数据集"
            objectName: "filmDesignMediumDatasetLabel_" + editor.mediumName
            color: Theme.snapshot.colorText
        }
        ThemeControls.ThemeComboBox {
            id: setBox
            accessibleLabel: editor.heading + "光学数据集"
            objectName: "filmDesignMediumDataset_" + editor.mediumName
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            model: editor.controller
                   ? editor.controller.opticalSetsFor(editor.mediumData.material_id || "") : []
            textRole: "name"
            valueRole: "id"
            currentIndex: editor.mediumData.kind === "material"
                          ? editor.indexFor(model, editor.mediumData.optical_set_id) : -1
            enabled: editor.mediumData.kind === "material" && model.length > 0
            onActivated: editor.controller.setMediumValue(
                             editor.mediumName, "optical_set_id", String(currentValue || ""))
            delegate: ItemDelegate {
                id: datasetOption
                required property var modelData
                required property int index
                width: setBox.width
                text: modelData.name || modelData.id || ""
                hoverEnabled: true
                highlighted: index === setBox.highlightedIndex
                background: Rectangle {
                    radius: Theme.snapshot.radiusSm
                    color: datasetOption.highlighted || datasetOption.hovered
                           ? Theme.snapshot.colorSurfaceRaised : Theme.snapshot.colorSurface
                    border.width: datasetOption.visualFocus ? Theme.snapshot.borderWidth : 0
                    border.color: datasetOption.visualFocus
                                  ? Theme.snapshot.colorFocusRing : "transparent"
                }
                ToolTip.visible: hovered && !!editor.datasetTooltip(modelData)
                ToolTip.text: editor.datasetTooltip(modelData)
            }
        }
    }
}
