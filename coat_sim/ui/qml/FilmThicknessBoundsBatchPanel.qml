pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ColumnLayout {
    id: batchPanel
    objectName: "filmOptimizationBatchBoundsPanel"
    required property var controller
    required property var themeTokens
    property string message: ""
    property bool failed: false
    Layout.minimumWidth: 0
    spacing: batchPanel.themeTokens.spaceXs
    enabled: !batchPanel.controller.busy

    Label {
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        text: "批量厚度边界（nm）"
        color: batchPanel.themeTokens.colorText
    }
    GridLayout {
        id: batchGrid
        readonly property bool wide: width >= 720
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        columns: batchGrid.wide ? 7 : 2
        columnSpacing: batchPanel.themeTokens.spaceXs
        rowSpacing: batchPanel.themeTokens.spaceXs
        Label { text: "范围"; color: batchPanel.themeTokens.colorTextMuted }
        ThemeControls.ThemeComboBox {
            id: scopeBox
            objectName: "filmOptimizationBatchBoundsScope"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.preferredWidth: 150
            model: [{id: "all", label: "全部层"}, {id: "selected", label: "已选择的厚度变量"}]
            textRole: "label"
            currentIndex: 0
            Accessible.name: "批量厚度修改范围"
        }
        Label { text: "下限"; color: batchPanel.themeTokens.colorTextMuted }
        ThemeControls.ThemeField {
            id: lowerField
            objectName: "filmOptimizationBatchLower"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.preferredWidth: 80
            placeholderText: "留空保持"
            accessibleLabel: "批量厚度下限（nm）"
            onTextEdited: batchPanel.message = ""
        }
        Label { text: "上限"; color: batchPanel.themeTokens.colorTextMuted }
        ThemeControls.ThemeField {
            id: upperField
            objectName: "filmOptimizationBatchUpper"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.preferredWidth: 80
            placeholderText: "留空保持"
            accessibleLabel: "批量厚度上限（nm）"
            onTextEdited: batchPanel.message = ""
        }
        ThemeControls.ThemeButton {
            objectName: "filmOptimizationBatchBoundsApply"
            Layout.columnSpan: batchGrid.wide ? 1 : 2
            Layout.fillWidth: true
            text: "应用边界"
            enabled: lowerField.text.trim().length > 0 || upperField.text.trim().length > 0
            onClicked: {
                const ok = batchPanel.controller.applyVariableBoundsBatch(
                    scopeBox.currentIndex === 1 ? "selected" : "all", lowerField.text, upperField.text)
                batchPanel.failed = !ok
                batchPanel.message = ok ? "已应用边界，层选择状态保持不变。" : batchPanel.controller.error
            }
        }
    }
    Label {
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        text: "真空纳米（nm）；留空不修改该项，0 是有效下限。上限须大于下限，层的选择状态保持不变。"
        color: batchPanel.themeTokens.colorTextMuted
        font.pointSize: batchPanel.themeTokens.fontSizeSmall
        wrapMode: Text.WordWrap
    }
    Label {
        objectName: "filmOptimizationBatchBoundsMessage"
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        visible: batchPanel.message.length > 0
        text: batchPanel.message
        color: batchPanel.failed ? batchPanel.themeTokens.colorError : batchPanel.themeTokens.colorInfo
        wrapMode: Text.WordWrap
    }
}
