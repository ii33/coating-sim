import QtQuick

// One sizing contract is shared by film and expression headers and delegates.
QtObject {
    id: metrics
    required property var themeTokens
    property real availableWidth: 0
    property real horizontalPadding: 0
    property real columnGap: 0
    property bool compact: false

    // Header text and editable cells share these widths. Use the actual themed
    // font metrics so CJK labels remain readable after font scaling.
    property TextMetrics symbolHeaderMetrics: TextMetrics {
        text: "符号"
        font.family: metrics.themeTokens.fontFamily
        font.pointSize: metrics.themeTokens.fontSizeBody
    }
    property TextMetrics thicknessHeaderMetrics: TextMetrics {
        text: "光学厚度"
        font.family: metrics.themeTokens.fontFamily
        font.pointSize: metrics.themeTokens.fontSizeBody
    }
    property TextMetrics groupHeaderMetrics: TextMetrics {
        text: "分组"
        font.family: metrics.themeTokens.fontFamily
        font.pointSize: metrics.themeTokens.fontSizeBody
    }
    readonly property real indexWidth: Math.max(compact ? 28 : 40,
                                                Math.ceil(symbolHeaderMetrics.width))
    readonly property real qwWidth: Math.max(compact ? 64 : 78,
                                             Math.ceil(thicknessHeaderMetrics.width))
    readonly property real nmWidth: Math.max(compact ? 70 : 96,
                                             Math.ceil(thicknessHeaderMetrics.width))
    readonly property real groupWidth: Math.max(compact ? 50 : 64,
                                               Math.ceil(groupHeaderMetrics.width) + 8)
    readonly property real insertWidth: compact ? 30 : 28
    readonly property real deleteWidth: compact ? 30 : 26
    readonly property real flexibleWidth: Math.max(0, availableWidth - 2 * horizontalPadding
        - 7 * columnGap - indexWidth - groupWidth - qwWidth - nmWidth - insertWidth - deleteWidth)
    readonly property real materialWidth: flexibleWidth * 0.52
    readonly property real datasetWidth: flexibleWidth - materialWidth
}
