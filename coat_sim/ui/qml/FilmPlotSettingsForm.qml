import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ColumnLayout {
    id: root
    objectName: "filmPlotSettingsForm"
    property var controller
    required property var themeTokens
    property string localError: ""
    spacing: root.themeTokens.spaceMd

    function loadFromController() {
        localError = ""
        var value = controller.plotSettings
        curveR.checked = value.curves.indexOf("R") >= 0
        curveT.checked = value.curves.indexOf("T") >= 0
        curveA.checked = value.curves.indexOf("A") >= 0
        curveSource.checked = value.curves.indexOf("source_spectrum") >= 0
        curveReflected.checked = value.curves.indexOf("reflected_spectrum") >= 0
        curveTransmitted.checked = value.curves.indexOf("transmitted_spectrum") >= 0
        curveAbsorbed.checked = value.curves.indexOf("absorbed_spectrum") >= 0
        polarizationS.checked = value.polarizations.indexOf("s") >= 0
        polarizationP.checked = value.polarizations.indexOf("p") >= 0
        xAuto.checked = value.x.auto
        xMin.setNumericSourceValue(value.x.min)
        xMax.setNumericSourceValue(value.x.max)
        xTickAuto.checked = value.x.major_tick_auto
        xTick.setNumericSourceValue(value.x.major_tick)
        yAuto.checked = value.y.auto
        yMin.setNumericSourceValue(value.y.min * 100)
        yMax.setNumericSourceValue(value.y.max * 100)
        yTickAuto.checked = value.y.major_tick_auto
        yTick.setNumericSourceValue(value.y.major_tick * 100)
        showGrid.checked = value.grid
    }
    function commitRangeEdit(field, automaticCheck) {
        if (!field.numericValueEdited)
            return
        var rawText = field.numericEditText
        var nextValue = Number(rawText)
        var currentValue = Number(field.numericDisplayValue)
        if (Number.isFinite(nextValue) && Number.isFinite(currentValue)
                && nextValue !== currentValue)
            automaticCheck.checked = false
        field.setNumericSourceValue(rawText)
    }
    function settingsPayload() {
        root.commitRangeEdit(xMin, xAuto)
        root.commitRangeEdit(xMax, xAuto)
        root.commitRangeEdit(yMin, yAuto)
        root.commitRangeEdit(yMax, yAuto)
        var selected = []
        if (curveR.checked) selected.push("R")
        if (curveT.checked) selected.push("T")
        if (curveA.checked) selected.push("A")
        if (curveSource.checked) selected.push("source_spectrum")
        if (curveReflected.checked) selected.push("reflected_spectrum")
        if (curveTransmitted.checked) selected.push("transmitted_spectrum")
        if (curveAbsorbed.checked) selected.push("absorbed_spectrum")
        var selectedPolarizations = []
        if (polarizationS.checked) selectedPolarizations.push("s")
        if (polarizationP.checked) selectedPolarizations.push("p")
        return {
            curves: selected,
            polarizations: selectedPolarizations,
            x: {auto: xAuto.checked,
                min: Number(xMin.numericValueEdited ? xMin.numericEditText : xMin.numericDisplayValue),
                max: Number(xMax.numericValueEdited ? xMax.numericEditText : xMax.numericDisplayValue),
                major_tick_auto: xTickAuto.checked,
                major_tick: Number(xTick.numericValueEdited ? xTick.numericEditText : xTick.numericDisplayValue)},
            y: {unit: "percent", auto: yAuto.checked,
                min: Number(yMin.numericValueEdited ? yMin.numericEditText : yMin.numericDisplayValue),
                max: Number(yMax.numericValueEdited ? yMax.numericEditText : yMax.numericDisplayValue),
                major_tick_auto: yTickAuto.checked,
                major_tick: Number(yTick.numericValueEdited ? yTick.numericEditText : yTick.numericDisplayValue)},
            grid: showGrid.checked
        }
    }

    Label {
        text: "绘制曲线"
        color: root.themeTokens.colorText
        font.weight: root.themeTokens.fontWeightEmphasis
    }
    GridLayout {
        Layout.fillWidth: true
        columns: root.width < 520 ? 1 : 2
        CheckBox { id: polarizationS; objectName: "filmPlotSettingsPolarizationS"; text: "s 偏振曲线叠加" }
        CheckBox { id: polarizationP; objectName: "filmPlotSettingsPolarizationP"; text: "p 偏振曲线叠加" }
        CheckBox { id: curveR; text: "R — 反射率" }
        CheckBox { id: curveT; text: "T — 透射率" }
        CheckBox { id: curveA; text: "A — 吸收率" }
        CheckBox { id: curveSource; text: "光源光谱" }
        CheckBox { id: curveReflected; text: "反射光谱" }
        CheckBox { id: curveTransmitted; text: "透射光谱" }
        CheckBox { id: curveAbsorbed; text: "吸收光谱" }
    }
    GridLayout {
        objectName: "filmPlotSettingsAxisGrid"
        Layout.fillWidth: true
        columns: root.width < 680 ? 1 : 2
        columnSpacing: root.themeTokens.spaceMd
        rowSpacing: root.themeTokens.spaceSm
        GroupBox {
            Layout.fillWidth: true
            title: "X 轴 — 波长（nm）"
            ColumnLayout {
                anchors.fill: parent
                CheckBox { id: xAuto; objectName: "filmPlotSettingsXAuto"; text: "自动范围" }
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "最小值" }
                    ThemeControls.ThemeField {
                        id: xMin; objectName: "filmPlotSettingsXMin"
                        Layout.fillWidth: true
                        accessibleLabel: "X 轴最小值（nm）"; numericDisplayDecimals: 3
                        validator: DoubleValidator {}
                    }
                    Label { text: "最大值" }
                    ThemeControls.ThemeField {
                        id: xMax; objectName: "filmPlotSettingsXMax"
                        Layout.fillWidth: true
                        accessibleLabel: "X 轴最大值（nm）"; numericDisplayDecimals: 3
                        validator: DoubleValidator {}
                    }
                }
                CheckBox { id: xTickAuto; text: "自动主刻度间距" }
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "主刻度" }
                    ThemeControls.ThemeField {
                        id: xTick; Layout.fillWidth: true; enabled: !xTickAuto.checked
                        accessibleLabel: "X 轴主刻度间距（nm）"; numericDisplayDecimals: 3
                        validator: DoubleValidator { bottom: 0.0000001 }
                    }
                }
            }
        }
        GroupBox {
            Layout.fillWidth: true
            title: "Y 轴 — 百分比"
            ColumnLayout {
                anchors.fill: parent
                CheckBox { id: yAuto; objectName: "filmPlotSettingsYAuto"; text: "自动范围" }
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "最小值" }
                    ThemeControls.ThemeField {
                        id: yMin; objectName: "filmPlotSettingsYMin"; Layout.fillWidth: true
                        accessibleLabel: "Y 轴最小值（%）"; numericDisplayDecimals: 3
                        validator: DoubleValidator {}
                    }
                    Label { text: "最大值" }
                    ThemeControls.ThemeField {
                        id: yMax; objectName: "filmPlotSettingsYMax"; Layout.fillWidth: true
                        accessibleLabel: "Y 轴最大值（%）"; numericDisplayDecimals: 3
                        validator: DoubleValidator {}
                    }
                }
                CheckBox { id: yTickAuto; text: "自动主刻度间距" }
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "主刻度（%）" }
                    ThemeControls.ThemeField {
                        id: yTick; Layout.fillWidth: true; enabled: !yTickAuto.checked
                        accessibleLabel: "Y 轴主刻度间距（%）"; numericDisplayDecimals: 3
                        validator: DoubleValidator { bottom: 0.0000001 }
                    }
                }
            }
        }
    }
    Connections {
        target: xMin
        function onActiveFocusChanged() {
            if (!xMin.activeFocus) root.commitRangeEdit(xMin, xAuto)
        }
        function onEditingFinished() { root.commitRangeEdit(xMin, xAuto) }
    }
    Connections {
        target: xMax
        function onActiveFocusChanged() {
            if (!xMax.activeFocus) root.commitRangeEdit(xMax, xAuto)
        }
        function onEditingFinished() { root.commitRangeEdit(xMax, xAuto) }
    }
    Connections {
        target: yMin
        function onActiveFocusChanged() {
            if (!yMin.activeFocus) root.commitRangeEdit(yMin, yAuto)
        }
        function onEditingFinished() { root.commitRangeEdit(yMin, yAuto) }
    }
    Connections {
        target: yMax
        function onActiveFocusChanged() {
            if (!yMax.activeFocus) root.commitRangeEdit(yMax, yAuto)
        }
        function onEditingFinished() { root.commitRangeEdit(yMax, yAuto) }
    }
    CheckBox {
        id: showGrid
        objectName: "filmPlotSettingsShowGrid"
        text: "显示网格辅助线（主刻度）"
    }
    Label {
        objectName: "filmPlotSettingsError"
        Layout.fillWidth: true
        visible: root.localError.length > 0
        text: root.localError
        color: root.themeTokens.colorError
        wrapMode: Text.WordWrap
    }
    Label {
        Layout.fillWidth: true
        text: "本征 R/T/A 与光源加权谱共用百分比坐标；选择手动范围时须确保最小值小于最大值。"
        color: root.themeTokens.colorTextMuted
        wrapMode: Text.WordWrap
    }
}
