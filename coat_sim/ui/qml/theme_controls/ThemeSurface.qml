import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Panel surface with caller-owned vertical content and theme-driven spacing.
Control {
    id: surface
    readonly property var themeTokens:
        typeof Theme.tokenValues !== "undefined" ? Theme.tokenValues : Theme.snapshot

    default property alias contentData: content.data
    property bool raised: false
    property bool bordered: true
    property real contentSpacing: surface.themeTokens.spaceSm

    padding: surface.themeTokens.spaceMd
    implicitWidth: content.implicitWidth + leftPadding + rightPadding
    implicitHeight: content.implicitHeight + topPadding + bottomPadding

    palette.window: surface.themeTokens.colorWindow
    palette.windowText: surface.themeTokens.colorText
    palette.base: surface.themeTokens.colorSurface
    palette.alternateBase: surface.themeTokens.colorSurfaceRaised
    palette.text: surface.themeTokens.colorText
    palette.button: surface.themeTokens.colorSurfaceRaised
    palette.buttonText: surface.themeTokens.colorText
    palette.highlight: surface.themeTokens.colorSelection
    palette.highlightedText: surface.themeTokens.colorText
    palette.placeholderText: surface.themeTokens.colorTextMuted
    palette.mid: surface.themeTokens.colorBorder
    palette.dark: surface.themeTokens.colorBorderStrong

    contentItem: ColumnLayout {
        id: content
        spacing: surface.contentSpacing
    }

    background: Rectangle {
        objectName: surface.objectName + "_background"
        color: surface.raised ? surface.themeTokens.colorSurfaceRaised
                              : surface.themeTokens.colorSurface
        radius: surface.themeTokens.radiusMd
        border.width: surface.bordered ? surface.themeTokens.borderWidth : 0
        border.color: surface.themeTokens.colorBorder
    }
}
