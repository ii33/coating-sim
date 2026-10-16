import QtQuick
import QtQuick.Controls
import "theme_controls" as ThemeControls

// Keep in-progress numeric text private to the focused control. Controller
// changes refresh inactive fields; progress/result notifications never do.
ThemeControls.ThemeField {
    id: field
    required property var controller
    required property string fieldKey
    objectName: "simulationField_" + fieldKey
    numericDisplayValue: field.controller.fields[field.fieldKey]
    onTextEdited: controller.setField(fieldKey, text)
}
