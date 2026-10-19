pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Shapes
import "theme_controls" as ThemeControls
import "theme_controls/NumericFormat.js" as NumericFormat

Rectangle {
    id: root
    objectName: "filmSpectrumPlot"
    property var controller
    required property var themeColors
    property real floatingControlsRightEdge: width
    property var plotSnapshot: controller.plotData
    property var plotView: controller.plotView
    signal openPlotSettingsRequested()
    readonly property bool softwareRendererActive:
        GraphicsInfo.api === GraphicsInfo.Software
    readonly property bool plotVectorReady: controller.hasResult && plotSnapshot.series.length > 0
    readonly property int plotCurveCount: plotSnapshot.series.length
    readonly property real curveScale:
        (plotSnapshot.data_x_max - plotSnapshot.data_x_min) / (plotView.x_max - plotView.x_min)
    readonly property real curveTranslationX:
        (plotSnapshot.data_x_min - plotView.x_min) / (plotView.x_max - plotView.x_min) * plotViewport.width
    readonly property real curveTranslationY:
        (plotView.y_max - plotSnapshot.data_y_max) / (plotView.y_max - plotView.y_min) * plotViewport.height
    radius: root.themeColors.radiusMd
    color: root.themeColors.colorSurface
    border.color: root.themeColors.colorBorder
    clip: true

    FontMetrics {
        id: plotLabelMetrics
        font.family: root.themeColors.fontFamily
        font.pointSize: root.themeColors.fontSizeSmall
    }
    Label {
        id: tickLabelMeasure
        visible: false
        text: "100%"
        font.family: root.themeColors.fontFamily
        font.pointSize: root.themeColors.fontSizeSmall
    }
    readonly property real plotLabelHeight: tickLabelMeasure.implicitHeight
    readonly property real rightTickMargin: {
        let extent = 42
        for (const tick of root.plotView.x_ticks)
            extent = Math.max(extent, plotLabelMetrics.advanceWidth(root.formatNumber(tick)))
        return Math.ceil(extent / 2) + root.themeColors.spaceXs
    }

    readonly property real yTickLabelWidth: {
        let extent = plotLabelMetrics.advanceWidth("100%")
        for (const tick of root.plotView.y_ticks)
            extent = Math.max(extent, plotLabelMetrics.advanceWidth(root.formatNumber(tick * 100) + "%"))
        return Math.ceil(extent)
    }
    readonly property real legendNaturalWidth: {
        let extent = 0
        for (const series of root.plotSnapshot.series)
            extent += root.themeColors.spaceMd + root.themeColors.spaceXs
                      + plotLabelMetrics.advanceWidth(series.label) + root.themeColors.spaceXs
        return Math.max(1, Math.ceil(extent - root.themeColors.spaceXs))
    }

    component ZoomActionContent: Item {
        id: actionContent
        property string mark: ""
        implicitWidth: 18
        implicitHeight: 18

        Item {
            id: glyph
            objectName: actionContent.objectName + "Glyph"
            anchors.centerIn: parent
            width: 18
            height: 18
            Rectangle {
                x: 1
                y: 1
                width: 11
                height: 11
                radius: width / 2
                color: "transparent"
                border.width: 1.5
                border.color: root.themeColors.colorText
            }
            Rectangle {
                x: 11
                y: 11
                width: 7
                height: 2
                radius: 1
                rotation: 45
                transformOrigin: Item.Left
                color: root.themeColors.colorText
            }
            Rectangle {
                x: 3.5; y: 6
                width: 6; height: 1.5
                radius: 0.75
                visible: actionContent.mark !== "↺"
                color: root.themeColors.colorText
            }
            Rectangle {
                x: 5.75; y: 3.75
                width: 1.5; height: 6
                radius: 0.75
                visible: actionContent.mark === "+"
                color: root.themeColors.colorText
            }
            Shape {
                anchors.fill: parent
                visible: actionContent.mark === "↺"
                ShapePath {
                    strokeColor: root.themeColors.colorText
                    strokeWidth: 1.2
                    fillColor: "transparent"
                    PathSvg { path: "M9.5 6.5 A3 3 0 1 1 5 4 L3.5 5 M3.5 2.5 L3.5 5 L6 5" }
                }
            }
        }
    }

    function mapPoints(points) {
        let mapped = []
        if (!points || plotViewport.width <= 0 || plotViewport.height <= 0)
            return mapped
        const xSpan = plotSnapshot.data_x_max - plotSnapshot.data_x_min
        const ySpan = plotSnapshot.data_y_max - plotSnapshot.data_y_min
        if (xSpan <= 0 || ySpan <= 0)
            return mapped
        for (let i = 0; i < points.length; ++i) {
            const x = (points[i][0] - plotSnapshot.data_x_min) / xSpan * plotViewport.width
            const y = (plotSnapshot.data_y_max - points[i][1]) / ySpan * plotViewport.height
            mapped.push(Qt.point(x, y))
        }
        return mapped
    }
    function formatNumber(value) {
        return NumericFormat.format(value, Qt.locale(), 3)
    }
    function plotX(value) {
        return plotViewport.x + (value - plotView.x_min)
               / (plotView.x_max - plotView.x_min) * plotViewport.width
    }
    function plotY(value) {
        return plotViewport.y + (plotView.y_max - value)
               / (plotView.y_max - plotView.y_min) * plotViewport.height
    }

    Label {
        id: optimizationNotice
        objectName: "filmOptimizationPreviewNotice"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.leftMargin: root.themeColors.spaceSm
        anchors.rightMargin: root.themeColors.spaceSm
        visible: root.controller.optimizationPreviewNotice.length > 0
        height: visible ? implicitHeight + root.themeColors.spaceXs * 2 : 0
        padding: root.themeColors.spaceXs
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
        text: root.controller.optimizationPreviewNotice
        color: root.themeColors.colorWarning
        font.family: root.themeColors.fontFamily
        font.pointSize: root.themeColors.fontSizeSmall
        background: Rectangle {
            color: root.themeColors.colorSurfaceRaised
            radius: root.themeColors.radiusSm
        }
    }

    Item {
        id: chartSurface
        objectName: "filmSpectrumChartSurface"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: optimizationNotice.bottom
        anchors.bottom: parent.bottom
        anchors.leftMargin: root.themeColors.spaceSm
        anchors.rightMargin: root.themeColors.spaceSm
        anchors.bottomMargin: root.themeColors.spaceSm
        clip: true
        z: 1

        Item {
            id: plotViewport
            objectName: "filmSpectrumVectorViewport"
            x: 32 + root.yTickLabelWidth + root.themeColors.spaceSm
            y: Math.max(root.themeColors.spaceSm, root.plotLabelHeight / 2)
            width: Math.max(1, chartSurface.width - x - root.rightTickMargin)
            height: Math.max(1, chartSurface.height - y
                             - root.plotLabelHeight - root.themeColors.spaceXs * 2
                             - xAxisTitle.height - floatingResultSummary.height
                             - staleNotice.height)
            clip: true
            visible: root.controller.hasResult

            Rectangle {
                anchors.fill: parent
                color: root.themeColors.colorSurfaceRaised
                border.color: root.themeColors.colorBorder
                border.width: root.themeColors.borderWidth
            }

            Repeater {
                model: root.plotView.grid ? root.plotView.x_minor_ticks : []
                delegate: Rectangle {
                    objectName: "filmSpectrumMinorGridXLine"
                    required property real modelData
                    x: (modelData - root.plotView.x_min)
                       / (root.plotView.x_max - root.plotView.x_min)
                       * plotViewport.width
                    y: 0
                    width: 1
                    height: plotViewport.height
                    color: root.themeColors.colorBorder
                    opacity: 0.22
                }
            }
            Repeater {
                model: root.plotView.grid ? root.plotView.y_minor_ticks : []
                delegate: Rectangle {
                    objectName: "filmSpectrumMinorGridYLine"
                    required property real modelData
                    x: 0
                    y: (root.plotView.y_max - modelData)
                       / (root.plotView.y_max - root.plotView.y_min)
                       * plotViewport.height
                    width: plotViewport.width
                    height: 1
                    color: root.themeColors.colorBorder
                    opacity: 0.22
                }
            }
            Repeater {
                model: root.plotView.grid ? root.plotView.x_ticks : []
                delegate: Rectangle {
                    objectName: "filmSpectrumMajorGridXLine"
                    required property real modelData
                    x: (modelData - root.plotView.x_min)
                       / (root.plotView.x_max - root.plotView.x_min)
                       * plotViewport.width
                    y: 0
                    width: 1
                    height: plotViewport.height
                    color: root.themeColors.colorBorder
                    opacity: 0.62
                }
            }
            Repeater {
                model: root.plotView.grid ? root.plotView.y_ticks : []
                delegate: Rectangle {
                    objectName: "filmSpectrumMajorGridYLine"
                    required property real modelData
                    x: 0
                    y: (root.plotView.y_max - modelData)
                       / (root.plotView.y_max - root.plotView.y_min)
                       * plotViewport.height
                    width: plotViewport.width
                    height: 1
                    color: root.themeColors.colorBorder
                    opacity: 0.62
                }
            }

            Item {
                id: curveLayer
                objectName: "filmSpectrumCurveTransformLayer"
                x: root.curveTranslationX
                y: root.curveTranslationY
                width: plotViewport.width
                height: plotViewport.height
                scale: root.curveScale
                transformOrigin: Item.TopLeft
                clip: true
                // Render vectors directly. A redundant software offscreen
                // layer shifts curves vertically at fractional DPR in Qt 6.12.
                // The viewport and this item already own clipping.

                Repeater {
                    objectName: "filmSpectrumCurveSeriesRepeater"
                    model: root.plotSnapshot.series
                    delegate: Item {
                        id: curveSeries
                        required property var modelData
                        required property int index
                        objectName: "filmSpectrumVectorSeries_" + index
                        anchors.fill: parent

                        Repeater {
                            objectName: "filmSpectrumCurveSegmentsRepeater"
                            model: curveSeries.modelData.segments
                                   && curveSeries.modelData.segments.length
                                   ? curveSeries.modelData.segments
                                   : [curveSeries.modelData.points || []]
                            delegate: Shape {
                                id: curveShape
                                required property var modelData
                                required property int index
                                objectName: "filmSpectrumVectorCurve_" + curveSeries.index
                                             + (index === 0 ? "" : "_segment_" + index)
                                anchors.fill: parent
                                preferredRendererType: Shape.GeometryRenderer
                                asynchronous: false

                                ShapePath {
                                    strokeColor: curveSeries.modelData.color
                                    strokeWidth: 1.8
                                    strokeStyle: curveSeries.modelData.dash === "solid"
                                                 ? ShapePath.SolidLine : ShapePath.DashLine
                                    dashPattern: curveSeries.modelData.dash === "dot" ? [1, 2] : [4, 2]
                                    fillColor: "transparent"
                                    pathHints: ShapePath.PathLinear
                                    PathPolyline {
                                        path: root.mapPoints(curveShape.modelData)
                                    }
                                }
                            }
                        }
                    }
                }
            }

            Rectangle {
                objectName: "filmSpectrumCursor"
                visible: interactions.cursorVisible
                x: interactions.cursorFraction * plotViewport.width
                y: 0
                width: 1
                height: plotViewport.height
                color: root.themeColors.colorFocusRing
                opacity: 0.95
                z: 10
            }
        }

        Repeater {
            model: root.controller.hasResult ? root.plotView.x_ticks : []
            delegate: Label {
                required property real modelData
                x: root.plotX(modelData) - width / 2
                y: plotViewport.y + plotViewport.height + root.themeColors.spaceXs
                width: Math.max(42, implicitWidth)
                text: root.formatNumber(modelData)
                color: root.themeColors.colorTextMuted
                horizontalAlignment: Text.AlignHCenter
                font.family: root.themeColors.fontFamily
                font.pointSize: root.themeColors.fontSizeSmall
            }
        }
        Repeater {
            model: root.controller.hasResult ? root.plotView.y_ticks : []
            delegate: Label {
                required property real modelData
                x: 0
                y: root.plotY(modelData) - height / 2
                width: plotViewport.x - root.themeColors.spaceXs
                text: root.formatNumber(modelData * 100) + "%"
                color: root.themeColors.colorTextMuted
                horizontalAlignment: Text.AlignRight
                verticalAlignment: Text.AlignVCenter
                font.family: root.themeColors.fontFamily
                font.pointSize: root.themeColors.fontSizeSmall
            }
        }

        Label {
            id: xAxisTitle
            objectName: "filmSpectrumXAxisTitle"
            visible: root.controller.hasResult
            x: plotViewport.x
            y: plotViewport.y + plotViewport.height + root.plotLabelHeight
               + root.themeColors.spaceXs * 2
            width: Math.max(1, Math.min(plotViewport.width,
                          root.floatingControlsRightEdge - chartSurface.x
                          - plotViewport.x))
            text: "波长（真空，nm）"
            color: root.themeColors.colorTextMuted
            horizontalAlignment: Text.AlignHCenter
            font.family: root.themeColors.fontFamily
            font.pointSize: root.themeColors.fontSizeSmall
        }
        Label {
            objectName: "filmSpectrumYAxisTitle"
            visible: root.controller.hasResult
            x: 16 - width / 2
            y: plotViewport.y + plotViewport.height / 2 - height / 2
            width: plotViewport.height
            height: 32
            rotation: -90
            text: "反射 / 透射 / 吸收（%）\n或相对光谱（%）"
            color: root.themeColors.colorTextMuted
            horizontalAlignment: Text.AlignHCenter
            font.family: root.themeColors.fontFamily
            font.pointSize: root.themeColors.fontSizeSmall
        }

        Rectangle {
            id: legendSurface
            objectName: "filmSpectrumLegendSurface"
            x: legend.x
            y: legend.y
            width: legend.width
            height: legend.height
            visible: legend.visible
            z: 2
            color: root.themeColors.colorSurfaceRaised
            opacity: 0.94
            radius: root.themeColors.radiusSm
            border.color: root.themeColors.colorBorder
            MouseArea {
                anchors.fill: parent
                acceptedButtons: Qt.LeftButton
                // The legend is presentation, not a draggable plot surface.
            }
        }
        Flow {
            id: legend
            objectName: "filmSpectrumVectorLegend"
            anchors.left: plotViewport.left
            anchors.top: plotViewport.top
            anchors.leftMargin: root.themeColors.spaceXs
            anchors.topMargin: plotViewport.width < 400
                               ? floatingControls.height + root.themeColors.spaceXs
                               : root.themeColors.spaceXs
            readonly property real availableWidth: Math.max(1, plotViewport.width < 400
                          ? Math.min(plotViewport.width,
                                     root.floatingControlsRightEdge - chartSurface.x - plotViewport.x)
                          : Math.min(plotViewport.width - floatingControls.width
                                     - root.themeColors.spaceMd,
                                     root.floatingControlsRightEdge - chartSurface.x
                                     - plotViewport.x - floatingControls.width
                                     - root.themeColors.spaceMd) - root.themeColors.spaceXs * 2)
            width: Math.min(availableWidth, root.legendNaturalWidth)
            height: visible ? implicitHeight : 0
            spacing: root.themeColors.spaceXs
            visible: root.controller.hasResult
            z: 3
            Repeater {
                objectName: "filmSpectrumLegendRepeater"
                model: root.plotSnapshot.series
                delegate: RowLayout {
                    id: legendItem
                    required property var modelData
                    readonly property string legendToken:
                        legendItem.modelData.channel + "_" + legendItem.modelData.polarization
                    objectName: "filmSpectrumLegendItem_" + legendItem.legendToken
                    width: Math.min(implicitWidth, legend.width)
                    height: implicitHeight
                    spacing: root.themeColors.spaceXs
                    Rectangle {
                        objectName: "filmSpectrumLegendSwatch_" + legendItem.legendToken
                        width: root.themeColors.spaceMd
                        height: 2
                        color: legendItem.modelData.color
                    }
                    Label {
                        objectName: "filmSpectrumLegendLabel_" + legendItem.legendToken
                        text: legendItem.modelData.label
                        color: root.themeColors.colorTextMuted
                        elide: Text.ElideRight
                        Layout.maximumWidth: Math.max(1, legend.width
                                                     - root.themeColors.spaceMd - root.themeColors.spaceXs)
                        font.family: root.themeColors.fontFamily
                        font.pointSize: root.themeColors.fontSizeSmall
                    }
                }
            }
        }

        Label {
            objectName: "filmSpectrumEmptyState"
            anchors.centerIn: parent
            width: parent.width - root.themeColors.spaceXl * 2
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            visible: !root.controller.hasResult
            text: root.controller.busy ? "正在计算…"
                  : root.controller.hasDesign ? "设置光谱参数后，点击计算并绘图。"
                  : "加载膜系 JSON 文件即可进行光谱仿真。"
            color: root.themeColors.colorTextMuted
            font.family: root.themeColors.fontFamily
            font.pointSize: root.themeColors.fontSizeBody
        }

        Rectangle {
            id: floatingResultSummary
            objectName: "filmSpectrumFloatingResultSummary"
            anchors.left: plotViewport.left
            anchors.bottom: parent.bottom
            anchors.bottomMargin: staleNotice.height
            width: plotViewport.width
            height: visible ? summaryRow.implicitHeight + root.themeColors.spaceXs * 2 : 0
            visible: root.controller.hasResult
            z: 4
            radius: root.themeColors.radiusSm
            color: root.themeColors.colorSurfaceRaised
            opacity: 0.94
            border.color: root.themeColors.colorBorder

            RowLayout {
                id: summaryRow
                anchors.fill: parent
                anchors.leftMargin: root.themeColors.spaceSm
                anchors.rightMargin: root.themeColors.spaceSm
                anchors.topMargin: root.themeColors.spaceXs
                anchors.bottomMargin: root.themeColors.spaceXs
                spacing: root.themeColors.spaceSm
                Label {
                    text: "结果说明"
                    color: root.themeColors.colorText
                    font.family: root.themeColors.fontFamily
                    font.pointSize: root.themeColors.fontSizeSmall
                    font.weight: root.themeColors.fontWeightEmphasis
                }
                Label {
                    id: summaryText
                    objectName: "filmSpectrumFloatingResultSummaryText"
                    readonly property string accessibleDataStatus:
                        String(root.plotSnapshot.dataStatusText || "")
                    Layout.fillWidth: true
                    Layout.preferredHeight: root.plotLabelHeight
                                            * (root.plotSnapshot.dataStatus === "partial" ? 2 : 1)
                    wrapMode: Text.NoWrap
                    text: {
                        const readout = String(root.controller.cursorReadout || "")
                        const hint = readout.length > 0 ? readout
                                : "滚轮缩放 · 放大后拖动平移"
                        const dataStatus = root.plotSnapshot.dataStatus === "partial"
                                           ? "部分采样无效 · 曲线已分段" : ""
                        return dataStatus.length > 0 ? dataStatus + "\n" + hint : hint
                    }
                    Accessible.description: accessibleDataStatus
                    Accessible.name: text
                    HoverHandler { id: summaryHover }
                    ToolTip.visible: summaryHover.hovered
                    ToolTip.text: summaryText.text
                    color: root.themeColors.colorTextMuted
                    font.family: root.themeColors.fontFamily
                    font.pointSize: root.themeColors.fontSizeSmall
                    maximumLineCount: 2
                    elide: Text.ElideRight
                }
            }
        }
        Label {
            objectName: "filmSpectrumNoCurvesState"
            anchors.centerIn: plotViewport
            width: plotViewport.width - root.themeColors.spaceXl * 2
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            visible: root.controller.hasResult && !root.plotVectorReady
            text: root.controller.plotSettings.curves.length === 0
                  ? "未选择显示曲线。可在绘图参数中选择 R、T 或 A。"
                  : String(root.plotSnapshot.dataStatusText || "所选曲线没有有效采样数据。")
            color: root.themeColors.colorTextMuted
            font.family: root.themeColors.fontFamily
            font.pointSize: root.themeColors.fontSizeBody
        }
        Label {
            id: staleNotice
            objectName: "filmSpectrumStaleNotice"
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: visible ? root.plotLabelHeight + root.themeColors.spaceXs * 2 : 0
            visible: root.controller.hasResult && root.controller.stale
            text: "当前曲线对应旧参数；重新计算后才能导出。"
            color: root.themeColors.colorWarning
            horizontalAlignment: Text.AlignHCenter
            font.family: root.themeColors.fontFamily
            font.pointSize: root.themeColors.fontSizeSmall
            background: Rectangle {
                color: root.themeColors.colorSurface
                opacity: root.themeColors.opacityScrim
                radius: root.themeColors.radiusSm
            }
        }
    }

    MouseArea {
        id: interactions
        objectName: "filmSpectrumInteractions"
        x: chartSurface.x + plotViewport.x
        y: chartSurface.y + plotViewport.y
        width: plotViewport.width
        height: plotViewport.height
        z: 0.5
        enabled: root.plotVectorReady
        hoverEnabled: true
        preventStealing: true
        acceptedButtons: Qt.LeftButton
        property bool cursorVisible: false
        property bool dragging: false
        property bool dragMoved: false
        property real cursorFraction: 0
        property real pressX: 0
        property real pressY: 0
        property real lastX: 0
        property real lastY: 0
        readonly property real panDragThreshold: Math.max(4, root.themeColors.spaceXs)

        onEnabledChanged: {
            if (!enabled) {
                dragging = false
                cursorVisible = false
                root.controller.clearPlotCursor()
            }
        }

        function insidePlot(px, py) {
            return px >= 0 && px <= width && py >= 0 && py <= height
        }
        function updateCursor(px, py) {
            if (!insidePlot(px, py) || plotViewport.width <= 0 || plotViewport.height <= 0) {
                cursorVisible = false
                root.controller.clearPlotCursor()
                return
            }
            const xPart = px / width
            cursorFraction = xPart
            cursorVisible = true
            const view = root.plotView
            root.controller.setPlotCursor(view.x_min + xPart * (view.x_max - view.x_min))
        }
        onPressed: function(mouse) {
            if (!insidePlot(mouse.x, mouse.y)) {
                mouse.accepted = false
                return
            }
            dragging = true
            dragMoved = false
            pressX = mouse.x
            pressY = mouse.y
            lastX = mouse.x
            lastY = mouse.y
            updateCursor(mouse.x, mouse.y)
        }
        onPositionChanged: function(mouse) {
            if (dragging && (mouse.buttons & Qt.LeftButton)) {
                if (!dragMoved
                        && Math.hypot(mouse.x - pressX, mouse.y - pressY) >= panDragThreshold)
                    dragMoved = true
                if (dragMoved && insidePlot(mouse.x, mouse.y)) {
                    root.controller.panPlot(
                        (mouse.x - lastX) / plotViewport.width,
                        (mouse.y - lastY) / plotViewport.height)
                }
            }
            // Leaving the axes stops movement. Re-entry resumes from the
            // nearest boundary, without replaying the outside pointer delta.
            lastX = Math.max(0, Math.min(width, mouse.x))
            lastY = Math.max(0, Math.min(height, mouse.y))
            updateCursor(mouse.x, mouse.y)
        }
        onReleased: function(mouse) {
            dragging = false
            updateCursor(mouse.x, mouse.y)
        }
        onCanceled: {
            dragging = false
            dragMoved = false
            cursorVisible = false
            root.controller.clearPlotCursor()
        }
        onDoubleClicked: function(mouse) {
            if (dragMoved)
                return
            dragging = false
            cursorVisible = false
            root.controller.clearPlotCursor()
            root.openPlotSettingsRequested()
        }
        onExited: {
            if (!dragging) {
                cursorVisible = false
                root.controller.clearPlotCursor()
            }
        }
        onWheel: function(wheel) {
            if (!insidePlot(wheel.x, wheel.y) || plotViewport.width <= 0 || plotViewport.height <= 0)
                return
            const xPart = wheel.x / width
            const yPart = 1 - wheel.y / height
            root.controller.zoomPlot(wheel.angleDelta.y > 0 ? 0.8 : 1.25, xPart, yPart)
            updateCursor(wheel.x, wheel.y)
            wheel.accepted = true
        }
    }

    Rectangle {
        id: floatingControls
        objectName: "filmSpectrumFloatingControls"
        anchors.top: chartSurface.top
        anchors.topMargin: plotViewport.y + root.themeColors.spaceXs
        x: Math.max(chartSurface.x + plotViewport.x + root.themeColors.spaceSm,
                    Math.min(chartSurface.x + plotViewport.x + plotViewport.width
                             - width - root.themeColors.spaceSm,
                             root.floatingControlsRightEdge - width - root.themeColors.spaceSm))
        width: controlsColumn.implicitWidth + root.themeColors.spaceSm * 2
        height: controlsColumn.implicitHeight + root.themeColors.spaceSm * 2
        radius: root.themeColors.radiusSm
        color: root.themeColors.colorSurface
        border.color: root.themeColors.colorBorder
        opacity: 0.98
        z: 5

        ColumnLayout {
            id: controlsColumn
            anchors.fill: parent
            anchors.margins: root.themeColors.spaceSm
            spacing: root.themeColors.spaceXs

            RowLayout {
                Layout.fillWidth: true
                spacing: root.themeColors.spaceXs

                ThemeControls.ThemeButton {
                    objectName: "filmSpectrumZoomInButton"
                    text: "放大"
                    Layout.preferredWidth: 36
                    Layout.minimumWidth: 36
                    Layout.maximumWidth: 36
                    Layout.preferredHeight: 36
                    topPadding: 0
                    bottomPadding: 0
                    leftPadding: 0
                    rightPadding: 0
                    enabled: root.controller.hasResult
                    Accessible.name: "放大图表"
                    ToolTip.visible: hovered
                    ToolTip.text: "放大"
                    contentItem: ZoomActionContent {
                        objectName: "filmSpectrumZoomInContent"
                        mark: "+"
                    }
                    onClicked: root.controller.zoomPlot(0.75, 0.5, 0.5)
                }
                ThemeControls.ThemeButton {
                    objectName: "filmSpectrumZoomOutButton"
                    text: "缩小"
                    Layout.preferredWidth: 36
                    Layout.minimumWidth: 36
                    Layout.maximumWidth: 36
                    Layout.preferredHeight: 36
                    topPadding: 0
                    bottomPadding: 0
                    leftPadding: 0
                    rightPadding: 0
                    enabled: root.controller.hasResult
                    Accessible.name: "缩小图表"
                    ToolTip.visible: hovered
                    ToolTip.text: "缩小"
                    contentItem: ZoomActionContent {
                        objectName: "filmSpectrumZoomOutContent"
                        mark: "−"
                    }
                    onClicked: root.controller.zoomPlot(1.33, 0.5, 0.5)
                }
                ThemeControls.ThemeButton {
                    objectName: "filmSpectrumResetButton"
                    text: "复位"
                    Layout.preferredWidth: 36
                    Layout.minimumWidth: 36
                    Layout.maximumWidth: 36
                    Layout.preferredHeight: 36
                    topPadding: 0
                    bottomPadding: 0
                    leftPadding: 0
                    rightPadding: 0
                    enabled: root.controller.hasResult
                    Accessible.name: "复位图表"
                    ToolTip.visible: hovered
                    ToolTip.text: "复位"
                    contentItem: ZoomActionContent {
                        objectName: "filmSpectrumResetContent"
                        mark: "↺"
                    }
                    onClicked: root.controller.resetPlotView()
                }
            }

        }
    }
}
