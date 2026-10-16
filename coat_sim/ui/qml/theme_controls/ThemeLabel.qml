import QtQuick
import QtQuick.Controls

// Semantic text for ordinary interface labels. Scientific annotations should
// remain owned by plot components so data colors keep their scientific meaning.
Label {
    id: label
    readonly property var themeTokens:
        typeof Theme.tokenValues !== "undefined" ? Theme.tokenValues : Theme.snapshot

    property bool muted: false
    property bool heading: false

    font.family: label.themeTokens.fontFamily
    font.pointSize: heading ? label.themeTokens.fontSizeHeading
                            : label.themeTokens.fontSizeBody
    font.weight: heading ? label.themeTokens.fontWeightEmphasis
                         : label.themeTokens.fontWeightNormal
    color: !enabled ? label.themeTokens.colorDisabledText
                    : muted ? label.themeTokens.colorTextMuted
                            : label.themeTokens.colorText
    palette.window: label.themeTokens.colorWindow
    palette.windowText: label.themeTokens.colorText
    palette.base: label.themeTokens.colorSurface
    palette.alternateBase: label.themeTokens.colorSurfaceRaised
    palette.text: label.themeTokens.colorText
    palette.button: label.themeTokens.colorSurfaceRaised
    palette.buttonText: label.themeTokens.colorText
    palette.highlight: label.themeTokens.colorSelection
    palette.highlightedText: label.themeTokens.colorText
    palette.placeholderText: label.themeTokens.colorTextMuted
    palette.mid: label.themeTokens.colorBorder
    palette.dark: label.themeTokens.colorBorderStrong
    Accessible.name: label.text
}
