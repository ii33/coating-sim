import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ThemeControls.ThemeDialogBase {
    id: dialog
    property var controller
    property string localError: ""
    signal exportRequested()

    title: "PNG 导出设置"
    modal: true
    standardButtons: Dialog.Cancel
    width: 500
    anchors.centerIn: Overlay.overlay
    onOpened: {
        localError = ""
        var value = controller.pngExportSettings
        widthIn.setNumericSourceValue(value.width_in)
        heightIn.setNumericSourceValue(value.height_in)
        dpi.setNumericSourceValue(value.dpi)
        transparent.checked = value.transparent
    }

    function applySettings() {
        var accepted = controller.setPngExportSettings({
            width_in: Number(widthIn.numericValueEdited ? widthIn.numericEditText : widthIn.numericDisplayValue),
            height_in: Number(heightIn.numericValueEdited ? heightIn.numericEditText : heightIn.numericDisplayValue),
            dpi: Number(dpi.numericValueEdited ? dpi.numericEditText : dpi.numericDisplayValue),
            transparent: transparent.checked
        })
        localError = accepted ? "" : controller.validationError
        if (accepted) {
            dialog.close()
            dialog.exportRequested()
        }
        return accepted
    }

    contentItem: ColumnLayout {
        Accessible.role: Accessible.Dialog
        Accessible.name: dialog.title
        spacing: Theme.snapshot.spaceMd

        Label {
            Layout.fillWidth: true
            text: "默认 9 × 5 英寸、160 DPI、不透明背景。导出会使用当前坐标范围和曲线。"
            color: Theme.snapshot.colorTextMuted
            wrapMode: Text.WordWrap
        }
        GridLayout {
            Layout.fillWidth: true
            columns: 2
            Label { text: "宽度（英寸）"; color: Theme.snapshot.colorText }
            ThemeControls.ThemeField {
                id: widthIn
                Layout.fillWidth: true
                accessibleLabel: "PNG 宽度（英寸）"
                validator: DoubleValidator { bottom: 4; top: 18; decimals: 2 }
                onTextEdited: numericDisplayValue = text
            }
            Label { text: "高度（英寸）"; color: Theme.snapshot.colorText }
            ThemeControls.ThemeField {
                id: heightIn
                Layout.fillWidth: true
                accessibleLabel: "PNG 高度（英寸）"
                validator: DoubleValidator { bottom: 3; top: 12; decimals: 2 }
                onTextEdited: numericDisplayValue = text
            }
            Label { text: "分辨率（DPI）"; color: Theme.snapshot.colorText }
            ThemeControls.ThemeField {
                id: dpi
                Layout.fillWidth: true
                accessibleLabel: "PNG 分辨率（DPI）"
                validator: IntValidator { bottom: 72; top: 300 }
                onTextEdited: numericDisplayValue = text
            }
        }
        CheckBox {
            id: transparent
            text: "透明背景"
            palette.text: Theme.snapshot.colorText
            Accessible.name: text
        }
        Label {
            Layout.fillWidth: true
            visible: dialog.localError.length > 0
            text: dialog.localError
            color: Theme.snapshot.colorError
            wrapMode: Text.WordWrap
        }
        RowLayout {
            Layout.alignment: Qt.AlignRight
            ThemeControls.ThemeButton {
                objectName: "filmPlotExportApplyButton"
                text: "导出 PNG"
                primary: true
                onClicked: dialog.applySettings()
            }
        }
    }
}
