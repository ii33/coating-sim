import QtQuick
import QtQuick.Controls

// Small themed wrapper around Controls.Button. Keep the native Button input,
// keyboard, and accessibility behavior; only provide semantic colors/metrics.
Button {
    id: control
    readonly property var themeTokens:
        typeof Theme.tokenValues !== "undefined" ? Theme.tokenValues : Theme.snapshot

    // Primary actions use the accent palette. Secondary actions use a surface
    // and a theme-controlled hover/pressed overlay.
    property bool primary: false

    function tint(value, alpha) {
        const source = Qt.color(value)
        return Qt.rgba(source.r, source.g, source.b, alpha)
    }

    focusPolicy: Qt.StrongFocus
    font.family: control.themeTokens.fontFamily
    font.pointSize: control.themeTokens.fontSizeBody
    font.weight: control.themeTokens.fontWeightEmphasis
    topPadding: control.themeTokens.spaceSm
    bottomPadding: control.themeTokens.spaceSm
    leftPadding: control.themeTokens.spaceMd
    rightPadding: control.themeTokens.spaceMd
    implicitHeight: Math.max(control.themeTokens.controlHeightMd,
                             contentItem.implicitHeight + topPadding + bottomPadding)

    palette.window: control.themeTokens.colorWindow
    palette.windowText: control.themeTokens.colorText
    palette.base: control.themeTokens.colorSurface
    palette.alternateBase: control.themeTokens.colorSurfaceRaised
    palette.text: control.themeTokens.colorText
    palette.button: control.themeTokens.colorSurfaceRaised
    palette.buttonText: control.themeTokens.colorText
    palette.highlight: control.themeTokens.colorSelection
    palette.highlightedText: control.themeTokens.colorText
    palette.placeholderText: control.themeTokens.colorTextMuted
    palette.mid: control.themeTokens.colorBorder
    palette.dark: control.themeTokens.colorBorderStrong

    Accessible.name: control.text

    contentItem: Text {
        text: control.text
        font: control.font
        color: !control.enabled ? control.themeTokens.colorDisabledText
                               : control.primary || control.checked
                                 ? control.themeTokens.colorTextOnAccent
                                 : control.themeTokens.colorText
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
        wrapMode: Text.NoWrap
    }

    background: Rectangle {
        objectName: control.objectName + "_background"
        id: surface
        radius: control.themeTokens.radiusMd
        color: !control.enabled ? control.themeTokens.colorDisabledSurface
              : control.primary || control.checked
                ? (control.down ? control.themeTokens.colorAccentPressed
                   : control.hovered ? control.themeTokens.colorAccentHover
                                     : control.themeTokens.colorAccent)
                : control.flat
                  ? (control.down ? control.tint(control.themeTokens.colorAccentPressed,
                                                control.themeTokens.opacityPressed)
                     : control.hovered ? control.tint(control.themeTokens.colorAccentHover,
                                                      control.themeTokens.opacityHover)
                                       : "transparent")
                  : control.themeTokens.colorSurfaceRaised
        border.width: control.activeFocus ? control.themeTokens.focusRingWidth
                                          : control.themeTokens.borderWidth
        border.color: control.activeFocus ? control.themeTokens.colorFocusRing
                     : control.hovered && control.enabled ? control.themeTokens.colorBorderStrong
                     : control.flat ? "transparent" : control.themeTokens.colorBorder

        Rectangle {
            anchors.fill: parent
            radius: parent.radius
            color: control.themeTokens.colorAccent
            opacity: !control.enabled || control.primary || control.flat || control.checked ? 0
                   : control.down ? control.themeTokens.opacityPressed
                   : control.hovered ? control.themeTokens.opacityHover : 0
        }
    }
}
