import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Numeric step control. Its value/range/stepSize/editable API stays identical
// to Qt SpinBox; only its editor and increment/decrement affordances are themed.
SpinBox {
    id: control
    readonly property var themeTokens:
        typeof Theme.tokenValues !== "undefined" ? Theme.tokenValues : Theme.snapshot

    property string accessibleLabel: ""
    property string accessibleDescription: ""

    // Reserve room for the widest formatted endpoint and the stacked
    // increment/decrement column so layouts cannot squeeze the editor.
    readonly property real minimumEditorWidth: Math.ceil(Math.max(
        fromTextMetrics.width, toTextMetrics.width, currentTextMetrics.width
    )) + 2 * control.themeTokens.spaceXs
    readonly property real indicatorButtonWidth: Math.max(
        height / 2,
        Math.ceil(Math.max(plusSymbolMetrics.width, minusSymbolMetrics.width)
                  + 2 * control.themeTokens.spaceXs)
    )

    TextMetrics {
        id: fromTextMetrics
        font: control.font
        text: control.textFromValue(control.from, control.locale)
    }
    TextMetrics {
        id: toTextMetrics
        font: control.font
        text: control.textFromValue(control.to, control.locale)
    }
    TextMetrics {
        id: currentTextMetrics
        font: control.font
        text: control.displayText
    }
    TextMetrics {
        id: plusSymbolMetrics
        font: control.font
        text: "+"
    }
    TextMetrics {
        id: minusSymbolMetrics
        font: control.font
        text: "\u2212"
    }

    focusPolicy: Qt.StrongFocus
    font.family: control.themeTokens.fontFamily
    font.pointSize: control.themeTokens.fontSizeBody
    font.weight: control.themeTokens.fontWeightNormal
    implicitHeight: Math.max(control.themeTokens.controlHeightMd,
                             contentItem.implicitHeight + topPadding + bottomPadding)
    implicitWidth: Math.ceil(leftPadding + minimumEditorWidth + rightPadding)
    Layout.minimumWidth: implicitWidth
    topPadding: control.themeTokens.spaceXs
    bottomPadding: control.themeTokens.spaceXs
    leftPadding: control.themeTokens.spaceSm
    rightPadding: indicatorButtonWidth + 2 * control.themeTokens.spaceXs

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

    Accessible.name: accessibleLabel
    Accessible.description: accessibleDescription

    contentItem: TextInput {
        id: editor
        z: 2
        text: control.displayText
        font: control.font
        color: !control.enabled ? control.themeTokens.colorDisabledText
                               : control.themeTokens.colorText
        selectionColor: control.themeTokens.colorSelection
        selectedTextColor: control.themeTokens.colorText
        horizontalAlignment: Qt.AlignHCenter
        verticalAlignment: Qt.AlignVCenter
        readOnly: !control.editable
        validator: control.validator
        inputMethodHints: control.inputMethodHints
        activeFocusOnTab: control.editable
        onTextEdited: control.value = control.valueFromText(text, control.locale)
        readonly property bool sharedInputFocusManaged: true
        readonly property var sharedInputFocusOwner: control
        InputFocusBehavior { target: editor }
    }

    up.indicator: ThemeButton {
        objectName: control.objectName ? control.objectName + "_up" : ""
        text: "+"
        flat: true
        width: control.indicatorButtonWidth
        height: control.height / 2
        topPadding: 0
        bottomPadding: 0
        leftPadding: 0
        rightPadding: 0
        anchors.right: parent.right
        anchors.rightMargin: control.themeTokens.spaceXs
        anchors.top: parent.top
        enabled: control.enabled && control.value < control.to
        Accessible.name: "增加"
        onClicked: {
            const previousValue = control.value
            control.increase()
            if (control.value !== previousValue)
                control.valueModified()
        }
    }

    down.indicator: ThemeButton {
        objectName: control.objectName ? control.objectName + "_down" : ""
        text: "−"
        flat: true
        width: control.indicatorButtonWidth
        height: control.height / 2
        topPadding: 0
        bottomPadding: 0
        leftPadding: 0
        rightPadding: 0
        anchors.right: parent.right
        anchors.rightMargin: control.themeTokens.spaceXs
        anchors.bottom: parent.bottom
        enabled: control.enabled && control.value > control.from
        Accessible.name: "减少"
        onClicked: {
            const previousValue = control.value
            control.decrease()
            if (control.value !== previousValue)
                control.valueModified()
        }
    }

    background: Rectangle {
        objectName: control.objectName + "_background"
        radius: control.themeTokens.radiusSm
        color: !control.enabled ? control.themeTokens.colorDisabledSurface
                                : control.themeTokens.colorSurface
        border.width: control.activeFocus ? control.themeTokens.focusRingWidth
                                          : control.themeTokens.borderWidth
        border.color: control.activeFocus ? control.themeTokens.colorFocusRing
                     : control.hovered && control.enabled ? control.themeTokens.colorBorderStrong
                                                          : control.themeTokens.colorBorder
    }
}
