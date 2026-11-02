pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Read-only projection of existing live frames. No optical evaluation occurs here.
ColumnLayout {
    id: chart
    required property var controller
    required property var manufacturingController
    required property var themeTokens
    readonly property bool repair: controller.searchMode === "automatic_repair"
    readonly property bool hasFrame: repair ? manufacturingController.hasLiveChartFrame : controller.hasReductionFeedback
    readonly property var rows: !hasFrame ? [] : (repair ? manufacturingController.liveLayerRows : controller.reductionFeedbackLayers)
    readonly property string currentMode: repair ? manufacturingController.liveTotals.mode : ""
    readonly property bool showingTrial: currentMode === "preview" || currentMode === "frozen_preview"
    readonly property string currentLegend: showingTrial ? "当前试探" : (currentMode === "applied" ? "已应用回看" : (currentMode === "source_unchanged" ? "原设计回看" : "最佳回看"))
    readonly property real scaleNm: repair ? manufacturingController.liveBarScaleNm : controller.reductionFeedbackBarScaleNm
    spacing: themeTokens.spaceXs

    Label {
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        text: "逐层物理厚度（nm）"
        color: chart.themeTokens.colorText
        font.pointSize: chart.themeTokens.fontSizeHeading
        font.weight: chart.themeTokens.fontWeightEmphasis
        wrapMode: Text.WordWrap
    }
    Label {
        objectName: "filmLayerThicknessLegend"
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        wrapMode: Text.WordWrap
        textFormat: Text.RichText
        font.pointSize: chart.themeTokens.fontSizeSmall
        text: "<font color='" + chart.themeTokens.colorTextMuted + "'>■ 原始</font>  "
              + (chart.repair ? "<font color='" + chart.themeTokens.colorWarning + "'>■ " + chart.currentLegend + "</font>  " : "")
              + "<font color='" + chart.themeTokens.colorAccent + "'>■ 核验最佳</font>"
    }
    Label {
        objectName: "filmLayerThicknessSummary"
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        visible: chart.hasFrame || (chart.repair ? chart.manufacturingController.busy : chart.controller.busy)
        text: chart.repair ? (chart.showingTrial ? "当前试探不代表最佳。" : "") + chart.manufacturingController.liveSummary + "\n原设计：" + chart.manufacturingController.liveTotals.original
              + "；" + chart.manufacturingController.liveTotals.current_label + "：" + chart.manufacturingController.liveTotals.current
              + "；最佳：" + chart.manufacturingController.liveTotals.best + "。"
              : chart.controller.reductionFeedbackSummary
        color: chart.themeTokens.colorText
        wrapMode: Text.WordWrap
        font.pointSize: chart.themeTokens.fontSizeSmall
        HoverHandler { id: summaryHover }
        ToolTip.visible: summaryHover.hovered
        ToolTip.text: text
    }
    Label {
        objectName: "filmLayerThicknessOpticalSummary"
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        visible: chart.repair && chart.hasFrame
        text: chart.manufacturingController.liveOpticalSummary
        color: chart.themeTokens.colorTextMuted
        wrapMode: Text.WordWrap
        font.pointSize: chart.themeTokens.fontSizeSmall
        HoverHandler { id: opticalHover }
        ToolTip.visible: opticalHover.hovered
        ToolTip.text: text
    }
    Item {
        Layout.fillWidth: true
        Layout.fillHeight: true
        Layout.minimumHeight: 90
        Label {
            anchors.centerIn: parent
            visible: !chart.hasFrame
            text: (chart.repair ? chart.manufacturingController.busy : chart.controller.busy)
                  ? "等待实时反馈；未知厚度不按 0 显示。" : "尚未开始；运行后显示逐层厚度。"
            color: chart.themeTokens.colorTextMuted
        }
        Item {
            id: plotArea
            anchors.fill: parent
            visible: chart.hasFrame
            readonly property real axisWidth: 56
            readonly property real bottomSpace: 30
            Repeater {
                model: 5
                delegate: Item {
                    required property int index
                    x: 0; width: parent.width
                    y: (plotArea.height - plotArea.bottomSpace - 8) * index / 4
                    Label {
                        width: plotArea.axisWidth - 5
                        horizontalAlignment: Text.AlignRight
                        y: -6
                        text: Number((chart.scaleNm * (1 - parent.index / 4)).toPrecision(4))
                        color: chart.themeTokens.colorTextMuted
                        font.pointSize: chart.themeTokens.fontSizeSmall
                    }
                    Rectangle { x: plotArea.axisWidth; width: plotArea.width - x; height: 1; color: chart.themeTokens.colorBorder }
                }
            }
            Flickable {
                id: viewport
                objectName: "filmLayerThicknessViewport"
                x: parent.axisWidth; width: parent.width - x; height: parent.height
                contentWidth: Math.max(width, chart.rows.length * 58)
                contentHeight: height
                flickableDirection: Flickable.HorizontalFlick
                clip: true
                ScrollBar.horizontal: ScrollBar { policy: viewport.contentWidth > viewport.width ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff }
                Row {
                    height: viewport.height - 8
                    Repeater {
                        model: chart.rows
                        delegate: Item {
                            id: column
                            required property var modelData
                            width: 58; height: viewport.height - 8
                            readonly property real barBase: height - 30
                            readonly property real plotHeight: Math.max(0, barBase)
                            readonly property var values: [modelData.original_nm,
                                chart.repair ? (modelData.current_deleted ? 0 : modelData.current_nm) : null,
                                modelData.best_deleted ? 0 : modelData.best_nm]
                            objectName: "filmLayerThicknessLayer_" + modelData.row_id
                            Repeater {
                                model: 3
                                delegate: Rectangle {
                                    required property int index
                                    readonly property var value: column.values[index]
                                    visible: value !== null && value !== undefined && Number.isFinite(Number(value))
                                    objectName: "filmLayerThicknessBar_" + index + "_" + column.modelData.row_id
                                    x: 5 + index * 16; width: 13
                                    height: !visible || chart.scaleNm <= 0 || Number(value) === 0 ? 0 : Math.max(2, column.plotHeight * Number(value) / chart.scaleNm)
                                    y: column.barBase - height
                                    color: index === 0 ? chart.themeTokens.colorTextMuted : (index === 1 ? chart.themeTokens.colorWarning : chart.themeTokens.colorAccent)
                                }
                            }
                            Label {
                                anchors.top: parent.top; anchors.topMargin: column.barBase + 3
                                width: parent.width; horizontalAlignment: Text.AlignHCenter
                                text: (column.modelData.side === "back" ? "背" : (column.modelData.side === "front" ? "正" : "?"))
                                      + (column.modelData.is_new === true ? "新" : "")
                                      + (column.modelData.index === null || column.modelData.index === undefined ? "?"
                                         : (chart.repair ? column.modelData.index : column.modelData.index + 1))
                                color: chart.themeTokens.colorTextMuted
                                font.pointSize: chart.themeTokens.fontSizeSmall
                            }
                            MouseArea {
                                anchors.fill: parent; hoverEnabled: true
                                ToolTip.visible: containsMouse
                                ToolTip.delay: 300
                                ToolTip.text: column.modelData.label + "\n原始：" + column.modelData.original
                                    + (chart.repair ? "\n" + chart.currentLegend + "：" + (column.modelData.current_deleted ? "0 nm（已删除）" : column.modelData.current) : "")
                                    + "\n完整核验最佳：" + (column.modelData.best_deleted ? "0 nm（已删除）" : column.modelData.best)
                                    + (chart.repair ? "\n" + column.modelData.status : "")
                            }
                        }
                    }
                }
            }
        }
    }
}
