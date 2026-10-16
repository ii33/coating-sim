import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

// A compact, keyboard-operable disclosure section. The header stays in the
// tab order; body content is supplied by the caller and is not recreated.
Control {
    id: section
    readonly property var themeTokens:
        typeof Theme.tokenValues !== "undefined" ? Theme.tokenValues : Theme.snapshot

    property string title: ""
    property string accessibleLabel: ""
    property bool expanded: true
    default property alias contentData: body.data

    Layout.fillWidth: true
    padding: 0
    implicitWidth: contentItem.implicitWidth
    implicitHeight: contentItem.implicitHeight

    palette.window: section.themeTokens.colorWindow
    palette.windowText: section.themeTokens.colorText
    palette.base: section.themeTokens.colorSurface
    palette.alternateBase: section.themeTokens.colorSurfaceRaised
    palette.text: section.themeTokens.colorText
    palette.button: section.themeTokens.colorSurfaceRaised
    palette.buttonText: section.themeTokens.colorText
    palette.highlight: section.themeTokens.colorSelection
    palette.highlightedText: section.themeTokens.colorText
    palette.placeholderText: section.themeTokens.colorTextMuted
    palette.mid: section.themeTokens.colorBorder
    palette.dark: section.themeTokens.colorBorderStrong
    font.family: section.themeTokens.fontFamily
    font.pointSize: section.themeTokens.fontSizeBody
    font.weight: section.themeTokens.fontWeightNormal

    background: Rectangle {
        color: section.themeTokens.colorSurface
        radius: section.themeTokens.radiusMd
        border.width: section.themeTokens.borderWidth
        border.color: section.themeTokens.colorBorder
    }

    contentItem: ColumnLayout {
        spacing: 0

        ThemeButton {
            id: header
            objectName: "themeSectionHeader"
            Layout.fillWidth: true
            Layout.margins: section.themeTokens.spaceXs
            text: section.title
            Accessible.name: section.accessibleLabel.length > 0
                             ? section.accessibleLabel : section.title
            Accessible.description: section.expanded ? "Expanded" : "Collapsed"
            onClicked: section.expanded = !section.expanded

            contentItem: RowLayout {
                spacing: section.themeTokens.spaceXs

                Canvas {
                    id: disclosureIndicator
                    objectName: "themeSectionDisclosureIndicator"
                    width: 12
                    height: 12
                    Layout.preferredWidth: 12
                    Layout.preferredHeight: 12
                    Accessible.ignored: true

                    property color indicatorColor: header.enabled
                        ? section.themeTokens.colorText
                        : section.themeTokens.colorDisabledText
                    property bool disclosureExpanded: section.expanded

                    onIndicatorColorChanged: requestPaint()
                    onDisclosureExpandedChanged: requestPaint()
                    onPaint: {
                        const context = getContext("2d")
                        context.clearRect(0, 0, width, height)
                        context.strokeStyle = indicatorColor
                        context.lineWidth = 1.5
                        context.lineCap = "round"
                        context.lineJoin = "round"
                        context.beginPath()
                        if (disclosureExpanded) {
                            context.moveTo(width * 0.2, height * 0.35)
                            context.lineTo(width * 0.5, height * 0.65)
                            context.lineTo(width * 0.8, height * 0.35)
                        } else {
                            context.moveTo(width * 0.35, height * 0.2)
                            context.lineTo(width * 0.65, height * 0.5)
                            context.lineTo(width * 0.35, height * 0.8)
                        }
                        context.stroke()
                    }
                }

                Text {
                    id: headerTitle
                    objectName: "themeSectionHeaderTitle"
                    Accessible.ignored: true
                    Layout.fillWidth: true
                    text: section.title
                    font: header.font
                    color: !header.enabled ? section.themeTokens.colorDisabledText
                          : header.primary || header.checked
                            ? section.themeTokens.colorTextOnAccent
                            : section.themeTokens.colorText
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                    wrapMode: Text.NoWrap
                }
            }
        }

        ColumnLayout {
            id: body
            objectName: "themeSectionBody"
            Layout.fillWidth: true
            Layout.leftMargin: section.themeTokens.spaceMd
            Layout.rightMargin: section.themeTokens.spaceMd
            Layout.bottomMargin: section.themeTokens.spaceMd
            spacing: section.themeTokens.spaceSm
            visible: section.expanded
        }
    }
}
