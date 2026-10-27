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
    title: "自动薄层修复"
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
                text: "自动处理低于“单层目标厚度−厚度容差”的薄层，无需勾选。较厚层可联动调整，允许增加、删除和合并膜层；所有保留层都重新检查厚度下限。结果不会自动应用。"
                wrapMode: Text.Wrap
            }
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                muted: true
                objectName: "filmManufacturingScopeHelp"
                text: dialog.controller.comparisonScopeSummary
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
                    text: "当前来源层（只读；QW 会按原材料和参考波长换算为物理 nm 后自动识别）"
                    wrapMode: Text.Wrap
                }
            }
            Repeater {
                model: dialog.controller.layerRows
                delegate: ThemeControls.ThemeLabel {
                    id: layerCheck
                    required property var modelData
                    Layout.fillWidth: true
                    objectName: "filmManufacturingLayer_" + modelData.layer_id
                    text: modelData.label + " · " + modelData.thickness
                    wrapMode: Text.Wrap
                }
            }
            GridLayout {
                Layout.fillWidth: true
                columns: dialog.width > 720 ? 3 : 1
                columnSpacing: dialog.themeTokens.spaceMd
                rowSpacing: dialog.themeTokens.spaceSm
                Repeater {
                    model: [
                        {key: "reference_nm", label: "单层目标厚度（nm）", help: "针对一层，不是膜系总厚度；待修复薄层从这个厚度附近开始调整。"},
                        {key: "thickness_tolerance_nm", label: "厚度容差（nm）", help: "允许比单层目标薄多少。例如目标 10、容差 1，保留层须不薄于 9 nm；较厚层没有 11 nm 上限。可填 0。"},
                        {key: "optical_tolerance_percentage_points", label: "光学指标容差（百分点）", help: "相对原曲线的最大绝对变化。例如 R 从 50% 变为 51%，变化为 1 个百分点，不是相对变化 1%。可填 0。"}
                    ]
                    delegate: ColumnLayout {
                        id: optionCell
                        required property var modelData
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                        Layout.alignment: Qt.AlignTop
                        ThemeControls.ThemeLabel {
                            objectName: "filmManufacturingLabel_" + optionCell.modelData.key
                            Layout.fillWidth: true
                            text: optionCell.modelData.label
                            wrapMode: Text.Wrap
                        }
                        ThemeControls.ThemeField {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 0
                            objectName: "filmManufacturing_" + optionCell.modelData.key
                            accessibleLabel: optionCell.modelData.label
                            accessibleDescription: optionCell.modelData.help
                            enabled: !dialog.controller.busy
                            text: dialog.controller.automaticOptions[optionCell.modelData.key]
                            placeholderText: optionCell.modelData.key === "reference_nm" ? "填写正数" : "填写非负数"
                            onTextEdited: dialog.controller.setAutomaticOption(optionCell.modelData.key, text)
                            onEditingFinished: dialog.controller.finishAutomaticOptionEdit(optionCell.modelData.key)
                        }
                        ThemeControls.ThemeLabel {
                            objectName: "filmManufacturingHelp_" + optionCell.modelData.key
                            Layout.fillWidth: true
                            muted: true
                            text: optionCell.modelData.help
                            wrapMode: Text.Wrap
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
                title: "查看与应用修复结果"
                visible: dialog.controller.hasResult
                ThemeControls.ThemeLabel {
                    Layout.fillWidth: true
                    text: "只有完整审计且满足单层下限、光学变化容差及全部启用原目标的结果可应用；没有启用原目标时，该项不适用。请核对下方联合验收证据，点击并确认后才替换来源膜系，可在设计窗口撤销。"
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
                        visible: false // Legacy A/B reports remain readable; new repair has one audited best.
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
                        text: "应用修复结果…"
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
            text: "开始自动修复"
            primary: true
            enabled: !dialog.controller.busy
            onClicked: dialog.controller.startAutomatic()
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
            text: dialog.controller.selectedRouteWarning + "\n将用比较中显示的完整候选替换当前来源膜系，层数可能改变。是否按上面的真实分类应用？来源、目标、材料或项目发生变化时会拒绝整个应用。"
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
