pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ColumnLayout {
    id: panel
    objectName: "filmInteractiveAnalysisPanel"
    required property var analysis
    required property var themeTokens
    Layout.fillWidth: true
    spacing: panel.themeTokens.spaceSm

    Label {
        Layout.fillWidth: true
        text: "所有正/背面膜层逐位调整；只预览临时副本，明确应用后才写入设计。光谱同步显示于独立仿真窗口。"
        color: panel.themeTokens.colorTextMuted
        wrapMode: Text.WordWrap
    }
    ThemeControls.ThemeButton {
        objectName: "filmInteractiveStartButton"
        visible: !panel.analysis.interactiveAnalysisActive
        Layout.fillWidth: true
        text: "开始交互式分析"
        primary: true
        onClicked: panel.analysis.startInteractiveAnalysis()
    }
    ThemeControls.ThemeComboBox {
        objectName: "filmInteractiveAnalysisMode"
        Layout.fillWidth: true
        model: panel.analysis.interactiveAnalysisModeOptions
        textRole: "label"
        currentIndex: Math.max(0, panel.analysis.interactiveAnalysisModeOptions.findIndex(
            function(item) { return item.id === panel.analysis.interactiveAnalysisMode }))
        enabled: panel.analysis.interactiveAnalysisCanEdit
        onActivated: {
            const option = panel.analysis.interactiveAnalysisModeOptions[currentIndex]
            if (option) panel.analysis.setInteractiveAnalysisMode(option.id)
        }
    }
    Label {
        objectName: "filmInteractiveAnalysisStatus"
        Layout.fillWidth: true
        text: panel.analysis.interactiveAnalysisStatus
        color: panel.analysis.interactiveAnalysisBusy ? panel.themeTokens.colorInfo
               : panel.analysis.interactiveAnalysisCanApply ? panel.themeTokens.colorSuccess
               : panel.themeTokens.colorText
        wrapMode: Text.WordWrap
    }
    Label {
        objectName: "filmInteractiveAnalysisError"
        visible: panel.analysis.interactiveAnalysisError.length > 0
        Layout.fillWidth: true
        text: panel.analysis.interactiveAnalysisError
              + (panel.analysis.interactiveAnalysisSpectrum.length > 0
                 ? "\n图中仍显示最近一次有效临时光谱。" : "")
        color: panel.themeTokens.colorError
        wrapMode: Text.WordWrap
    }

    ColumnLayout {
        visible: panel.analysis.interactiveAnalysisMode === "incident_angle"
        Layout.fillWidth: true
        Label { text: "入射角（°）"; color: panel.themeTokens.colorText }
        ThemeControls.ThemeField {
            objectName: "filmInteractiveAnalysisAngle"
            Layout.fillWidth: true
            enabled: panel.analysis.interactiveAnalysisCanEdit
            numericDisplayValue: panel.analysis.interactiveAnalysisAngle
            numericEditDecimals: 4
            onEditingFinished: panel.analysis.setInteractiveAngle(text)
        }
    }

    ColumnLayout {
        visible: panel.analysis.interactiveAnalysisMode === "group_coefficient"
        Layout.fillWidth: true
        Label {
            Layout.fillWidth: true
            text: "组系数以本次打开时的原始厚度为基准计算，不累乘；未分组膜层保持原厚度。"
            color: panel.themeTokens.colorInfo
            wrapMode: Text.WordWrap
        }
        Repeater {
            objectName: "filmInteractiveAnalysisGroupFactors"
            model: panel.analysis.interactiveAnalysisGroups
            delegate: RowLayout {
                id: analysisGroup
                required property var modelData
                Layout.fillWidth: true
                Label {
                    Layout.fillWidth: true
                    text: "组 " + analysisGroup.modelData.group_id
                          + " · " + analysisGroup.modelData.members + " 层"
                    color: panel.themeTokens.colorText
                }
                ThemeControls.ThemeButton {
                    text: "−"
                    flat: true
                    enabled: panel.analysis.interactiveAnalysisCanEdit
                    onClicked: panel.analysis.adjustInteractiveGroupFactor(
                                   String(analysisGroup.modelData.group_id), -0.01)
                }
                ThemeControls.ThemeField {
                    objectName: "filmInteractiveGroupFactor_" + analysisGroup.modelData.group_id
                    Layout.preferredWidth: 80
                    enabled: panel.analysis.interactiveAnalysisCanEdit
                    numericDisplayValue: analysisGroup.modelData.coefficient
                    numericEditDecimals: 4
                    onEditingFinished: panel.analysis.setInteractiveGroupFactor(
                                           String(analysisGroup.modelData.group_id), text)
                }
                ThemeControls.ThemeButton {
                    text: "+"
                    flat: true
                    enabled: panel.analysis.interactiveAnalysisCanEdit
                    onClicked: panel.analysis.adjustInteractiveGroupFactor(
                                   String(analysisGroup.modelData.group_id), 0.01)
                }
            }
        }
        Label {
            visible: panel.analysis.interactiveAnalysisGroups.length === 0
            Layout.fillWidth: true
            text: "当前没有已分组膜层；可在下方批量设置组号。"
            color: panel.themeTokens.colorTextMuted
            wrapMode: Text.WordWrap
        }
    }

    Label {
        Layout.fillWidth: true
        text: panel.analysis.interactiveAnalysisMode === "optical_qw"
              ? "膜层厚度（QW）；显示精度跟随厚度显示设置。箭头逐位进退，点击数字可直接替换该位。"
              : "膜层厚度（nm）；显示精度跟随厚度显示设置。箭头逐位进退，点击数字可直接替换该位。"
        color: panel.themeTokens.colorTextMuted
        wrapMode: Text.WordWrap
    }
    Repeater {
        objectName: "filmInteractiveAnalysisLayerRows"
        model: panel.analysis.interactiveAnalysisRows
        delegate: Rectangle {
            id: analysisLayer
            required property var modelData
            required property int index
            Layout.fillWidth: true
            implicitHeight: layerContents.implicitHeight + panel.themeTokens.spaceSm * 2
            color: analysisLayer.modelData.active
                   ? panel.themeTokens.colorSurfaceRaised : panel.themeTokens.colorSurface
            border.color: panel.themeTokens.colorBorder
            radius: panel.themeTokens.radiusSm
            ColumnLayout {
                id: layerContents
                anchors.fill: parent
                anchors.margins: panel.themeTokens.spaceSm
                spacing: panel.themeTokens.spaceXs
                Label {
                    Layout.fillWidth: true
                    text: analysisLayer.modelData.side_label + " "
                          + analysisLayer.modelData.layer_number + " · "
                          + analysisLayer.modelData.material + " · #"
                          + analysisLayer.modelData.layer_id
                          + (analysisLayer.modelData.group_id.length > 0
                             ? " · 组 " + analysisLayer.modelData.group_id : "")
                          + (analysisLayer.modelData.active ? "" : " · 未启用")
                    color: analysisLayer.modelData.active
                           ? panel.themeTokens.colorText : panel.themeTokens.colorTextMuted
                    wrapMode: Text.WrapAnywhere
                }
                Label {
                    Layout.fillWidth: true
                    text: "原 " + analysisLayer.modelData.original_text + " "
                          + analysisLayer.modelData.unit + "  →  当前 "
                          + analysisLayer.modelData.current_text + " "
                          + analysisLayer.modelData.unit
                    color: panel.themeTokens.colorInfo
                    wrapMode: Text.WordWrap
                }
                Flow {
                    Layout.fillWidth: true
                    spacing: panel.themeTokens.spaceXs
                    Repeater {
                        model: analysisLayer.modelData.digits
                        delegate: Column {
                            id: analysisDigit
                            required property var modelData
                            width: 34
                            spacing: 0
                            ThemeControls.ThemeButton {
                                objectName: "filmInteractiveDigitUp_"
                                             + analysisLayer.modelData.side + "_"
                                             + analysisLayer.modelData.layer_id + "_"
                                             + analysisDigit.modelData.place
                                text: "▲"
                                flat: true
                                visible: !analysisDigit.modelData.decimal
                                enabled: analysisLayer.modelData.editable
                                width: 32
                                height: 18
                                onClicked: panel.analysis.adjustInteractiveDigit(
                                               analysisLayer.modelData.side,
                                               analysisLayer.modelData.layer_id,
                                               analysisDigit.modelData.place, 1)
                            }
                            ThemeControls.ThemeField {
                                objectName: "filmInteractiveDigit_"
                                             + analysisLayer.modelData.side + "_"
                                             + analysisLayer.modelData.layer_id + "_"
                                             + analysisDigit.modelData.place
                                visible: !analysisDigit.modelData.decimal
                                width: 32
                                height: 30
                                enabled: analysisLayer.modelData.editable
                                text: String(analysisDigit.modelData.digit)
                                horizontalAlignment: TextInput.AlignHCenter
                                selectAllOnFirstFocus: true
                                inputMethodHints: Qt.ImhDigitsOnly | Qt.ImhNoPredictiveText
                                onEditingFinished: {
                                    if (/^[0-9]$/.test(text))
                                        panel.analysis.setInteractiveDigit(
                                            analysisLayer.modelData.side,
                                            analysisLayer.modelData.layer_id,
                                            analysisDigit.modelData.place, text)
                                }
                            }
                            Label {
                                visible: analysisDigit.modelData.decimal
                                width: 32
                                height: 30
                                text: "."
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                                color: panel.themeTokens.colorTextMuted
                            }
                            ThemeControls.ThemeButton {
                                objectName: "filmInteractiveDigitDown_"
                                             + analysisLayer.modelData.side + "_"
                                             + analysisLayer.modelData.layer_id + "_"
                                             + analysisDigit.modelData.place
                                text: "▼"
                                flat: true
                                visible: !analysisDigit.modelData.decimal
                                enabled: analysisLayer.modelData.editable
                                width: 32
                                height: 18
                                onClicked: panel.analysis.adjustInteractiveDigit(
                                               analysisLayer.modelData.side,
                                               analysisLayer.modelData.layer_id,
                                               analysisDigit.modelData.place, -1)
                            }
                        }
                    }
                }
            }
        }
    }

    Label {
        visible: panel.analysis.interactiveAnalysisMode === "incident_angle"
        Layout.fillWidth: true
        text: "入射角模式只应用仿真角度；批量膜层修改请切换到物理厚度、QW 或组系数模式。"
        color: panel.themeTokens.colorWarning
        wrapMode: Text.WordWrap
    }
    Label {
        Layout.fillWidth: true
        text: "批量修改 · 筛选条件为空表示不筛选；替换值留空表示不修改，厚度 0 有效。"
        color: panel.themeTokens.colorText
        font.weight: panel.themeTokens.fontWeightEmphasis
        wrapMode: Text.WordWrap
    }
    ThemeControls.ThemeComboBox {
        id: interactiveBatchSide
        objectName: "filmInteractiveBatchSide"
        Layout.fillWidth: true
        model: [{id: "", label: "正背面均不筛选"},
                {id: "front", label: "仅正面"},
                {id: "back", label: "仅背面"}]
        textRole: "label"
        currentIndex: 0
    }
    ThemeControls.ThemeComboBox {
        id: interactiveBatchMaterial
        objectName: "filmInteractiveBatchMaterial"
        Layout.fillWidth: true
        model: panel.analysis.interactiveAnalysisMaterialOptions
        textRole: "name"
        currentIndex: 0
    }
    RowLayout {
        Layout.fillWidth: true
        Label { text: "筛选组"; color: panel.themeTokens.colorText }
        ThemeControls.ThemeField {
            id: interactiveBatchFilterGroup
            objectName: "filmInteractiveBatchFilterGroup"
            Layout.fillWidth: true
            placeholderText: "空 = 全部"
            inputMethodHints: Qt.ImhDigitsOnly
        }
    }
    ThemeControls.ThemeComboBox {
        id: interactiveBatchReplacement
        objectName: "filmInteractiveBatchReplacement"
        Layout.fillWidth: true
        model: panel.analysis.interactiveReplacementOptions
        textRole: "label"
        currentIndex: 0
        enabled: panel.analysis.interactiveAnalysisCanEdit
    }
    RowLayout {
        Layout.fillWidth: true
        Label {
            text: panel.analysis.interactiveAnalysisMode === "optical_qw"
                  ? "厚度 (QW)" : "厚度 (nm)"
            color: panel.themeTokens.colorText
        }
        ThemeControls.ThemeField {
            id: interactiveBatchThickness
            objectName: "filmInteractiveBatchThickness"
            Layout.fillWidth: true
            placeholderText: "留空 = 不改"
            inputMethodHints: Qt.ImhFormattedNumbersOnly
            enabled: panel.analysis.interactiveAnalysisMode === "physical_nm"
                     || panel.analysis.interactiveAnalysisMode === "optical_qw"
        }
    }
    RowLayout {
        Layout.fillWidth: true
        Label { text: "新组号"; color: panel.themeTokens.colorText }
        ThemeControls.ThemeField {
            id: interactiveBatchGroup
            objectName: "filmInteractiveBatchGroup"
            Layout.fillWidth: true
            placeholderText: "留空 = 不改"
            inputMethodHints: Qt.ImhDigitsOnly
        }
        CheckBox {
            id: interactiveBatchClearGroup
            objectName: "filmInteractiveBatchClearGroup"
            text: "取消分组"
            checked: false
        }
    }
    ThemeControls.ThemeButton {
        objectName: "filmInteractiveBatchApply"
        Layout.fillWidth: true
        text: "将批量修改放入临时分析"
        enabled: panel.analysis.interactiveAnalysisCanEdit
                 && panel.analysis.interactiveAnalysisMode !== "incident_angle"
        onClicked: panel.analysis.applyInteractiveBatch({
            filter: {
                side: interactiveBatchSide.currentIndex === 0 ? ""
                      : interactiveBatchSide.model[interactiveBatchSide.currentIndex].id,
                material_id: interactiveBatchMaterial.currentIndex === 0 ? ""
                             : interactiveBatchMaterial.model[interactiveBatchMaterial.currentIndex].id,
                group_id: interactiveBatchFilterGroup.text
            },
            changes: {
                thickness_value: interactiveBatchThickness.text,
                material_id: interactiveBatchReplacement.currentIndex === 0 ? ""
                    : interactiveBatchReplacement.model[interactiveBatchReplacement.currentIndex].material_id,
                optical_set_id: interactiveBatchReplacement.currentIndex === 0 ? ""
                    : interactiveBatchReplacement.model[interactiveBatchReplacement.currentIndex].optical_set_id,
                group_id: interactiveBatchGroup.text,
                clear_group: interactiveBatchClearGroup.checked
            }
        })
    }

    Label {
        Layout.fillWidth: true
        text: "临时光谱预览 · R / T / A"
        color: panel.themeTokens.colorText
        font.weight: panel.themeTokens.fontWeightEmphasis
    }
    Canvas {
        id: interactiveAnalysisCanvas
        objectName: "filmInteractiveAnalysisSpectrum"
        Layout.fillWidth: true
        Layout.preferredHeight: 150
        onPaint: {
            const context = getContext("2d")
            context.clearRect(0, 0, width, height)
            const values = panel.analysis.interactiveAnalysisSpectrum
            if (!values || values.length < 2) return
            const left = 30, right = width - 8, top = 8, bottom = height - 20
            context.strokeStyle = panel.themeTokens.colorBorder
            context.lineWidth = 1
            context.beginPath(); context.moveTo(left, top); context.lineTo(left, bottom)
            context.lineTo(right, bottom); context.stroke()
            const colors = [panel.themeTokens.colorAccent,
                            panel.themeTokens.colorSuccess,
                            panel.themeTokens.colorWarning]
            const fields = ["R", "T", "A"]
            for (let c = 0; c < fields.length; ++c) {
                context.strokeStyle = colors[c]
                context.lineWidth = 1.6
                context.beginPath()
                for (let i = 0; i < values.length; ++i) {
                    const x = left + i * (right - left) / (values.length - 1)
                    const y = bottom - Math.max(0, Math.min(1, Number(values[i][fields[c]])))
                                    * (bottom - top)
                    if (i === 0) context.moveTo(x, y); else context.lineTo(x, y)
                }
                context.stroke()
            }
            context.fillStyle = panel.themeTokens.colorTextMuted
            context.font = "10px sans-serif"
            context.fillText(String(values[0].wavelength_nm) + " nm", left, height - 4)
            context.fillText(String(values[values.length - 1].wavelength_nm) + " nm", right - 50, height - 4)
        }
        Connections {
            target: panel.analysis
            function onChanged() { interactiveAnalysisCanvas.requestPaint() }
        }
        Component.onCompleted: requestPaint()
    }
    ThemeControls.ThemeButton {
        objectName: "filmInteractiveOpenSimulation"
        Layout.fillWidth: true
        text: "在独立光谱仿真窗口中查看"
        enabled: !panel.analysis.interactiveAnalysisBusy
        onClicked: panel.analysis.openSimulationWindow()
    }
    RowLayout {
        Layout.fillWidth: true
        Label { text: "R"; color: panel.themeTokens.colorAccent }
        Label { text: "T"; color: panel.themeTokens.colorSuccess }
        Label { text: "A"; color: panel.themeTokens.colorWarning }
    }
    RowLayout {
        Layout.fillWidth: true
        ThemeControls.ThemeButton {
            objectName: "filmInteractiveAnalysisReset"
            Layout.fillWidth: true
            text: "重置"
            enabled: panel.analysis.interactiveAnalysisCanEdit
            onClicked: panel.analysis.resetInteractiveAnalysis()
        }
        ThemeControls.ThemeButton {
            objectName: "filmInteractiveAnalysisCancel"
            Layout.fillWidth: true
            text: "取消"
            onClicked: panel.analysis.clearInteractiveAnalysis()
        }
        ThemeControls.ThemeButton {
            objectName: "filmInteractiveAnalysisApply"
            Layout.fillWidth: true
            primary: true
            text: "应用到设计"
            enabled: panel.analysis.interactiveAnalysisCanApply
            onClicked: panel.analysis.applyInteractiveAnalysis()
        }
    }
}
