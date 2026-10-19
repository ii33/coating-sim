import QtQuick
import QtQuick.Controls
import "NumericFormat.js" as NumericFormat

// A themed TextField that retains Qt's editing, selection, IME, and keyboard
// behavior. Callers still own validation and business/controller bindings.
TextField {
    id: field
    readonly property var themeTokens:
        typeof Theme.tokenValues !== "undefined" ? Theme.tokenValues : Theme.snapshot

    property string accessibleLabel: ""
    property string accessibleDescription: ""
    // Presentation source for numeric fields. The editing buffer remains text.
    property var numericDisplayValue: undefined
    property int numericDisplayDecimals: 3
    // Opt-in for editable quantities whose committed precision is bounded.
    // Scientific constants keep their full source value while editing.
    property int numericEditDecimals: -1
    property bool numericValueEdited: false
    property string numericEditText: ""
    // Replace the value on each new focus session; repeated clicks while
    // editing retain native cursor placement and partial selection.
    property bool selectAllOnFirstFocus: true
    readonly property bool sharedInputFocusManaged: true

    // Let the native text input/IME see keys first during composition; outside
    // composition, table editors intercept navigation before TextInput.
    Keys.priority: inputMethodComposing ? Keys.AfterItem : Keys.BeforeItem

    function syncNumericDisplay() {
        if (!activeFocus && numericDisplayValue !== undefined)
            text = NumericFormat.format(numericDisplayValue, Qt.locale(),
                                        numericDisplayDecimals)
    }

    function setNumericSourceValue(value) {
        numericValueEdited = false
        numericEditText = ""
        numericDisplayValue = value
        syncNumericDisplay()
    }

    function handleColumnNavigation(event, requestFocus) {
        if (inputMethodComposing) {
            const isVerticalNavigation = event.key === Qt.Key_Up
                    || event.key === Qt.Key_Down
            const isEnterNavigation = event.key === Qt.Key_Return
                    || event.key === Qt.Key_Enter
            if (isVerticalNavigation || isEnterNavigation) {
                // If TextInput/IME did not consume this key, stop it here so
                // an enclosing ListView cannot move focus or commit the row.
                event.accepted = true
                return true
            }
            return false
        }

        let delta = 0
        if (event.key === Qt.Key_Up) {
            delta = -1
        } else if (event.key === Qt.Key_Down) {
            delta = 1
        } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            const otherModifiers = event.modifiers
                    & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)
            if (otherModifiers !== 0)
                return false
            delta = (event.modifiers & Qt.ShiftModifier) !== 0 ? -1 : 1
        } else {
            return false
        }

        // Consume boundary keys too, so Enter cannot trigger an enclosing
        // dialog's default action or mutate the row list.
        event.accepted = true
        requestFocus(delta)
        return true
    }

    function focusAndSelectAll() {
        forceActiveFocus(Qt.TabFocusReason)
        inputFocus.requestSelection()
    }

    onNumericDisplayValueChanged: {
        if (!activeFocus) {
            numericValueEdited = false
            numericEditText = ""
        }
        displayTimer.restart()
    }
    Component.onCompleted: displayTimer.restart()
    onActiveFocusChanged: {
        if (activeFocus && numericDisplayValue !== undefined)
            text = numericDisplayValue === null ? ""
                : numericEditDecimals >= 0
                  ? NumericFormat.format(numericDisplayValue, Qt.locale(), numericEditDecimals)
                  : String(numericDisplayValue)
        else if (!activeFocus)
            displayTimer.restart()
    }
    Timer {
        id: displayTimer
        interval: 0
        onTriggered: field.syncNumericDisplay()
    }
    InputFocusBehavior {
        id: inputFocus
        target: field
        selectOnFocus: field.selectAllOnFirstFocus
    }

    Connections {
        target: field
        function onTextEdited() {
            if (field.numericDisplayValue !== undefined) {
                field.numericValueEdited = true
                field.numericEditText = field.text
            }
        }
    }

    focusPolicy: Qt.StrongFocus
    selectByMouse: true
    font.family: field.themeTokens.fontFamily
    font.pointSize: field.themeTokens.fontSizeBody
    font.weight: field.themeTokens.fontWeightNormal
    topPadding: field.themeTokens.spaceSm
    bottomPadding: field.themeTokens.spaceSm
    leftPadding: field.themeTokens.spaceSm
    rightPadding: field.themeTokens.spaceSm
    implicitHeight: Math.max(field.themeTokens.controlHeightMd,
                             contentHeight + topPadding + bottomPadding)

    color: enabled ? field.themeTokens.colorText : field.themeTokens.colorDisabledText
    placeholderTextColor: field.themeTokens.colorTextMuted
    selectionColor: field.themeTokens.colorSelection
    selectedTextColor: field.themeTokens.colorText

    // The native cursor defaults vary with the active Qt Quick Controls
    // style. Draw it from the semantic foreground token so it stays visible
    // on both light and dark text-field surfaces.
    cursorDelegate: Rectangle {
        objectName: "themeFieldCursor"
        width: Math.max(1, field.themeTokens.borderWidth)
        color: field.themeTokens.colorText
        visible: field.activeFocus
    }

    palette.window: field.themeTokens.colorWindow
    palette.windowText: field.themeTokens.colorText
    palette.base: field.themeTokens.colorSurface
    palette.alternateBase: field.themeTokens.colorSurfaceRaised
    palette.text: field.themeTokens.colorText
    palette.button: field.themeTokens.colorSurfaceRaised
    palette.buttonText: field.themeTokens.colorText
    palette.highlight: field.themeTokens.colorSelection
    palette.highlightedText: field.themeTokens.colorText
    palette.placeholderText: field.themeTokens.colorTextMuted
    palette.mid: field.themeTokens.colorBorder
    palette.dark: field.themeTokens.colorBorderStrong

    Accessible.name: field.accessibleLabel.length > 0
                     ? field.accessibleLabel : field.placeholderText
    Accessible.description: field.accessibleDescription

    background: Rectangle {
        objectName: field.objectName + "_background"
        radius: field.themeTokens.radiusSm
        color: field.enabled ? field.themeTokens.colorSurface
                             : field.themeTokens.colorDisabledSurface
        border.width: field.activeFocus ? field.themeTokens.focusRingWidth
                                        : field.themeTokens.borderWidth
        border.color: field.activeFocus ? field.themeTokens.colorFocusRing
                     : field.hovered && field.enabled ? field.themeTokens.colorBorderStrong
                                                      : field.themeTokens.colorBorder
    }
}
