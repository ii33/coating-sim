import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls
import "theme_controls/NumericFormat.js" as NumericFormat

Rectangle {
    id: root
    objectName: "filmSimulationCalculationItems"
    property var controller
    property var analysisSnapshot: controller.spectrumAnalysis
    property var calculationItemsSnapshot: controller.calculationItems
    property bool expanded: true
    property bool sidebarManaged: false
    signal toggleRequested()
    implicitWidth: expanded ? 292 : 44
    implicitHeight: sidebarManaged ? 580 : 400
    radius: Theme.snapshot.radiusMd
    color: sidebarManaged ? "transparent" : Theme.snapshot.colorSurface
    border.color: Theme.snapshot.colorBorder
    border.width: sidebarManaged ? 0 : 1

    function number(value) {
        const n = Number(value)
        return Number.isFinite(n) ? NumericFormat.format(n, Qt.locale(), 3) : "—"
    }
    function percent(value) {
        const n = Number(value)
        return Number.isFinite(n) ? number(n * 100) + "%" : "—"
    }
    function polarizationLabel(value) {
        return value === "unpolarized" ? "非偏振" : String(value)
    }
    function polarizationIndex() {
        const items = controller.analysisPolarizations
        for (let i = 0; i < items.length; ++i) {
            if (items[i] === controller.analysisPolarization)
                return i
        }
        return 0
    }
    function pointText(point) {
        const range = point.wavelength_min_nm === point.wavelength_max_nm
                ? number(point.wavelength_nm) + " nm"
                : number(point.wavelength_min_nm) + "–"
                  + number(point.wavelength_max_nm) + " nm（平台）"
        return range + " · " + percent(point.value)
    }
    function extremaText(points, limit) {
        if (!points || points.length === 0)
            return "无"
        let shown = []
        const count = Math.min(points.length, limit)
        for (let i = 0; i < count; ++i)
            shown.push(pointText(points[i]))
        if (points.length > count)
            shown.push("… 另有 " + (points.length - count) + " 个")
        return shown.join("；")
    }
    function intervalText(interval) {
        const width = interval.complete
                ? number(interval.width_nm) + " nm"
                : "≥" + number(interval.observed_width_nm) + " nm（计算域截断）"
        return number(interval.start_nm) + "–" + number(interval.end_nm)
               + " nm · " + width
    }
    function intervalsText(intervals, reverse) {
        if (!intervals || intervals.length === 0)
            return "无超过阈值的区间"
        let rows = []
        for (let step = 0; step < intervals.length; ++step) {
            const index = reverse ? intervals.length - 1 - step : step
            rows.push(intervalText(intervals[index]))
        }
        return rows.join("；")
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Theme.snapshot.spaceSm
        spacing: Theme.snapshot.spaceXs

        RowLayout {
            Layout.fillWidth: true
            Label {
                Layout.fillWidth: true
                visible: root.expanded && !root.sidebarManaged
                text: "光谱统计"
                color: Theme.snapshot.colorText
                font.family: Theme.snapshot.fontFamily
                font.pointSize: Theme.snapshot.fontSizeBody
                font.weight: Theme.snapshot.fontWeightEmphasis
            }
            ThemeControls.ThemeButton {
                objectName: "filmSimulationStatisticsInnerToggle"
                visible: root.expanded && !root.sidebarManaged
                Layout.preferredWidth: implicitWidth
                Layout.minimumWidth: 0
                text: root.expanded ? "收起" : "›"
                Accessible.name: root.expanded ? "收起光谱统计" : "展开光谱统计"
                ToolTip.visible: hovered
                ToolTip.text: Accessible.name + "（F4）"
                onClicked: root.toggleRequested()
            }
            ThemeControls.ThemeButton {
                objectName: "filmSimulationAddBandButton"
                visible: root.expanded
                text: "+ 波段"
                Accessible.name: "添加计算波段"
                onClicked: root.controller.addCalculationBand()
            }
        }

        Label {
            visible: root.expanded
            Layout.fillWidth: true
            text: "区间积分和阈值通带只使用已计算波长；不外推。"
            color: Theme.snapshot.colorTextMuted
            wrapMode: Text.WordWrap
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeSmall
        }

        RowLayout {
            visible: root.expanded
            Layout.fillWidth: true
            spacing: Theme.snapshot.spaceXs
            Label { text: "曲线"; color: Theme.snapshot.colorTextMuted }
            ThemeControls.ThemeComboBox {
                id: analysisChannel
                objectName: "filmSpectrumAnalysisChannel"
                Layout.fillWidth: true
                accessibleLabel: "极值与通带统计曲线"
                model: ["T 透射率", "R 反射率"]
                currentIndex: root.controller.analysisChannel === "R" ? 1 : 0
                onActivated: function(index) {
                    root.controller.setSpectrumAnalysisChannel(index === 1 ? "R" : "T")
                }
            }
            ThemeControls.ThemeComboBox {
                id: analysisPolarization
                objectName: "filmSpectrumAnalysisPolarization"
                Layout.fillWidth: true
                accessibleLabel: "极值与通带统计偏振"
                model: root.controller.analysisPolarizations.map(function(value) {
                    return root.polarizationLabel(value)
                })
                currentIndex: root.polarizationIndex()
                onActivated: function(index) {
                    const items = root.controller.analysisPolarizations
                    if (index >= 0 && index < items.length)
                        root.controller.setSpectrumAnalysisPolarization(items[index])
                }
            }
        }

        RowLayout {
            visible: root.expanded
            Layout.fillWidth: true
            Label {
                text: "通过阈值 %"
                color: Theme.snapshot.colorTextMuted
                font.family: Theme.snapshot.fontFamily
                font.pointSize: Theme.snapshot.fontSizeSmall
            }
            ThemeControls.ThemeField {
                id: thresholdField
                objectName: "filmSpectrumAnalysisThreshold"
                Layout.preferredWidth: 92
                accessibleLabel: "通带阈值百分比"
                accessibleDescription: "默认 50%，范围 0–100%；仅影响显示统计。"
                numericDisplayValue: root.controller.spectrumThresholdPercent
                numericDisplayDecimals: 3
                inputMethodHints: Qt.ImhFormattedNumbersOnly
                validator: DoubleValidator { bottom: 0; top: 100; decimals: 3 }
                onEditingFinished: {
                    if (text.trim().length > 0)
                        root.controller.setSpectrumAnalysisThresholdPercent(Number(text))
                }
            }
            Label {
                Layout.fillWidth: true
                text: "默认 50%"
                color: Theme.snapshot.colorTextMuted
                font.family: Theme.snapshot.fontFamily
                font.pointSize: Theme.snapshot.fontSizeSmall
            }
        }

        Label {
            objectName: "filmSpectrumAnalysisMetadata"
            visible: root.expanded
            Layout.fillWidth: true
            text: root.analysisSnapshot.status === "ok"
                  ? root.polarizationLabel(root.analysisSnapshot.polarization)
                    + " · θ " + root.number(root.analysisSnapshot.angle_deg) + "°"
                    + " · λ " + root.number(root.analysisSnapshot.wavelength_min_nm)
                    + "–" + root.number(root.analysisSnapshot.wavelength_max_nm) + " nm"
                  : root.analysisSnapshot.message
            color: root.analysisSnapshot.status === "ok"
                   ? Theme.snapshot.colorTextMuted : Theme.snapshot.colorWarning
            wrapMode: Text.WordWrap
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeSmall
        }

        Label {
            objectName: "filmSpectrumGlobalExtrema"
            visible: root.expanded && root.analysisSnapshot.status === "ok"
            Layout.fillWidth: true
            text: "全局最小 " + root.extremaText(root.analysisSnapshot.global_min, 8)
                  + "\n全局最大 " + root.extremaText(root.analysisSnapshot.global_max, 8)
            color: Theme.snapshot.colorText
            wrapMode: Text.WordWrap
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeSmall
        }

        Label {
            objectName: "filmSpectrumLocalPeaks"
            visible: root.expanded && root.analysisSnapshot.status === "ok"
            Layout.fillWidth: true
            text: "局部峰（波长 nm · 值）: "
                  + root.extremaText(root.analysisSnapshot.local_peaks, 8)
            color: Theme.snapshot.colorText
            wrapMode: Text.WordWrap
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeSmall
        }
        Label {
            objectName: "filmSpectrumLocalValleys"
            visible: root.expanded && root.analysisSnapshot.status === "ok"
            Layout.fillWidth: true
            text: "局部谷（波长 nm · 值）: "
                  + root.extremaText(root.analysisSnapshot.local_valleys, 8)
            color: Theme.snapshot.colorText
            wrapMode: Text.WordWrap
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeSmall
        }

        Label {
            objectName: "filmSpectrumLongWavePass"
            visible: root.expanded && root.analysisSnapshot.status === "ok"
            Layout.fillWidth: true
            text: "长波通 / 低频（λ 增大 →）："
                  + root.intervalsText(
                        root.analysisSnapshot.long_wave_pass_intervals, false)
            color: Theme.snapshot.colorText
            wrapMode: Text.WordWrap
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeSmall
        }
        Label {
            objectName: "filmSpectrumShortWavePass"
            visible: root.expanded && root.analysisSnapshot.status === "ok"
            Layout.fillWidth: true
            text: "短波通 / 高频（λ 减小 ←）："
                  + root.intervalsText(
                        root.analysisSnapshot.short_wave_pass_intervals, true)
            color: Theme.snapshot.colorText
            wrapMode: Text.WordWrap
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeSmall
        }

        Rectangle {
            visible: root.expanded
            Layout.fillWidth: true
            height: Theme.snapshot.borderWidth
            color: Theme.snapshot.colorBorder
        }
        Label {
            visible: root.expanded
            text: "波段积分"
            color: Theme.snapshot.colorText
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeSmall
            font.weight: Theme.snapshot.fontWeightEmphasis
        }

        ScrollView {
            id: bandsView
            objectName: "filmSimulationBandsScrollView"
            visible: root.expanded
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            ScrollBar.vertical.policy: ScrollBar.AsNeeded

            ColumnLayout {
                width: bandsView.availableWidth
                spacing: Theme.snapshot.spaceSm

                Label {
                    Layout.fillWidth: true
                    visible: root.calculationItemsSnapshot.length === 0
                    text: "没有计算项，请添加波段。"
                    color: Theme.snapshot.colorTextMuted
                    wrapMode: Text.WordWrap
                }

                Repeater {
                    model: root.calculationItemsSnapshot
                    delegate: Rectangle {
                        id: bandCard
                        required property var modelData
                        Layout.fillWidth: true
                        implicitHeight: cardLayout.implicitHeight + Theme.snapshot.spaceSm * 2
                        radius: Theme.snapshot.radiusSm
                        color: Theme.snapshot.colorSurfaceRaised
                        border.color: Theme.snapshot.colorBorder

                        ColumnLayout {
                            id: cardLayout
                            anchors.fill: parent
                            anchors.margins: Theme.snapshot.spaceSm
                            spacing: Theme.snapshot.spaceXs

                            RowLayout {
                                Layout.fillWidth: true
                                Label {
                                    Layout.fillWidth: true
                                    text: "波段 " + (bandCard.modelData.index + 1)
                                    color: Theme.snapshot.colorText
                                    font.family: Theme.snapshot.fontFamily
                                    font.weight: Theme.snapshot.fontWeightEmphasis
                                }
                                ThemeControls.ThemeButton {
                                    text: "移除"
                                    Accessible.name: "移除计算波段 " + (bandCard.modelData.index + 1)
                                    onClicked: root.controller.removeCalculationBand(
                                                   bandCard.modelData.index)
                                }
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                Label { text: "nm"; color: Theme.snapshot.colorTextMuted }
                                ThemeControls.ThemeField {
                                    id: minimumField
                                    Layout.fillWidth: true
                                    accessibleLabel: "波段下限 nm"
                                    numericDisplayValue: bandCard.modelData.minimum_nm === null
                                                         ? "" : bandCard.modelData.minimum_nm
                                    numericDisplayDecimals: 3
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    validator: DoubleValidator {
                                        bottom: 0.000001; top: 1.0e12; decimals: 10
                                    }
                                    onEditingFinished: root.controller.setCalculationBand(
                                        bandCard.modelData.index, Number(text), Number(maximumField.text))
                                }
                                Label { text: "至"; color: Theme.snapshot.colorTextMuted }
                                ThemeControls.ThemeField {
                                    id: maximumField
                                    Layout.fillWidth: true
                                    accessibleLabel: "波段上限 nm"
                                    numericDisplayValue: bandCard.modelData.maximum_nm === null
                                                         ? "" : bandCard.modelData.maximum_nm
                                    numericDisplayDecimals: 3
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    validator: DoubleValidator {
                                        bottom: 0.000001; top: 1.0e12; decimals: 10
                                    }
                                    onEditingFinished: root.controller.setCalculationBand(
                                        bandCard.modelData.index, Number(minimumField.text), Number(text))
                                }
                            }

                            Label {
                                Layout.fillWidth: true
                                visible: bandCard.modelData.status !== "ok"
                                text: bandCard.modelData.message
                                color: Theme.snapshot.colorWarning
                                wrapMode: Text.WordWrap
                                font.family: Theme.snapshot.fontFamily
                                font.pointSize: Theme.snapshot.fontSizeSmall
                            }
                            Label {
                                Layout.fillWidth: true
                                visible: bandCard.modelData.status === "ok"
                                text: "R 均值 " + root.percent(bandCard.modelData.mean_R)
                                      + " · 最小 " + root.percent(bandCard.modelData.min_R)
                                      + " · 最大 " + root.percent(bandCard.modelData.max_R)
                                color: Theme.snapshot.colorText
                                wrapMode: Text.WordWrap
                                font.family: Theme.snapshot.fontFamily
                                font.pointSize: Theme.snapshot.fontSizeSmall
                            }
                            Label {
                                Layout.fillWidth: true
                                visible: bandCard.modelData.status === "ok"
                                text: "T 均值 " + root.percent(bandCard.modelData.mean_T)
                                      + " · 最小 " + root.percent(bandCard.modelData.min_T)
                                      + " · 最大 " + root.percent(bandCard.modelData.max_T)
                                color: Theme.snapshot.colorText
                                wrapMode: Text.WordWrap
                                font.family: Theme.snapshot.fontFamily
                                font.pointSize: Theme.snapshot.fontSizeSmall
                            }
                        }
                    }
                }
            }
        }
    }
}
