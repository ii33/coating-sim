import QtQuick
import QtQuick.Shapes

// Vector-only double chevron for sidebar actions. The inherited Button keeps
// the normal hit target, keyboard behavior, focus ring, and accessibility.
ThemeButton {
    id: control

    // One of: left, right, up, down. This points in the direction of the
    // action (for example, a right-side rail collapses to the right).
    property string direction: "left"
    // Retain the inherited ThemeButton label's natural hit size when replacing
    // a text label with this glyph.
    property bool preserveTextMetrics: false

    readonly property color glyphColor: !control.enabled ? control.themeTokens.colorDisabledText
                                        : control.down ? control.themeTokens.colorAccentPressed
                                        : control.hovered ? control.themeTokens.colorAccentHover
                                                          : control.themeTokens.colorText

    leftPadding: preserveTextMetrics ? control.themeTokens.spaceMd : 0
    rightPadding: preserveTextMetrics ? control.themeTokens.spaceMd : 0
    topPadding: preserveTextMetrics ? control.themeTokens.spaceSm : 0
    bottomPadding: preserveTextMetrics ? control.themeTokens.spaceSm : 0
    implicitWidth: preserveTextMetrics
                   ? textMetrics.advanceWidth + control.themeTokens.spaceMd * 2
                   : 28
    implicitHeight: Math.max(control.themeTokens.controlHeightMd,
                             preserveTextMetrics
                             ? textMetrics.height + control.themeTokens.spaceSm * 2
                             : contentItem.implicitHeight)

    TextMetrics {
        id: textMetrics
        font: control.font
        text: control.text
    }

    contentItem: Item {
        implicitWidth: 20
        implicitHeight: 20
        anchors.centerIn: parent
        Shape {
            id: glyph
            objectName: control.objectName + "Glyph"
            width: 20
            height: 20
            anchors.centerIn: parent
            transformOrigin: Item.Center
            preferredRendererType: Shape.CurveRenderer
            rotation: control.direction === "left" ? 180
                     : control.direction === "down" ? 90
                     : control.direction === "up" ? -90 : 0

            ShapePath {
                strokeColor: control.glyphColor
                strokeWidth: Math.max(2, control.themeTokens.borderWidth * 1.7)
                capStyle: ShapePath.RoundCap
                joinStyle: ShapePath.RoundJoin
                fillColor: "transparent"
                startX: 4
                startY: 4
                PathLine { x: 9; y: 10 }
                PathLine { x: 4; y: 16 }
            }
            ShapePath {
                strokeColor: control.glyphColor
                strokeWidth: Math.max(2, control.themeTokens.borderWidth * 1.7)
                capStyle: ShapePath.RoundCap
                joinStyle: ShapePath.RoundJoin
                fillColor: "transparent"
                startX: 12
                startY: 4
                PathLine { x: 17; y: 10 }
                PathLine { x: 12; y: 16 }
            }
        }
    }
}
