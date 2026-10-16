import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

Column {
    id: control
    property string label: ""
    property string value: ""
    property string hint: ""
    property bool numeric: true
    property string fieldName: ""
    property bool compact: false
    property int compactFieldWidth: 84
    signal edited(string value)

    spacing: compact ? 2 : 4
    Layout.minimumWidth: compact
        ? Math.max(compactFieldWidth, numeric ? numericFieldMetrics.width + 24 : 120)
          + Math.max(52, Math.min(96, labelMetrics.width * 0.45)) + 4
        : 0

    TextMetrics {
        id: labelMetrics
        font: compactLabel.font
        text: control.label
    }
    TextMetrics {
        id: numericFieldMetrics
        font: compactField.font
        text: "-123.456"
    }

    ThemeControls.ThemeLabel {
        id: fullLabel
        visible: !control.compact
        text: control.label
        width: parent.width
        wrapMode: Text.Wrap
    }
    ThemeControls.ThemeField {
        objectName: control.fieldName
        visible: !control.compact
        width: parent.width
        text: control.numeric ? "" : control.value
        numericDisplayValue: control.numeric ? control.value : undefined
        selectByMouse: true
        inputMethodHints: control.numeric ? Qt.ImhFormattedNumbersOnly : Qt.ImhNone
        accessibleLabel: control.label
        accessibleDescription: control.hint
        onTextEdited: control.edited(text)
    }
    RowLayout {
        objectName: "sourceCompactFieldRow"
        visible: control.compact
        width: parent.width
        spacing: 4

        ThemeControls.ThemeLabel {
            id: compactLabel
            Layout.fillWidth: true
            Layout.minimumWidth: Math.max(52, Math.min(96, labelMetrics.width * 0.45))
            Layout.preferredWidth: Math.max(Layout.minimumWidth,
                                             parent.width - compactField.Layout.preferredWidth - 8)
            text: control.label
            wrapMode: Text.WordWrap
            verticalAlignment: Text.AlignVCenter
        }
        ThemeControls.ThemeField {
            id: compactField
            objectName: control.fieldName
            Layout.preferredWidth: control.numeric
                ? Math.max(control.compactFieldWidth, numericFieldMetrics.width + 24)
                : Math.min(220, parent.width * 0.45)
            Layout.minimumWidth: control.numeric
                ? Math.min(72, control.compactFieldWidth)
                : Math.min(120, parent.width * 0.4)
            Layout.maximumWidth: control.numeric
                ? Math.max(control.compactFieldWidth, numericFieldMetrics.width + 24)
                : parent.width
            Layout.fillWidth: !control.numeric
            text: control.numeric ? "" : control.value
            numericDisplayValue: control.numeric ? control.value : undefined
            selectByMouse: true
            inputMethodHints: control.numeric ? Qt.ImhFormattedNumbersOnly : Qt.ImhNone
            accessibleLabel: control.label
            accessibleDescription: control.hint
            onTextEdited: control.edited(text)
        }
    }
    ThemeControls.ThemeLabel {
        objectName: "sourceFieldHint"
        visible: control.hint.length > 0
        text: control.hint
        width: parent.width
        wrapMode: Text.Wrap
        muted: true
        font.pointSize: typeof Theme !== "undefined" ? Theme.snapshot.fontSizeSmall : 10
    }
}
