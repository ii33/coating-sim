pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ColumnLayout {
    id: panel
    objectName: "filmManufacturingAlgorithmOptions"
    required property var controller
    required property var optimizationController
    required property var themeTokens
    spacing: themeTokens.spaceSm
    visible: panel.optimizationController.searchMode === "automatic_repair"

    ThemeControls.ThemeLabel {
        objectName: "filmManufacturingScopeHelp"
        Layout.fillWidth: true
        text: "自动识别低于厚度下限的正面层；候选须通过相对原曲线的最大变化保护和独立光学核验。只显示完整核验改进，不改变原设计，取消时沿用既有契约尝试应用当前最佳改进。"
        wrapMode: Text.WordWrap
    }
    ThemeControls.ThemeLabel {
        Layout.fillWidth: true
        muted: true
        text: panel.controller.comparisonScopeSummary
        wrapMode: Text.WordWrap
    }
    RowLayout {
        Layout.fillWidth: true
        ThemeControls.ThemeButton {
            objectName: "filmManufacturingRefresh"
            text: "刷新当前来源"
            enabled: !panel.controller.busy
            onClicked: panel.controller.prepare()
        }
        ThemeControls.ThemeLabel {
            Layout.fillWidth: true
            text: "仅检测并修复当前前层；不使用手动层选择。"
            wrapMode: Text.WordWrap
            muted: true
        }
    }
    ListView {
        objectName: "filmManufacturingLayerList"
        Layout.fillWidth: true
        Layout.preferredHeight: Math.min(116, contentHeight)
        clip: true
        model: panel.controller.layerRows
        delegate: ThemeControls.ThemeLabel {
            required property var modelData
            width: ListView.view.width
            text: modelData.label + " · " + modelData.thickness
            wrapMode: Text.WordWrap
        }
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
    }
    GridLayout {
        Layout.fillWidth: true
        columns: panel.width > 600 ? 2 : 1
        columnSpacing: panel.themeTokens.spaceSm
        rowSpacing: panel.themeTokens.spaceXs
        Repeater {
            objectName: "filmManufacturingParameterRepeater"
            model: [
                {key: "lower_limit_nm", label: "单层厚度下限（nm）",
                 help: "低于此物理厚度的前层会自动识别；0 不是目标总厚度。"},
                {key: "optical_tolerance_percentage_points", label: "相对原曲线最大变化（百分点）",
                 help: "训练与独立审计逐点核验；1 表示 R/T 的最大绝对差不超过 1 个百分点，不是相对百分比。"}
            ]
            delegate: ColumnLayout {
                id: optionCell
                required property var modelData
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                ThemeControls.ThemeLabel {
                    objectName: "filmManufacturingLabel_" + optionCell.modelData.key
                    Layout.fillWidth: true
                    text: optionCell.modelData.label
                    wrapMode: Text.Wrap
                }
                ThemeControls.ThemeField {
                    objectName: "filmManufacturing_" + optionCell.modelData.key
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    accessibleLabel: optionCell.modelData.label
                    accessibleDescription: optionCell.modelData.help
                    enabled: !panel.controller.busy
                    text: panel.controller.automaticOptions[optionCell.modelData.key]
                    placeholderText: "填写非负有限值"
                    onTextEdited: panel.controller.setAutomaticOption(optionCell.modelData.key, text)
                    onEditingFinished: panel.controller.finishAutomaticOptionEdit(optionCell.modelData.key)
                }
                ThemeControls.ThemeLabel {
                    objectName: "filmManufacturingHelp_" + optionCell.modelData.key
                    Layout.fillWidth: true
                    muted: true
                    text: optionCell.modelData.help
                    wrapMode: Text.WordWrap
                }
            }
        }
    }
    ThemeControls.ThemeLabel {
        objectName: "filmManufacturingStatus"
        Layout.fillWidth: true
        text: panel.controller.statusText
        wrapMode: Text.WordWrap
    }
    ThemeControls.ThemeLabel {
        objectName: "filmManufacturingError"
        Layout.fillWidth: true
        visible: text.length > 0
        text: panel.controller.error
        color: panel.themeTokens.colorError
        wrapMode: Text.WordWrap
    }
    ThemeControls.ThemeSection {
        Layout.fillWidth: true
        title: "实时厚度与光学反馈"
        visible: panel.controller.hasLivePreview || panel.controller.busy
        ThemeControls.ThemeLabel {
            objectName: "filmRepairLiveSummary"
            Layout.fillWidth: true
            text: panel.controller.liveSummary
            wrapMode: Text.WordWrap
        }
        ThemeControls.ThemeLabel {
            objectName: "filmRepairLiveTotal"
            Layout.fillWidth: true
            text: "原设计：" + panel.controller.liveTotals.original_layers + " 层 / "
                + panel.controller.liveTotals.original + "；当前："
                + panel.controller.liveTotals.current_layers + " 层 / "
                + panel.controller.liveTotals.current + "；最佳："
                + panel.controller.liveTotals.best_layers + " 层 / "
                + panel.controller.liveTotals.best
            wrapMode: Text.WordWrap
        }
        ThemeControls.ThemeLabel {
            objectName: "filmRepairLiveOpticalSummary"
            Layout.fillWidth: true
            text: panel.controller.liveOpticalSummary
            wrapMode: Text.WordWrap
        }
        ThemeControls.ThemeLabel {
            Layout.fillWidth: true
            muted: true
            text: "背景条按本轮厚度刻度显示当前试探厚度；逐层状态区分试探与完整核验改进。"
            wrapMode: Text.WordWrap
        }
        ListView {
            objectName: "filmRepairLiveLayerScroll"
            Layout.fillWidth: true
            Layout.preferredHeight: 220
            Layout.minimumHeight: 80
            clip: true
            model: panel.controller.liveLayerRows
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
            delegate: Item {
                id: liveRow
                required property var modelData
                objectName: "filmRepairLiveLayer_" + modelData.row_id
                width: ListView.view.width
                implicitHeight: values.implicitHeight + 2 * panel.themeTokens.spaceXs
                Rectangle {
                    objectName: "filmRepairLiveBar_" + liveRow.modelData.row_id
                    anchors.left: parent.left
                    anchors.top: parent.top
                    anchors.bottom: parent.bottom
                    width: parent.width * liveRow.modelData.current_ratio
                    radius: panel.themeTokens.radiusSm
                    color: panel.themeTokens.colorAccent
                    opacity: liveRow.modelData.best_deleted ? 0.04 : 0.12
                }
                ColumnLayout {
                    id: values
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: panel.themeTokens.spaceXs
                    ThemeControls.ThemeLabel {
                        Layout.fillWidth: true
                        text: liveRow.modelData.label
                        wrapMode: Text.WordWrap
                    }
                    ThemeControls.ThemeLabel {
                        objectName: "filmRepairLiveValues_" + liveRow.modelData.row_id
                        Layout.fillWidth: true
                        color: panel.themeTokens.colorText
                        text: "原厚：" + liveRow.modelData.original + "；当前："
                            + (liveRow.modelData.current_deleted ? "0 nm（已消层）" : liveRow.modelData.current)
                            + "（变化 " + liveRow.modelData.current_delta + "）；最佳改进："
                            + (liveRow.modelData.best_deleted ? "0 nm（已消层）" : liveRow.modelData.best)
                            + "（变化 " + liveRow.modelData.best_delta + "）"
                        wrapMode: Text.WordWrap
                    }
                    ThemeControls.ThemeLabel {
                        Layout.fillWidth: true
                        muted: true
                        text: liveRow.modelData.status
                        wrapMode: Text.WordWrap
                    }
                }
            }
        }
    }
    FilmManufacturingComparison {
        Layout.fillWidth: true
        visible: panel.controller.hasResult
        controller: panel.controller
        themeTokens: panel.themeTokens
    }
    RowLayout {
        Layout.fillWidth: true
        visible: panel.controller.hasResult
        ThemeControls.ThemeLabel {
            Layout.fillWidth: true
            text: "报告可另存回看；应用仍须显式确认来源与候选。"
            muted: true
            wrapMode: Text.WordWrap
        }
        ThemeControls.ThemeButton {
            objectName: "filmManufacturingSave"
            text: "保存报告…"
            enabled: panel.controller.hasResult && !panel.controller.busy
            onClicked: reportDialog.open()
        }
    }

    FileDialog {
        id: reportDialog
        title: "保存完整薄层修复报告"
        fileMode: FileDialog.SaveFile
        nameFilters: ["JSON report (*.json)"]
        defaultSuffix: "json"
        onAccepted: panel.controller.saveReportUrl(selectedFile, false)
    }
    Connections {
        target: panel.controller
        function onOverwriteRequested(path) {
            overwriteDialog.targetPath = path
            overwriteDialog.open()
        }
    }
    ThemeControls.ThemeDialogBase {
        id: overwriteDialog
        property string targetPath: ""
        title: "覆盖已有报告？"
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        ThemeControls.ThemeLabel {
            text: overwriteDialog.targetPath
            wrapMode: Text.WordWrap
        }
        onAccepted: panel.controller.saveReport(targetPath, true)
    }
}
