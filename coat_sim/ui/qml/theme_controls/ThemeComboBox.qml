import QtQuick
import QtQuick.Controls

// Qt ComboBox model/selection/input behavior with a semantic popup and rows.
ComboBox {
    id: control
    readonly property var themeTokens:
        typeof Theme.tokenValues !== "undefined" ? Theme.tokenValues : Theme.snapshot

    property string accessibleLabel: ""
    property string accessibleDescription: ""

    focusPolicy: Qt.StrongFocus
    font.family: control.themeTokens.fontFamily
    font.pointSize: control.themeTokens.fontSizeBody
    font.weight: control.themeTokens.fontWeightNormal
    implicitHeight: Math.max(control.themeTokens.controlHeightMd,
                             contentItem.implicitHeight + topPadding + bottomPadding)
    topPadding: control.themeTokens.spaceSm
    bottomPadding: control.themeTokens.spaceSm
    leftPadding: control.themeTokens.spaceSm
    rightPadding: control.themeTokens.spaceSm + implicitIndicatorWidth

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

    Accessible.name: accessibleLabel.length > 0 ? accessibleLabel : displayText
    Accessible.description: accessibleDescription

    contentItem: editable ? editor : displayLabel

    Text {
        id: displayLabel
        visible: !control.editable
        text: control.displayText
        textFormat: Text.PlainText
        font: control.font
        color: !control.enabled ? control.themeTokens.colorDisabledText
                               : control.themeTokens.colorText
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    TextInput {
        id: editor
        visible: control.editable
        clip: true
        readonly property bool sharedInputFocusManaged: true
        readonly property var sharedInputFocusOwner: control
        readOnly: !control.editable
        selectByMouse: control.editable
        autoScroll: control.editable
        selectionColor: control.themeTokens.colorSelection
        selectedTextColor: control.themeTokens.colorText
        validator: control.validator
        inputMethodHints: control.inputMethodHints
        InputFocusBehavior { target: editor }
        // Control positions and sizes contentItem within availableWidth, after
        // applying the control's padding. Do not subtract those paddings again.
        text: control.editable ? control.editText : control.displayText
        font: control.font
        color: !control.enabled ? control.themeTokens.colorDisabledText
                               : control.themeTokens.colorText
        verticalAlignment: Text.AlignVCenter
    }

    indicator: Canvas {
        objectName: "themeComboBoxIndicator"
        readonly property real arrowSize: control.themeTokens.fontSizeBody * 4 / 3
        property color indicatorColor: !control.enabled ? control.themeTokens.colorDisabledText
                                                        : control.themeTokens.colorTextMuted
        implicitWidth: arrowSize
        implicitHeight: arrowSize
        width: implicitWidth
        height: implicitHeight
        x: control.width - width - control.themeTokens.spaceSm
        y: (control.height - height) / 2
        onIndicatorColorChanged: requestPaint()
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        Component.onCompleted: requestPaint()
        onPaint: {
            const ctx = getContext("2d")
            ctx.clearRect(0, 0, width, height)
            ctx.strokeStyle = indicatorColor.toString()
            ctx.lineWidth = Math.max(1, control.themeTokens.borderWidth * 2)
            ctx.lineCap = "round"
            ctx.lineJoin = "round"
            ctx.beginPath()
            ctx.moveTo(width * 0.24, height * 0.4)
            ctx.lineTo(width * 0.5, height * 0.65)
            ctx.lineTo(width * 0.76, height * 0.4)
            ctx.stroke()
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

    delegate: ItemDelegate {
        id: row
        required property int index
        width: control.popup.width
        text: control.textAt(index)
        highlighted: control.highlightedIndex === index
        readonly property bool currentSelection: control.currentIndex === index
        font: control.font
        padding: control.themeTokens.spaceSm
        Accessible.name: text

        contentItem: Text {
            text: row.text
            textFormat: Text.PlainText
            font: row.font
            color: !row.enabled ? control.themeTokens.colorDisabledText
                                : row.highlighted ? control.themeTokens.colorTextOnAccent
                                                  : control.themeTokens.colorText
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            objectName: "themeComboBoxRow_" + row.index + "_background"
            color: !row.enabled ? control.themeTokens.colorDisabledSurface
                  : row.highlighted ? control.themeTokens.colorAccent
                  : row.currentSelection ? control.themeTokens.colorSelection
                  : row.hovered ? control.themeTokens.colorSurfaceRaised
                                : control.themeTokens.colorSurface
            border.width: row.activeFocus ? control.themeTokens.focusRingWidth : 0
            border.color: control.themeTokens.colorFocusRing
        }
    }

    popup: Popup {
        objectName: "themeComboBoxPopup"
        y: control.height
        width: control.width
        padding: control.themeTokens.borderWidth
        implicitHeight: Math.min(listView.contentHeight,
                                 control.themeTokens.controlHeightMd * 8)
        contentItem: ListView {
            id: listView
            clip: true
            implicitHeight: contentHeight
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            ScrollIndicator.vertical: ScrollIndicator { }
        }
        background: Rectangle {
            objectName: "themeComboBoxPopup_background"
            color: control.themeTokens.colorSurface
            radius: control.themeTokens.radiusSm
            border.width: control.themeTokens.borderWidth
            border.color: control.themeTokens.colorBorderStrong
        }
    }
}
