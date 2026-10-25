pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs as NativeDialogs
import "theme_controls" as ThemeControls

ThemeControls.ThemeDialogBase {
    id: dialog
    objectName: "filmManufacturingRepairDialog"
    required property var controller
    title: "工艺薄层修复"
    modal: true
    closePolicy: Popup.CloseOnEscape
    width: Math.min(1060, Math.max(280, parent ? parent.width - 32 : 1060))
    height: Math.min(850, Math.max(280, parent ? parent.height - 32 : 850))
    x: parent ? (parent.width - width) / 2 : 0
    y: parent ? (parent.height - height) / 2 : 0
    onClosed: { if (controller.busy) controller.cancel() }

    contentItem: ScrollView {
        id: viewport
        objectName: "filmManufacturingViewport"
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ColumnLayout {
            width: viewport.availableWidth
            spacing: dialog.themeTokens.spaceMd
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                text: "实验性独立 A/B 比较，不保证更优或满足实际制造要求；不改变原始设计或原始光学目标。A 保留并增厚选中层；B 删除选中层后合并。两路随后均精修全部保留层，参考厚度不是全局硬下限。没有隐藏薄层阈值，也不自动选择或应用。"
                wrapMode: Text.Wrap
            }
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                muted: true
                text: "来源厚度保留原始单位显示；QW 会在后台按冻结材料及原参考波长，通过已有物理厚度接口转换为 nm，不把 QW 数值当成 nm。每路评价配额仅指精修调用，起点评价和独立审计另列账目。"
                wrapMode: Text.Wrap
            }
            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeButton {
                    objectName: "filmManufacturingRefresh"
                    text: "刷新当前来源"
                    enabled: !dialog.controller.busy
                    onClicked: dialog.controller.prepare()
                }
                ThemeControls.ThemeLabel {
                    Layout.fillWidth: true
                    text: "只勾选明确要处理的当前前层，稳定 ID 与实际厚度如下。"
                    wrapMode: Text.Wrap
                }
            }
            Repeater {
                model: dialog.controller.layerRows
                delegate: CheckBox {
                    id: layerCheck
                    required property var modelData
                    Layout.fillWidth: true
                    objectName: "filmManufacturingLayer_" + modelData.layer_id
                    enabled: !dialog.controller.busy
                    checked: modelData.selected
                    text: modelData.label + " · " + modelData.thickness + " · ID: " + modelData.layer_id
                    onClicked: dialog.controller.setLayerSelected(modelData.layer_id, checked)
                    contentItem: Text {
                        text: layerCheck.text
                        font: layerCheck.font
                        color: layerCheck.enabled ? dialog.themeTokens.colorText : dialog.themeTokens.colorDisabledText
                        leftPadding: layerCheck.indicator.width + layerCheck.spacing
                        wrapMode: Text.Wrap
                        verticalAlignment: Text.AlignVCenter
                    }
                }
            }
            GridLayout {
                Layout.fillWidth: true
                columns: dialog.width > 720 ? 4 : dialog.width > 420 ? 2 : 1
                columnSpacing: dialog.themeTokens.spaceMd
                rowSpacing: dialog.themeTokens.spaceSm
                Repeater {
                    model: [
                        {key: "reference_nm", label: "参考厚度 nm（必须填写）"},
                        {key: "refinement_evaluations_per_route", label: "每路精修评价配额（正整数）"},
                        {key: "max_iterations_per_route", label: "每路最大迭代（正整数）"},
                        {key: "seconds_per_route", label: "每路秒数配额（正数）"}
                    ]
                    delegate: ColumnLayout {
                        id: optionCell
                        required property var modelData
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        ThemeControls.ThemeLabel {
                            Layout.fillWidth: true
                            text: optionCell.modelData.label
                            wrapMode: Text.Wrap
                        }
                        ThemeControls.ThemeField {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            objectName: "filmManufacturing_" + optionCell.modelData.key
                            accessibleLabel: optionCell.modelData.label
                            enabled: !dialog.controller.busy
                            text: dialog.controller.options[optionCell.modelData.key]
                            placeholderText: "空白不是零或无限"
                            onTextEdited: dialog.controller.setOption(optionCell.modelData.key, text)
                            onEditingFinished: dialog.controller.finishOptionEdit(optionCell.modelData.key)
                        }
                    }
                }
            }
            ThemeControls.ThemeLabel {
                objectName: "filmManufacturingStatus"
                Layout.fillWidth: true
                text: dialog.controller.statusText + (dialog.controller.jobId ? "\n作业 ID: " + dialog.controller.jobId : "")
                wrapMode: Text.Wrap
            }
            ThemeControls.ThemeLabel {
                objectName: "filmManufacturingError"
                Layout.fillWidth: true
                visible: text.length > 0
                text: dialog.controller.error
                wrapMode: Text.Wrap
            }
            FilmManufacturingComparison {
                Layout.fillWidth: true
                visible: dialog.controller.hasResult
                controller: dialog.controller
                themeTokens: dialog.themeTokens
            }
            ThemeControls.ThemeSection {
                Layout.fillWidth: true
                title: "显式选择与应用"
                visible: dialog.controller.hasResult
                ThemeControls.ThemeLabel {
                    Layout.fillWidth: true
                    text: "警告：比较不会放宽原始目标。完整审计的候选也可能显示原目标未通过，应用不代表光学验收通过。请逐波段核对训练与独立审计；未审计的结果不能应用。仅选定 A/B 并明确确认后，才会替换来源膜系，可在设计窗口撤销。"
                    wrapMode: Text.Wrap
                }
                ThemeControls.ThemeLabel {
                    Layout.fillWidth: true
                    objectName: "filmManufacturingSelectedWarning"
                    text: dialog.controller.selectedRouteWarning
                    wrapMode: Text.Wrap
                }
                RowLayout {
                    Layout.fillWidth: true
                    ThemeControls.ThemeComboBox {
                        objectName: "filmManufacturingRoute"
                        Layout.fillWidth: true
                        enabled: !dialog.controller.busy
                        model: ["请选择 A/B，不自动选择", "A · 保留增厚", "B · 删除合并"]
                        currentIndex: dialog.controller.selectedRoute === "raise_selected" ? 1
                                      : dialog.controller.selectedRoute === "delete_selected_merge" ? 2 : 0
                        onActivated: function(index) {
                            dialog.controller.setSelectedRoute(index === 1 ? "raise_selected" : index === 2 ? "delete_selected_merge" : "")
                        }
                    }
                    ThemeControls.ThemeButton {
                        objectName: "filmManufacturingApply"
                        text: "应用所选路线…"
                        enabled: dialog.controller.canApply
                        onClicked: applyConfirm.open()
                    }
                }
            }
        }
    }
    footer: GridLayout {
        columns: dialog.width > 720 ? 5 : 2
        columnSpacing: dialog.themeTokens.spaceSm
        rowSpacing: dialog.themeTokens.spaceSm
        ThemeControls.ThemeButton {
            Layout.fillWidth: dialog.width <= 720
            objectName: "filmManufacturingSave"
            text: "保存完整报告…"
            enabled: dialog.controller.hasResult && !dialog.controller.busy
            onClicked: saveDialog.open()
        }
        Item { Layout.fillWidth: true; visible: dialog.width > 720 }
        ThemeControls.ThemeButton {
            Layout.fillWidth: dialog.width <= 720
            objectName: "filmManufacturingRun"
            text: "运行 A/B 比较"
            primary: true
            enabled: !dialog.controller.busy
            onClicked: dialog.controller.start()
        }
        ThemeControls.ThemeButton {
            Layout.fillWidth: dialog.width <= 720
            objectName: "filmManufacturingCancel"
            text: "取消作业"
            enabled: dialog.controller.busy
            onClicked: dialog.controller.cancel()
        }
        ThemeControls.ThemeButton {
            Layout.fillWidth: dialog.width <= 720
            objectName: "filmManufacturingClose"
            text: dialog.controller.busy ? "取消并关闭" : "关闭"
            onClicked: dialog.close()
        }
    }
    ThemeControls.ThemeDialogBase {
        id: applyConfirm
        objectName: "filmManufacturingApplyConfirm"
        title: "确认应用工艺修复候选"
        modal: true
        width: Math.min(580, dialog.width - 24)
        x: (dialog.width - width) / 2
        y: (dialog.height - height) / 2
        standardButtons: Dialog.Ok | Dialog.Cancel
        ThemeControls.ThemeLabel {
            width: applyConfirm.availableWidth
            text: dialog.controller.selectedRouteWarning + "\n将用已选择的 A/B 候选替换当前来源膜系。是否按比较中显示的真实分类应用？来源、目标、材料或项目发生变化时会拒绝整个应用。"
            wrapMode: Text.Wrap
        }
        onAccepted: dialog.controller.applySelectedRoute()
    }
    NativeDialogs.FileDialog {
        id: saveDialog
        title: "保存完整工艺修复报告"
        fileMode: NativeDialogs.FileDialog.SaveFile
        nameFilters: ["JSON 报告 (*.json)"]
        defaultSuffix: "json"
        onAccepted: dialog.controller.saveReportUrl(selectedFile, false)
    }
    Connections {
        target: dialog.controller
        function onOverwriteRequested(path) { overwriteConfirm.targetPath = path; overwriteConfirm.open() }
    }
    ThemeControls.ThemeDialogBase {
        id: overwriteConfirm
        property string targetPath: ""
        title: "覆盖已有报告？"
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        ThemeControls.ThemeLabel { text: overwriteConfirm.targetPath; wrapMode: Text.Wrap }
        onAccepted: dialog.controller.saveReport(targetPath, true)
    }
}
