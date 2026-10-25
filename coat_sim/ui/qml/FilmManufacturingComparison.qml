pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ColumnLayout {
    id: panel
    required property var controller
    required property var themeTokens
    spacing: themeTokens.spaceMd
    ThemeControls.ThemeLabel {
        Layout.fillWidth: true
        text: panel.controller.resultSummary
        wrapMode: Text.Wrap
    }
    Repeater {
        model: panel.controller.comparisonRows
        delegate: ThemeControls.ThemeSection {
            id: resultSection
            required property var modelData
            objectName: "filmManufacturingComparison_" + modelData.route
            Layout.fillWidth: true
            title: modelData.label
            expanded: true
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                text: resultSection.modelData.details
                wrapMode: Text.Wrap
            }
        }
    }
}
