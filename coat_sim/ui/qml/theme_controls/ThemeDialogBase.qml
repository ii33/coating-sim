import QtQuick
import QtQuick.Controls

// Optional visual base for dialogs. Dialog modality, buttons, size, and
// business behavior remain explicit in each consuming window.
Dialog {
    id: dialog
    readonly property var themeTokens:
        typeof Theme.tokenValues !== "undefined" ? Theme.tokenValues : Theme.snapshot

    contentItem: Item {
        implicitWidth: childrenRect.width
        implicitHeight: childrenRect.height
        Accessible.role: Accessible.Dialog
        Accessible.name: dialog.title
    }

    padding: dialog.themeTokens.spaceLg
    font.family: dialog.themeTokens.fontFamily
    font.pointSize: dialog.themeTokens.fontSizeBody
    font.weight: dialog.themeTokens.fontWeightNormal

    palette.window: dialog.themeTokens.colorWindow
    palette.windowText: dialog.themeTokens.colorText
    palette.base: dialog.themeTokens.colorSurface
    palette.alternateBase: dialog.themeTokens.colorSurfaceRaised
    palette.text: dialog.themeTokens.colorText
    palette.button: dialog.themeTokens.colorSurfaceRaised
    palette.buttonText: dialog.themeTokens.colorText
    palette.highlight: dialog.themeTokens.colorSelection
    palette.highlightedText: dialog.themeTokens.colorText
    palette.placeholderText: dialog.themeTokens.colorTextMuted
    palette.mid: dialog.themeTokens.colorBorder
    palette.dark: dialog.themeTokens.colorBorderStrong

    header: Label {
        text: dialog.title
        visible: text.length > 0
        leftPadding: dialog.themeTokens.spaceLg
        rightPadding: dialog.themeTokens.spaceLg
        topPadding: dialog.themeTokens.spaceMd
        bottomPadding: dialog.themeTokens.spaceMd
        color: dialog.themeTokens.colorText
        font.family: dialog.themeTokens.fontFamily
        font.pointSize: dialog.themeTokens.fontSizeHeading
        font.weight: dialog.themeTokens.fontWeightEmphasis
        background: Rectangle {
            color: dialog.themeTokens.colorSurfaceRaised
            border.color: dialog.themeTokens.colorBorder
            border.width: dialog.themeTokens.borderWidth
            radius: dialog.themeTokens.radiusMd
        }
    }

    background: Rectangle {
        color: dialog.themeTokens.colorSurface
        radius: dialog.themeTokens.radiusMd
        border.width: dialog.themeTokens.borderWidth
        border.color: dialog.themeTokens.colorBorder
    }
}



