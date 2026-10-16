import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ThemeControls.ThemeDialogBase {
    id: dialog
    objectName: "filmDesignImportDialog"
    required property var editor
    modal: true
    title: "从膜系导入工艺层"
    width: Math.min(1060, parent ? parent.width - 32 : 1060)
    height: Math.min(620, parent ? parent.height - 32 : 620)
    x: parent ? (parent.width - width) / 2 : 0
    y: parent ? (parent.height - height) / 2 : 0
    standardButtons: Dialog.NoButton

    function selectSurface(index) {
        if (index === 1) editor.setFilmDesignImportSurface("front")
        else if (index === 2) editor.setFilmDesignImportSurface("back")
    }

    ThemeControls.ThemeDialogBase {
        id: replaceDialog
        parent: Overlay.overlay
        palette: dialog.palette
        title: "替换当前步骤？"
        modal: true
        anchors.centerIn: Overlay.overlay
        width: Math.min(460, dialog.width - 32)
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Label {
            text: dialog.editor.filmDesignImportDiffSummary +
                  "\n导入将替换全部当前步骤及其膜系导入来源。当前步骤的目标厚度、速率和顺序编辑不会保留。确认继续？"
            wrapMode: Text.Wrap
        }
        onAccepted: {
            if (dialog.editor.applyFilmDesignImport(true)) dialog.close()
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: dialog.themeTokens.spaceSm

        RowLayout {
            Layout.fillWidth: true
            spacing: dialog.themeTokens.spaceSm
            ThemeControls.ThemeLabel { text: "来源"; Layout.preferredWidth: 40 }
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                elide: Text.ElideMiddle
                text: dialog.editor.filmDesignImportSummary.name.length
                      ? dialog.editor.filmDesignImportSourceLabel + " · " +
                        dialog.editor.filmDesignImportSummary.name + " · SHA-256 " +
                        dialog.editor.filmDesignImportSummary.sha256.slice(0, 12)
                      : dialog.editor.filmDesignImportSourceLabel || "未取得膜系设计"
            }
            ThemeControls.ThemeLabel { text: "导入面" }
            ThemeControls.ThemeComboBox {
                id: faceChoice
                objectName: "filmDesignImportFace"
                Layout.preferredWidth: 190
                model: ["请选择 front / back", "front · 正面", "back · 背面"]
                currentIndex: dialog.editor.filmDesignImportSurface === "front" ? 1
                            : dialog.editor.filmDesignImportSurface === "back" ? 2 : 0
                accessibleLabel: "选择要导入的膜系表面"
                onActivated: function(index) { dialog.selectSurface(index) }
            }
        }

        ThemeControls.ThemeLabel {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            muted: true
            text: dialog.editor.filmDesignImportSummary.name.length
                ? "厚度原始值与单位/QW、参考波长、用于 QW 换算的 n、材料与数据集均取自冻结快照。设计层按 substrate_outward 显示并生成工艺步骤；另一面保留在快照中但不执行，本版不支持翻面。"
                : "此功能读取当前打开的膜系设计，不要求先保存。请从活动膜系窗口发起并确认导入面。"
        }

        ThemeControls.ThemeLabel {
            objectName: "filmDesignImportDiffSummary"
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            heading: true
            text: dialog.editor.filmDesignImportDiffSummary
        }

        RowLayout {
            id: tableHeader
            Layout.fillWidth: true
            spacing: dialog.themeTokens.spaceSm
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: dialog.themeTokens.controlHeightMd
                color: dialog.themeTokens.colorSurfaceRaised
                border.color: dialog.themeTokens.colorBorder
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: dialog.themeTokens.spaceSm
                    anchors.rightMargin: dialog.themeTokens.spaceSm
                    spacing: dialog.themeTokens.spaceSm
                    ThemeControls.ThemeLabel { text: "层 / ID"; Layout.preferredWidth: 120; heading: true }
                    ThemeControls.ThemeLabel { text: "材料 / 数据集"; Layout.preferredWidth: 200; heading: true }
                    ThemeControls.ThemeLabel { text: "原始厚度 / QW 换算"; Layout.preferredWidth: 230; heading: true }
                    ThemeControls.ThemeLabel { text: "与当前差异"; Layout.preferredWidth: 160; heading: true }
                    ThemeControls.ThemeLabel { text: "机器蒸发源位（需逐材料选择）"; Layout.fillWidth: true; heading: true }
                }
            }
        }

        ListView {
            id: layerRows
            objectName: "filmDesignImportRows"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: dialog.themeTokens.spaceXs
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { }
            model: dialog.editor.filmDesignImportRows
            delegate: Rectangle {
                id: layerRow
                required property var modelData
                width: layerRows.width
                height: dialog.themeTokens.controlHeightMd * 2 + dialog.themeTokens.spaceXs
                radius: dialog.themeTokens.radiusSm
                color: dialog.themeTokens.colorSurface
                border.color: dialog.themeTokens.colorBorder

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: dialog.themeTokens.spaceSm
                    anchors.rightMargin: dialog.themeTokens.spaceSm
                    spacing: dialog.themeTokens.spaceSm
                    ColumnLayout {
                        Layout.preferredWidth: 120
                        spacing: 2
                        ThemeControls.ThemeLabel { text: "第 " + layerRow.modelData.index + " 层"; heading: true }
                        ThemeControls.ThemeLabel { text: layerRow.modelData.layer_id; elide: Text.ElideRight; Layout.fillWidth: true; muted: true }
                    }
                    ColumnLayout {
                        Layout.preferredWidth: 200
                        spacing: 2
                        ThemeControls.ThemeLabel { text: layerRow.modelData.material_name; elide: Text.ElideRight; Layout.fillWidth: true }
                        ThemeControls.ThemeLabel { text: layerRow.modelData.optical_set_name + " · " + layerRow.modelData.optical_set_id; elide: Text.ElideRight; Layout.fillWidth: true; muted: true }
                    }
                    ColumnLayout {
                        Layout.preferredWidth: 230
                        spacing: 2
                        ThemeControls.ThemeLabel {
                            text: String(layerRow.modelData.thickness_value) + " " + layerRow.modelData.thickness_unit +
                                  (layerRow.modelData.thickness_unit === "qw"
                                   ? " · λ0=" + layerRow.modelData.reference_wavelength_nm + " nm, n=" + layerRow.modelData.n_reference
                                   : "")
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                        ThemeControls.ThemeLabel {
                            text: "物理厚度 " + Number(layerRow.modelData.physical_thickness_nm).toLocaleString(Qt.locale(), 'f', 6) + " nm"
                            muted: true
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                    }
                    ThemeControls.ThemeLabel {
                        Layout.preferredWidth: 160
                        Layout.maximumWidth: 160
                        text: layerRow.modelData.change_note
                        elide: Text.ElideRight
                        wrapMode: Text.WordWrap
                    }
                    ThemeControls.ThemeComboBox {
                        id: sourceChoice
                        objectName: "filmDesignSourceChoice_" + layerRow.modelData.material_id
                        Layout.fillWidth: true
                        Layout.minimumWidth: 200
                        model: (dialog.editor.filmDesignImportSources || []).filter(function(item) { return item.valid })
                        textRole: "label"
                        currentIndex: {
                            const sources = dialog.editor.filmDesignImportSources || []
                            const available = sources.filter(function(item) { return item.valid })
                            return available.findIndex(function(item) { return item.slot === layerRow.modelData.source_slot })
                        }
                        displayText: currentIndex >= 0 ? currentText : "请选择有效蒸发源位"
                        accessibleLabel: "材料 " + layerRow.modelData.material_name + " 的机器蒸发源位"
                        accessibleDescription: "必须显式选择；系统不会按材料名称或编号推断。"
                        onActivated: function(index) {
                            const source = model[index]
                            if (source && source.valid)
                                dialog.editor.setFilmDesignImportSource(layerRow.modelData.material_id, source.slot)
                        }
                    }
                }
            }
            ThemeControls.ThemeLabel {
                anchors.centerIn: parent
                width: parent.width - 32
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
                visible: !dialog.editor.filmDesignImportRows.length
                text: dialog.editor.filmDesignImportError.length
                      ? dialog.editor.filmDesignImportError
                      : "请先明确选择 front 或 back 面；单面基片的 back 面不可导入。"
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: dialog.themeTokens.spaceSm
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                color: dialog.editor.filmDesignImportError.length
                       ? dialog.themeTokens.colorError
                       : dialog.themeTokens.colorTextMuted
                text: dialog.editor.filmDesignImportError.length
                    ? dialog.editor.filmDesignImportError
                    : dialog.editor.filmDesignImportMissingSourceCount > 0
                      ? "尚有 " + dialog.editor.filmDesignImportMissingSourceCount + " 种材料未映射到有效机器蒸发源位。"
                      : dialog.editor.filmDesignImportReadyToApply
                        ? "映射完整。点击导入后还需确认替换当前步骤。"
                        : "等待有效膜系快照、导入面和机器源映射。"
            }
            ThemeControls.ThemeButton { text: "取消"; onClicked: dialog.close() }
            ThemeControls.ThemeButton {
                objectName: "applyFilmDesignImportButton"
                text: "导入并替换步骤…"
                primary: true
                enabled: dialog.editor.filmDesignImportReadyToApply
                onClicked: replaceDialog.open()
            }
        }
    }
}
