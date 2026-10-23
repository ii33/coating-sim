pragma ComponentBehavior: Bound

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
    property var extremaItemsSnapshot: controller.extremaItems
    property bool expanded: true
    property bool sidebarManaged: false
    // ThemeEngineBridge injects Theme into the QML context; qmllint cannot model this runtime context property.
    // qmllint disable unqualified
    readonly property var themeTokens: Theme.snapshot
    // qmllint enable unqualified
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
    function syncCalculationRows() {
        const items = root.controller.calculationItems
        if (items.length < calculationRows.count)
            calculationRows.clear()
        for (let row = 0; row < items.length; ++row) {
            if (row < calculationRows.count)
                calculationRows.setProperty(row, "bandRow", items[row])
            else
                calculationRows.append({ bandRow: items[row] })
        }
    }

    function syncExtremaRows() {
        const items = root.controller.extremaItems
        if (items.length < extremaRows.count)
            extremaRows.clear()
        for (let row = 0; row < items.length; ++row) {
            if (row < extremaRows.count)
                extremaRows.setProperty(row, "bandRow", items[row])
            else
                extremaRows.append({ bandRow: items[row] })
        }
    }

    function ensureBandsItemVisible(item) {
        const flickable = bandsView.contentItem as Flickable
        const content = flickable ? flickable.contentItem : null
        if (!item || !flickable || !content)
            return
        const position = item.mapToItem(content, 0, 0)
        const top = position.y
        const bottom = top + item.height
        const margin = root.themeTokens.spaceXs
        let target = flickable.contentY
        if (top < flickable.contentY + margin)
            target = top - margin
        else if (bottom > flickable.contentY + flickable.height - margin)
            target = bottom + margin - flickable.height
        const maximum = Math.max(0, flickable.contentHeight - flickable.height)
        flickable.contentY = Math.max(0, Math.min(maximum, target))
    }

    Component.onCompleted: {
        root.syncCalculationRows()
        root.syncExtremaRows()
    }

    Connections {
        target: root.controller
        function onCalculationItemsChanged() { root.syncCalculationRows() }
        function onExtremaItemsChanged() { root.syncExtremaRows() }
    }

    ListModel {
        id: calculationRows
        dynamicRoles: true
    }

    ListModel {
        id: extremaRows
        dynamicRoles: true
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
            text: "积分、峰谷与通带均基于已计算光谱；峰谷只用显式波段。"
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
            text: "统计波段"
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

                RowLayout {
                    Layout.fillWidth: true
                    Label {
                        Layout.fillWidth: true
                        text: "峰谷极值统计波段"
                        color: root.themeTokens.colorText
                        font.family: root.themeTokens.fontFamily
                        font.pointSize: root.themeTokens.fontSizeSmall
                        font.weight: root.themeTokens.fontWeightEmphasis
                    }
                    ThemeControls.ThemeButton {
                        objectName: "filmSimulationAddExtremaBandButton"
                        text: "+ 极值波段"
                        Accessible.name: "添加极值统计波段"
                        onClicked: root.controller.addExtremaBand()
                    }
                }

                Label {
                    objectName: "filmSpectrumExtremaUnconfigured"
                    Layout.fillWidth: true
                    text: "峰谷只统计显式波段内已计算的样点；不插值或补算。空列表表示未配置。"
                    color: root.themeTokens.colorTextMuted
                    wrapMode: Text.WordWrap
                    font.family: root.themeTokens.fontFamily
                    font.pointSize: root.themeTokens.fontSizeSmall
                }

                Label {
                    Layout.fillWidth: true
                    visible: root.expanded && root.extremaItemsSnapshot.length === 0
                    text: "未配置极值统计波段。"
                    color: root.themeTokens.colorTextMuted
                    wrapMode: Text.WordWrap
                    font.family: root.themeTokens.fontFamily
                    font.pointSize: root.themeTokens.fontSizeSmall
                }

                Repeater {
                    model: extremaRows
                    delegate: Rectangle {
                        id: extremaCard
                        required property var bandRow
                        property string minimumDraftText: extremaCard.numberText(bandRow.minimum_nm)
                        property string maximumDraftText: extremaCard.numberText(bandRow.maximum_nm)
                        property bool minimumDraftDirty: false
                        property bool maximumDraftDirty: false
                        readonly property var modelData: bandRow
                        Layout.fillWidth: true
                        implicitHeight: extremaLayout.implicitHeight + root.themeTokens.spaceSm * 2
                        radius: root.themeTokens.radiusSm
                        color: root.themeTokens.colorSurfaceRaised
                        border.color: root.themeTokens.colorBorder

                        function numberText(value) {
                            return value === null || value === undefined ? "" : String(value)
                        }
                        function syncDraftText() {
                            if (minimumDraftDirty && Number.isFinite(Number(minimumDraftText))
                                    && Number(minimumDraftText) === bandRow.minimum_nm)
                                minimumDraftDirty = false
                            if (maximumDraftDirty && Number.isFinite(Number(maximumDraftText))
                                    && Number(maximumDraftText) === bandRow.maximum_nm)
                                maximumDraftDirty = false
                            if (!minimumDraftDirty)
                                minimumDraftText = numberText(bandRow.minimum_nm)
                            if (!maximumDraftDirty)
                                maximumDraftText = numberText(bandRow.maximum_nm)
                        }
                        onBandRowChanged: syncDraftText()

                        ColumnLayout {
                            id: extremaLayout
                            anchors.fill: parent
                            anchors.margins: root.themeTokens.spaceSm
                            spacing: root.themeTokens.spaceXs

                            RowLayout {
                                Layout.fillWidth: true
                                Label {
                                    Layout.fillWidth: true
                                    text: "极值波段 " + (extremaCard.modelData.index + 1)
                                    color: root.themeTokens.colorText
                                    font.family: root.themeTokens.fontFamily
                                    font.weight: root.themeTokens.fontWeightEmphasis
                                }
                                ThemeControls.ThemeButton {
                                    id: removeExtremaBandButton
                                    objectName: "filmSimulationRemoveExtremaBandButton_"
                                                 + extremaCard.modelData.index
                                    text: "移除"
                                    Accessible.name: "移除极值统计波段 "
                                                   + (extremaCard.modelData.index + 1)
                                    onActiveFocusChanged: {
                                        if (activeFocus)
                                            root.ensureBandsItemVisible(removeExtremaBandButton)
                                    }
                                    onClicked: root.controller.removeExtremaBand(
                                                   extremaCard.modelData.index)
                                }
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                Label { text: "nm"; color: root.themeTokens.colorTextMuted }
                                ThemeControls.ThemeField {
                                    id: extremaMinimumField
                                    objectName: "filmSimulationExtremaMinimumField_"
                                                 + extremaCard.modelData.index
                                    Layout.fillWidth: true
                                    accessibleLabel: "极值波段下限 nm"
                                    onActiveFocusChanged: {
                                        if (activeFocus)
                                            root.ensureBandsItemVisible(extremaMinimumField)
                                    }
                                    numericDisplayValue: extremaCard.minimumDraftText
                                    numericDisplayDecimals: 3
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    validator: DoubleValidator {
                                        bottom: 0.000001; top: 1.0e12; decimals: 10
                                    }
                                    onTextEdited: {
                                        extremaCard.minimumDraftDirty = true
                                        extremaCard.minimumDraftText = text
                                    }
                                    onEditingFinished: root.controller.setExtremaBand(
                                        extremaCard.modelData.index,
                                        extremaMinimumField.acceptableInput ? Number(text)
                                                                           : extremaCard.modelData.minimum_nm,
                                        extremaCard.modelData.maximum_nm)
                                }
                                Label { text: "至"; color: root.themeTokens.colorTextMuted }
                                ThemeControls.ThemeField {
                                    id: extremaMaximumField
                                    objectName: "filmSimulationExtremaMaximumField_"
                                                 + extremaCard.modelData.index
                                    Layout.fillWidth: true
                                    accessibleLabel: "极值波段上限 nm"
                                    onActiveFocusChanged: {
                                        if (activeFocus)
                                            root.ensureBandsItemVisible(extremaMaximumField)
                                    }
                                    numericDisplayValue: extremaCard.maximumDraftText
                                    numericDisplayDecimals: 3
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    validator: DoubleValidator {
                                        bottom: 0.000001; top: 1.0e12; decimals: 10
                                    }
                                    onTextEdited: {
                                        extremaCard.maximumDraftDirty = true
                                        extremaCard.maximumDraftText = text
                                    }
                                    onEditingFinished: root.controller.setExtremaBand(
                                        extremaCard.modelData.index,
                                        extremaCard.modelData.minimum_nm,
                                        extremaMaximumField.acceptableInput ? Number(text)
                                                                           : extremaCard.modelData.maximum_nm)
                                }
                            }

                            Label {
                                Layout.fillWidth: true
                                visible: extremaCard.modelData.status === "ok"
                                text: "波段内已取样 "
                                      + extremaCard.numberText(extremaCard.modelData.sample_minimum_nm)
                                      + "–"
                                      + extremaCard.numberText(extremaCard.modelData.sample_maximum_nm)
                                      + " nm · " + extremaCard.modelData.sample_count + " 点"
                                      + " · " + root.polarizationLabel(
                                            extremaCard.modelData.polarization)
                                      + " · θ " + root.number(extremaCard.modelData.angle_deg) + "°"
                                color: root.themeTokens.colorTextMuted
                                wrapMode: Text.WordWrap
                                font.family: root.themeTokens.fontFamily
                                font.pointSize: root.themeTokens.fontSizeSmall
                            }
                            Label {
                                objectName: "filmSpectrumExtremaGlobal_"
                                             + extremaCard.modelData.index
                                Layout.fillWidth: true
                                visible: root.expanded && extremaCard.modelData.status === "ok"
                                text: "波段内全局最小 "
                                      + root.extremaText(extremaCard.modelData.global_min, 8)
                                      + "\n波段内全局最大 "
                                      + root.extremaText(extremaCard.modelData.global_max, 8)
                                      + "\n注：波段两端可作为全局极值，不作为局部峰谷。"
                                color: root.themeTokens.colorText
                                wrapMode: Text.WordWrap
                                font.family: root.themeTokens.fontFamily
                                font.pointSize: root.themeTokens.fontSizeSmall
                            }
                            Label {
                                objectName: "filmSpectrumExtremaPeaks_"
                                             + extremaCard.modelData.index
                                Layout.fillWidth: true
                                visible: root.expanded && extremaCard.modelData.status === "ok"
                                text: "波段内局部峰（波长 nm · 值）: "
                                      + root.extremaText(extremaCard.modelData.local_peaks, 8)
                                color: root.themeTokens.colorText
                                wrapMode: Text.WordWrap
                                font.family: root.themeTokens.fontFamily
                                font.pointSize: root.themeTokens.fontSizeSmall
                            }
                            Label {
                                objectName: "filmSpectrumExtremaValleys_"
                                             + extremaCard.modelData.index
                                Layout.fillWidth: true
                                visible: root.expanded && extremaCard.modelData.status === "ok"
                                text: "波段内局部谷（波长 nm · 值）: "
                                      + root.extremaText(extremaCard.modelData.local_valleys, 8)
                                color: root.themeTokens.colorText
                                wrapMode: Text.WordWrap
                                font.family: root.themeTokens.fontFamily
                                font.pointSize: root.themeTokens.fontSizeSmall
                            }
                            Label {
                                Layout.fillWidth: true
                                visible: extremaCard.modelData.status !== "ok"
                                text: extremaCard.modelData.message
                                color: root.themeTokens.colorWarning
                                wrapMode: Text.WordWrap
                                font.family: root.themeTokens.fontFamily
                                font.pointSize: root.themeTokens.fontSizeSmall
                            }
                        }
                    }
                }
                Rectangle {
                    visible: root.expanded
                    Layout.fillWidth: true
                    height: root.themeTokens.borderWidth
                    color: root.themeTokens.colorBorder
                }

                Label {
                    visible: root.expanded
                    text: "波段积分"
                    color: root.themeTokens.colorText
                    font.family: root.themeTokens.fontFamily
                    font.pointSize: root.themeTokens.fontSizeSmall
                    font.weight: root.themeTokens.fontWeightEmphasis
                }
                Label {
                    Layout.fillWidth: true
                    visible: root.expanded && root.calculationItemsSnapshot.length === 0
                    text: "没有计算项，请添加波段。"
                    color: Theme.snapshot.colorTextMuted
                    wrapMode: Text.WordWrap
                    font.family: Theme.snapshot.fontFamily
                    font.pointSize: Theme.snapshot.fontSizeSmall
                }

                Repeater {
                    model: calculationRows
                    delegate: Rectangle {
                        id: bandCard
                        required property var bandRow
                        property string minimumDraftText: bandCard.numberText(bandRow.minimum_nm)
                        property string maximumDraftText: bandCard.numberText(bandRow.maximum_nm)
                        property bool minimumDraftDirty: false
                        property bool maximumDraftDirty: false
                        readonly property var modelData: bandRow
                        Layout.fillWidth: true
                        implicitHeight: cardLayout.implicitHeight + Theme.snapshot.spaceSm * 2
                        radius: Theme.snapshot.radiusSm
                        color: Theme.snapshot.colorSurfaceRaised
                        border.color: Theme.snapshot.colorBorder

                        function numberText(value) {
                            return value === null || value === undefined ? "" : String(value)
                        }
                        function syncDraftText() {
                            if (minimumDraftDirty && Number.isFinite(Number(minimumDraftText))
                                    && Number(minimumDraftText) === bandRow.minimum_nm)
                                minimumDraftDirty = false
                            if (maximumDraftDirty && Number.isFinite(Number(maximumDraftText))
                                    && Number(maximumDraftText) === bandRow.maximum_nm)
                                maximumDraftDirty = false
                            if (!minimumDraftDirty)
                                minimumDraftText = numberText(bandRow.minimum_nm)
                            if (!maximumDraftDirty)
                                maximumDraftText = numberText(bandRow.maximum_nm)
                        }
                        onBandRowChanged: syncDraftText()

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
                                    id: removeCalculationBandButton
                                    objectName: "filmSimulationRemoveBandButton_"
                                                 + bandCard.modelData.index
                                    text: "移除"
                                    Accessible.name: "移除计算波段 " + (bandCard.modelData.index + 1)
                                    onActiveFocusChanged: {
                                        if (activeFocus)
                                            root.ensureBandsItemVisible(removeCalculationBandButton)
                                    }
                                    onClicked: root.controller.removeCalculationBand(
                                                   bandCard.modelData.index)
                                }
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                Label { text: "nm"; color: Theme.snapshot.colorTextMuted }
                                ThemeControls.ThemeField {
                                    id: minimumField
                                    objectName: "filmSimulationBandMinimumField_"
                                                 + bandCard.modelData.index
                                    Layout.fillWidth: true
                                    accessibleLabel: "波段下限 nm"
                                    onActiveFocusChanged: {
                                        if (activeFocus)
                                            root.ensureBandsItemVisible(minimumField)
                                    }
                                    numericDisplayValue: bandCard.minimumDraftText
                                    numericDisplayDecimals: 3
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    validator: DoubleValidator {
                                        bottom: 0.000001; top: 1.0e12; decimals: 10
                                    }
                                    onTextEdited: {
                                        bandCard.minimumDraftDirty = true
                                        bandCard.minimumDraftText = text
                                    }
                                    onEditingFinished: root.controller.setCalculationBand(
                                        bandCard.modelData.index,
                                        minimumField.acceptableInput ? Number(text)
                                                                    : bandCard.modelData.minimum_nm,
                                        bandCard.modelData.maximum_nm)
                                }
                                Label { text: "至"; color: Theme.snapshot.colorTextMuted }
                                ThemeControls.ThemeField {
                                    id: maximumField
                                    objectName: "filmSimulationBandMaximumField_"
                                                 + bandCard.modelData.index
                                    Layout.fillWidth: true
                                    accessibleLabel: "波段上限 nm"
                                    onActiveFocusChanged: {
                                        if (activeFocus)
                                            root.ensureBandsItemVisible(maximumField)
                                    }
                                    numericDisplayValue: bandCard.maximumDraftText
                                    numericDisplayDecimals: 3
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    validator: DoubleValidator {
                                        bottom: 0.000001; top: 1.0e12; decimals: 10
                                    }
                                    onTextEdited: {
                                        bandCard.maximumDraftDirty = true
                                        bandCard.maximumDraftText = text
                                    }
                                    onEditingFinished: root.controller.setCalculationBand(
                                        bandCard.modelData.index,
                                        bandCard.modelData.minimum_nm,
                                        maximumField.acceptableInput ? Number(text)
                                                                    : bandCard.modelData.maximum_nm)
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
