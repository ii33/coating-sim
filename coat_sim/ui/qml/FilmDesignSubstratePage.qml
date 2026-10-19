pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "theme_controls" as ThemeControls

ScrollView {
    id: substratePageScrollRoot
    property var editorWindow
    property var pageTabs
    readonly property var themeTokens: substratePageScrollRoot.editorWindow.themeColors
    objectName: "filmDesignSubstratePageScroll"
    clip: true
    contentWidth: availableWidth
    ScrollBar.vertical: ScrollBar {
        id: substrateVerticalBar
        objectName: "filmDesignSubstrateVerticalScrollBar"
    }

    ColumnLayout {
        width: Math.max(0, substratePageScrollRoot.availableWidth
            - substrateVerticalBar.width - substratePageScrollRoot.themeTokens.spaceXs)
        spacing: substratePageScrollRoot.themeTokens.spaceLg

        Rectangle {
            objectName: "filmDesignSubstrateFieldsCard"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            implicitHeight: substrateFields.implicitHeight + substratePageScrollRoot.themeTokens.spaceMd * 2
            radius: substratePageScrollRoot.themeTokens.radiusMd
            color: substratePageScrollRoot.themeTokens.colorSurface
            border.color: substratePageScrollRoot.themeTokens.colorBorder
            border.width: substratePageScrollRoot.themeTokens.borderWidth

            GridLayout {
                id: substrateFields
                objectName: "filmDesignParameterGrid"
                anchors.fill: parent
                anchors.margins: substratePageScrollRoot.themeTokens.spaceMd
                // Stack label/value pairs when the actual editor column is
                // narrow, including an expanded feature sidebar or large fonts.
                readonly property bool wide: width >= Math.max(640,
                    2 * (substrateMaterialLabel.implicitWidth + 180)
                    + 3 * substratePageScrollRoot.themeTokens.spaceSm)
                columns: wide ? 4 : 2
                columnSpacing: substratePageScrollRoot.themeTokens.spaceSm
                rowSpacing: substratePageScrollRoot.themeTokens.spaceSm

                ThemeControls.ThemeLabel {
                    Layout.columnSpan: substrateFields.columns
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    text: "独立基片属性"
                    heading: true
                    wrapMode: Text.WordWrap
                }
                ThemeControls.ThemeLabel {
                    objectName: "filmDesignLabelReferenceWavelength"
                    text: "参考波长"
                }
                RowLayout {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    spacing: substratePageScrollRoot.themeTokens.spaceSm
                    ThemeControls.ThemeField {
                        id: referenceField
                        objectName: "filmDesignReferenceWavelength"
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        accessibleLabel: "参考波长，单位 nm"
                        text: String(substratePageScrollRoot.editorWindow.controller.designMeta.reference_wavelength_nm)
                        selectByMouse: true
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        onTextEdited: substratePageScrollRoot.editorWindow.controller.markEditingDirty()
                        onEditingFinished: substratePageScrollRoot.editorWindow.controller.setDesignValue("reference_wavelength_nm", text)
                    }
                    ThemeControls.ThemeLabel { text: "nm"; muted: true }
                }
                CheckBox {
                    id: useKCheckBox
                    objectName: "filmDesignUseK"
                    Layout.columnSpan: 2
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    text: "仿真使用消光系数 k"
                    font.family: substratePageScrollRoot.themeTokens.fontFamily
                    font.pointSize: substratePageScrollRoot.themeTokens.fontSizeBody
                    contentItem: Text {
                        text: useKCheckBox.text
                        font: useKCheckBox.font
                        leftPadding: useKCheckBox.indicator.width + useKCheckBox.spacing
                        verticalAlignment: Text.AlignVCenter
                        wrapMode: Text.WordWrap
                        color: useKCheckBox.enabled ? substratePageScrollRoot.themeTokens.colorText
                                              : substratePageScrollRoot.themeTokens.colorDisabledText
                    }
                    checked: substratePageScrollRoot.editorWindow.controller.designMeta.use_k
                    onClicked: substratePageScrollRoot.editorWindow.controller.setDesignValue("use_k", checked)
                }
                ThemeControls.ThemeLabel {
                    id: substrateMaterialLabel
                    objectName: "filmDesignLabelSubstrateMaterial"
                    text: "基片材质"
                }
                ThemeControls.ThemeMaterialPicker {
                    id: substrateMaterial
                    objectName: "filmDesignSubstrateMaterial"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    model: substratePageScrollRoot.editorWindow.controller.substrateOptions
                    textRole: "name"
                    valueRole: "id"
                    duplicateContextLabel: "基片材料"
                    currentIndex: substratePageScrollRoot.editorWindow.indexFor(model, substratePageScrollRoot.editorWindow.controller.designMeta.substrate.material_id)
                    optionObjectNamePrefix: "filmSubstrateMaterialOption_"
                    accessibleLabel: "基片材质"
                    placeholderText: "输入材料名称搜索"
                    onActivated: function(index) {
                        substratePageScrollRoot.editorWindow.controller.setSubstrateValue(
                            "material_id", String(valueAt(index) || ""))
                    }
                }
                ThemeControls.ThemeLabel {
                    objectName: "filmDesignLabelSubstrateDataset"
                    text: "光学数据集"
                }
                ThemeControls.ThemeComboBox {
                    id: substrateSet
                    objectName: "filmDesignSubstrateDataset"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    accessibleLabel: "基片光学数据集"
                    model: substratePageScrollRoot.editorWindow.controller.opticalSetsForSubstrate(
                               substrateMaterial.currentValue || "")
                    textRole: "name"
                    valueRole: "id"
                    currentIndex: substratePageScrollRoot.editorWindow.indexFor(
                        model, substratePageScrollRoot.editorWindow.controller.designMeta.substrate.optical_set_id)
                    enabled: !!substratePageScrollRoot.editorWindow.controller.designMeta.substrate.material_id
                             && model.length > 0
                    onActivated: function(index) { substratePageScrollRoot.editorWindow.commitSubstrateSetSelection(index) }
                    delegate: ItemDelegate {
                        id: substrateDatasetOption
                        required property var modelData
                        required property int index
                        width: substrateSet.width
                        text: modelData.name || modelData.id || ""
                        hoverEnabled: true
                        highlighted: index === substrateSet.highlightedIndex
                        background: Rectangle {
                            radius: substratePageScrollRoot.themeTokens.radiusSm
                            color: substrateDatasetOption.highlighted || substrateDatasetOption.hovered
                                   ? substratePageScrollRoot.themeTokens.colorSurfaceRaised : substratePageScrollRoot.themeTokens.colorSurface
                            border.width: substrateDatasetOption.visualFocus ? substratePageScrollRoot.themeTokens.borderWidth : 0
                            border.color: substrateDatasetOption.visualFocus
                                          ? substratePageScrollRoot.themeTokens.colorFocusRing : "transparent"
                        }
                        ToolTip.visible: hovered && !!(modelData.notes
                            || modelData.wavelength_min !== undefined
                            || modelData.value_range_text)
                        ToolTip.text: {
                            const parts = []
                            if (modelData.wavelength_min !== undefined
                                    && modelData.wavelength_max !== undefined)
                                parts.push("波段：" + modelData.wavelength_min + "–"
                                           + modelData.wavelength_max + " "
                                           + (modelData.wavelength_unit || "nm"))
                            if (modelData.value_range_text)
                                parts.push("值范围：" + modelData.value_range_text)
                            const note = String(modelData.notes || "").replace(/\s+/g, " ").trim()
                            if (note)
                                parts.push(note.length > 120 ? note.slice(0, 117) + "…" : note)
                            return parts.join("\n")
                        }
                    }
                }
                ThemeControls.ThemeLabel {
                    objectName: "filmDesignLabelSubstrateThickness"
                    text: "实际厚度"
                }
                RowLayout {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    spacing: substratePageScrollRoot.themeTokens.spaceSm
                    ThemeControls.ThemeField {
                        id: substrateThickness
                        objectName: "filmDesignSubstrateThickness"
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        accessibleLabel: "基片实际厚度"
                        text: String(substratePageScrollRoot.editorWindow.controller.designMeta.substrate.thickness.value)
                        selectByMouse: true
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        onTextEdited: substratePageScrollRoot.editorWindow.controller.markEditingDirty()
                        onEditingFinished: substratePageScrollRoot.editorWindow.controller.setSubstrateValue("thickness.value", text)
                    }
                    ThemeControls.ThemeComboBox {
                        objectName: "filmDesignSubstrateThicknessUnit"
                        Layout.preferredWidth: 90
                        Layout.minimumWidth: 0
                        accessibleLabel: "基片实际厚度单位"
                        model: ["nm", "um", "mm"]
                        currentIndex: Math.max(0, model.indexOf(substratePageScrollRoot.editorWindow.controller.designMeta.substrate.thickness.unit))
                        onActivated: substratePageScrollRoot.editorWindow.controller.setSubstrateValue("thickness.unit", currentText)
                    }
                }
                ThemeControls.ThemeLabel {
                    objectName: "filmDesignLabelSubstrateSides"
                    text: "单/双面"
                }
                ThemeControls.ThemeComboBox {
                    id: sidesBox
                    objectName: "filmDesignSubstrateSides"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    accessibleLabel: "基片单面或双面模式"
                    model: [{label: "单面", value: "single"}, {label: "双面", value: "double"}]
                    textRole: "label"
                    valueRole: "value"
                    currentIndex: substratePageScrollRoot.editorWindow.controller.designMeta.substrate.sides === "double" ? 1 : 0
                    onActivated: {
                        const side = String(currentValue || "")
                        const previousSide = String(
                            substratePageScrollRoot.editorWindow.controller.designMeta.substrate.sides || "single")
                        if (side === previousSide) return
                        substratePageScrollRoot.editorWindow.controller.setSubstrateValue("sides", side)
                        if (side === "single") substratePageScrollRoot.pageTabs.currentIndex = 0
                    }
                }
                ThemeControls.ThemeLabel {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    Layout.columnSpan: substrateFields.columns
                    text: substratePageScrollRoot.editorWindow.controller.designMeta.substrate.sides === "single"
                          ? "单面模式：基片按半无限处理；出射介质与背面设计会保留，但不参与计算。"
                          : "双面模式：保留实际基片厚度与后界面；当 d > 10×计算波段最大波长时采用平均条纹近似，否则完整相干计算。"
                    wrapMode: Text.WordWrap
                    color: substratePageScrollRoot.themeTokens.colorInfo
                }
            }
        }

        Rectangle {
            objectName: "filmDesignMediaFieldsCard"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            implicitHeight: mediumFields.implicitHeight + substratePageScrollRoot.themeTokens.spaceMd * 2
            radius: substratePageScrollRoot.themeTokens.radiusMd
            color: substratePageScrollRoot.themeTokens.colorSurface
            border.color: substratePageScrollRoot.themeTokens.colorBorder
            border.width: substratePageScrollRoot.themeTokens.borderWidth
            ColumnLayout {
                id: mediumFields
                anchors.fill: parent
                anchors.margins: substratePageScrollRoot.themeTokens.spaceMd
                spacing: substratePageScrollRoot.themeTokens.spaceMd
                FilmDesignMediumEditor {
                    themeTokens: substratePageScrollRoot.themeTokens
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    controller: substratePageScrollRoot.editorWindow.controller
                    mediumName: "incident_medium"
                    mediumData: substratePageScrollRoot.editorWindow.controller.designMeta.incident_medium
                    heading: "入射介质"
                }
                Rectangle { Layout.fillWidth: true; implicitHeight: substratePageScrollRoot.themeTokens.borderWidth; color: substratePageScrollRoot.themeTokens.colorBorder }
                FilmDesignMediumEditor {
                    themeTokens: substratePageScrollRoot.themeTokens
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    controller: substratePageScrollRoot.editorWindow.controller
                    mediumName: "exit_medium"
                    mediumData: substratePageScrollRoot.editorWindow.controller.designMeta.exit_medium
                    heading: "出射介质"
                }
                ThemeControls.ThemeLabel {
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    visible: substratePageScrollRoot.editorWindow.controller.designMeta.substrate.sides === "single"
                    text: "单面模式下，出射介质保留在设计中但不参与计算。"
                    wrapMode: Text.WordWrap
                    muted: true
                }
            }
        }
    }
}
