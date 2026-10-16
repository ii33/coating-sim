import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "."
import "theme_controls" as ThemeControls

Item {
    id: workspace
    required property var controller
    property bool showAssetSelector: false
    property bool showModeSelector: false
    property bool activeWorkspace: true
    property bool externalBusy: false
    property bool compactActions: width < 1000
    property bool parametersExpanded: true
    property string selectedCategory: "asset"
    property string spectrumTarget: "area"
    property string previewMode: "scene"
    property int themeRevision: Theme.revision
    signal selectAssetRequested()
    signal geometricModeRequested()
    signal categorySelectionRequested(string category)

    function focusAssetAction() {
        assetSelectionButton.forceActiveFocus()
    }

    function toggleParameters() {
        // Preserve the original editingFinished boundary before hiding a focused editor.
        parametersButton.forceActiveFocus()
        parametersExpanded = !parametersExpanded
    }

    function remainingTime(seconds) {
        if (!isFinite(seconds) || seconds < 0)
            return "剩余时间待估"
        const rounded = Math.ceil(seconds)
        if (rounded < 60)
            return "预计剩余 " + rounded + " 秒"
        if (rounded >= 3600)
            return "预计剩余 " + Math.floor(rounded / 3600) + " 小时 "
                   + Math.floor((rounded % 3600) / 60) + " 分"
        const minutes = Math.floor(rounded / 60)
        const remainder = rounded % 60
        return "预计剩余 " + minutes + " 分 " + remainder + " 秒"
    }

    function stageText(stage) {
        const names = {
            "preflight": "预检", "asset_preflight": "资产预检",
            "field_load": "读取厚度场", "asset_read": "读取资产",
            "film_adapter": "关联膜层", "film_adapter_complete": "膜层已校验",
            "job_validation": "校验任务", "job_building": "准备渲染器",
            "job_ready": "输入已就绪",
            "uploading": controller.computeTarget.hostId === "local" ? "上传 GPU 数据" : "上传资产",
            "rendering": "逐波长渲染", "integrating": "光谱积分",
            "complete": "结果确认", "cancellation boundary": "等待取消边界",
            "cancelled": "已取消", "failed": "失败", "submitting": "提交远端任务",
            "queued": "远端排队", "reconnecting": "重新连接远端",
            "downloading": "下载并验证结果", "cancelling": "等待远端取消"
        }
        return names[stage] || stage || "启动中"
    }

    FileDialog {
        id: spectrumDialog
        objectName: "appearanceSpectrumDialog"
        title: "导入波长与标量光谱 CSV"
        fileMode: FileDialog.OpenFile
        nameFilters: ["标量光谱 CSV (*.csv *.txt)", "所有文件 (*)"]
        onAccepted: controller.loadSpectrum(selectedFile.toString(), workspace.spectrumTarget)
    }

    Shortcut {
        sequence: "F4"
        context: Qt.WindowShortcut
        enabled: workspace.activeWorkspace
        onActivated: workspace.toggleParameters()
    }

    component NumberField: RowLayout {
        required property var host
        required property string label
        required property string fieldKey
        Layout.fillWidth: true
        spacing: Theme.snapshot.spaceXs
        ThemeControls.ThemeLabel {
            text: label
            muted: true
            Layout.preferredWidth: 100
            Layout.minimumWidth: 72
            wrapMode: Text.WordWrap
        }
        ThemeControls.ThemeField {
            objectName: "appearanceField_" + fieldKey
            text: String(host.draft[fieldKey] ?? "")
            Layout.fillWidth: true
            Layout.minimumWidth: 64
            selectByMouse: true
            horizontalAlignment: TextInput.AlignRight
            validator: DoubleValidator { decimals: 12; notation: DoubleValidator.StandardNotation }
            onEditingFinished: host.setField(fieldKey, text)
            accessibleLabel: label
        }
    }

    component VectorFields: ColumnLayout {
        required property var host
        required property string label
        required property string prefix
        Layout.fillWidth: true
        spacing: Theme.snapshot.spaceXs
        ThemeControls.ThemeLabel { text: label; muted: true }
        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.snapshot.spaceXs
            Repeater {
                model: ["x", "y", "z"]
                delegate: RowLayout {
                    required property string modelData
                    Layout.fillWidth: true
                    spacing: Theme.snapshot.spaceXs
                    ThemeControls.ThemeLabel {
                        text: modelData.toUpperCase()
                        muted: true
                        font.pointSize: Theme.snapshot.fontSizeSmall
                    }
                    ThemeControls.ThemeField {
                        objectName: "appearanceVector_" + prefix + modelData
                        text: String(host.draft[prefix + modelData] ?? "")
                        Layout.fillWidth: true
                        Layout.minimumWidth: 44
                        selectByMouse: true
                        horizontalAlignment: TextInput.AlignRight
                        validator: DoubleValidator { decimals: 12; notation: DoubleValidator.StandardNotation }
                        onEditingFinished: host.setField(prefix + modelData, text)
                        accessibleLabel: label + " " + modelData.toUpperCase()
                    }
                }
            }
        }
    }

    ColumnLayout {
        objectName: "appearanceContentLayout"
        anchors.fill: parent
        anchors.leftMargin: Theme.snapshot.spaceMd
        anchors.rightMargin: Theme.snapshot.spaceMd
        anchors.topMargin: Theme.snapshot.spaceSm
        anchors.bottomMargin: Theme.snapshot.spaceMd
        spacing: Theme.snapshot.spaceMd

        Rectangle {
            objectName: "appearanceToolbar"
            Accessible.name: "操作栏"
            Layout.fillWidth: true
            Layout.preferredHeight: Math.max(58, actionBar.implicitHeight + Theme.snapshot.spaceSm * 2)
            radius: Theme.snapshot.radiusMd
            color: Theme.snapshot.colorSurface
            border.color: Theme.snapshot.colorBorder
            RowLayout {
                id: actionBar
                objectName: "appearanceActionBar"
                anchors.fill: parent
                anchors.margins: Theme.snapshot.spaceSm
                spacing: Theme.snapshot.spaceXs
                ThemeControls.ThemeComboBox {
                    objectName: "appearanceWorkspaceMode"
                    visible: workspace.showModeSelector
                    Layout.preferredWidth: workspace.compactActions ? 164 : 210
                    Layout.minimumWidth: 138
                    model: ["外观渲染", "几何光追（高级）"]
                    currentIndex: 0
                    Accessible.name: "工作模式"
                    onActivated: if (index === 1) workspace.geometricModeRequested()
                }
                ThemeControls.ThemeButton {
                    id: assetSelectionButton
                    objectName: "appearanceSelectAssetButton"
                    visible: workspace.showAssetSelector
                    text: "选择逐层资产"
                    enabled: !controller.running && !workspace.externalBusy
                    onClicked: workspace.selectAssetRequested()
                }
                ThemeControls.ThemeLabel {
                    objectName: "appearanceContextSummary"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 40
                    text: controller.contextSummary.replace(/\n/g, " · ")
                    muted: true
                    elide: Text.ElideMiddle
                    Accessible.description: controller.contextSummary
                    ToolTip.visible: contextHover.hovered
                    ToolTip.text: controller.contextSummary
                    HoverHandler { id: contextHover }
                }
                ThemeControls.ThemeButton {
                    id: parametersButton
                    objectName: "appearanceParametersButton"
                    text: workspace.compactActions ? "参数" : "外观参数"
                    checkable: true
                    checked: workspace.parametersExpanded
                    Accessible.name: workspace.parametersExpanded ? "收起外观参数栏" : "展开外观参数栏"
                    ToolTip.visible: hovered
                    ToolTip.text: Accessible.name + "（F4）"
                    onClicked: workspace.toggleParameters()
                }
                ThemeControls.ThemeButton {
                    objectName: "appearanceCancelButton"
                    text: controller.phase === "cancelling"
                          ? (workspace.compactActions ? "取消中"
                             : controller.computeTarget.hostId === "local" ? "等待帧边界" : "等待远端确认") : "取消"
                    enabled: controller.canCancel
                    Accessible.name: controller.computeTarget.hostId === "local"
                                     ? "取消本地外观渲染" : "取消远端外观渲染"
                    onClicked: controller.cancelRender()
                }
                ComputeTargetSelector {
                    Layout.preferredWidth: 140
                    Layout.minimumWidth: 110
                    compact: true
                    target: controller.computeTarget
                    enabled: !controller.running
                }
                ThemeControls.ThemeButton {
                    objectName: "appearanceRunButton"
                    text: controller.phase === "running" || controller.phase === "starting"
                          ? "正在渲染" : "渲染外观"
                    primary: true
                    enabled: controller.canStart && controller.computeTarget.canSubmit
                             && !workspace.externalBusy
                    onClicked: controller.startRender()
                }
            }
        }

        RowLayout {
            id: resultArea
            objectName: "appearanceContentSplit"
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: Theme.snapshot.spaceMd

            Rectangle {
                id: resultPanel
                objectName: "appearanceResultPanel"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.minimumWidth: 240
                color: Theme.snapshot.colorSurface
                border.color: Theme.snapshot.colorBorder
                border.width: Theme.snapshot.borderWidth
                radius: Theme.snapshot.radiusMd

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: Theme.snapshot.spaceSm
                    spacing: Theme.snapshot.spaceSm
                    RowLayout {
                        Layout.fillWidth: true
                        ThemeControls.ThemeLabel {
                            Layout.fillWidth: true
                            text: workspace.previewMode === "scene" ? "三维取景" : "渲染结果"
                            heading: true
                            elide: Text.ElideRight
                        }
                        ThemeControls.ThemeButton {
                            objectName: "appearanceSceneTab"
                            text: "3D"
                            checkable: true
                            checked: workspace.previewMode === "scene"
                            onClicked: workspace.previewMode = "scene"
                            Accessible.name: "显示几何相机预览"
                        }
                        ThemeControls.ThemeButton {
                            objectName: "appearanceRenderTab"
                            text: "结果"
                            checkable: true
                            checked: workspace.previewMode === "render"
                            enabled: controller.hasPreview
                            onClicked: workspace.previewMode = "render"
                            Accessible.name: "显示已完成渲染"
                        }
                    }
                    ThemeControls.ThemeLabel {
                        objectName: "appearancePreviewSummary"
                        text: workspace.previewMode === "scene"
                              ? "仅几何预览 · 比例 " + Number(controller.previewScene.aspect_ratio).toFixed(3)
                              : controller.previewSummary
                        muted: true
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        ToolTip.visible: summaryHover.hovered
                        ToolTip.text: text
                        HoverHandler { id: summaryHover }
                    }
                    ThemeControls.ThemeLabel {
                        objectName: "appearanceCircleBoundaryWarning"
                        Layout.fillWidth: true
                        visible: controller.previewScene.substrate.outline_kind === "circle"
                        text: "圆形边界膜厚按捕获节点均值及局部最近节点补值，是预览近似，并非边界实测值。"
                        color: Theme.snapshot.colorWarning
                        wrapMode: Text.WordWrap
                        font.pointSize: Theme.snapshot.fontSizeSmall
                    }
                    ThemeControls.ThemeLabel {
                        objectName: "appearancePreviewGpuWarning"
                        Layout.fillWidth: true
                        visible: workspace.previewMode === "render" && controller.hasPreview
                                 && controller.previewBackend === "gpu"
                        text: "此图由 NVIDIA CUDA GPU 渲染，结果未经精度与一致性验证，仅供预览。"
                        color: Theme.snapshot.colorWarning
                        wrapMode: Text.WordWrap
                        font.pointSize: Theme.snapshot.fontSizeSmall
                    }
                    ColumnLayout {
                        objectName: "appearanceProgressPanel"
                        Layout.fillWidth: true
                        visible: controller.running
                        spacing: Theme.snapshot.spaceXs
                        RowLayout {
                            Layout.fillWidth: true
                            ThemeControls.ThemeLabel {
                                objectName: "appearanceProgressStage"
                                Layout.fillWidth: true
                                text: "阶段：" + workspace.stageText(controller.stage) + " · " + controller.status
                                elide: Text.ElideRight
                                ToolTip.visible: stageHover.hovered
                                ToolTip.text: text
                                HoverHandler { id: stageHover }
                            }
                            ThemeControls.ThemeLabel {
                                objectName: "appearanceProgressEta"
                                text: controller.phase === "cancelling"
                                      ? "等待当前波长帧取消"
                                      : workspace.remainingTime(controller.etaSeconds)
                                muted: true
                            }
                        }
                        ProgressBar {
                            objectName: "appearanceProgressBar"
                            Layout.fillWidth: true
                            from: 0
                            to: 1
                            value: Math.max(0, Math.min(1, controller.progressFraction))
                            indeterminate: controller.progressFraction < 0
                            Accessible.name: controller.progressFraction < 0
                                             ? "渲染进度待更新"
                                             : "渲染进度 " + Math.round(value * 100) + "%"
                        }
                    }
                    Item {
                        objectName: "appearanceViewport"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        Image {
                            objectName: "appearancePreviewImage"
                            anchors.fill: parent
                            visible: workspace.previewMode === "render" && controller.hasPreview
                            source: controller.previewUrl
                            fillMode: Image.PreserveAspectFit
                            asynchronous: true
                            cache: false
                        }
                        AppearanceScenePreview {
                            objectName: "appearanceScenePreview"
                            anchors.fill: parent
                            visible: workspace.previewMode === "scene"
                            controller: workspace.controller
                        }
                        ThemeControls.ThemeLabel {
                            anchors.centerIn: parent
                            visible: workspace.previewMode === "render" && !controller.hasPreview
                            text: "完成渲染后，光谱积分的显示图会出现在这里。"
                            muted: true
                            wrapMode: Text.WordWrap
                            horizontalAlignment: Text.AlignHCenter
                            width: Math.min(parent.width - Theme.snapshot.spaceLg * 2, 360)
                        }
                        Rectangle {
                            objectName: "appearanceStaleBadge"
                            anchors.top: parent.top
                            anchors.right: parent.right
                            anchors.margins: Theme.snapshot.spaceSm
                            visible: workspace.previewMode === "render" && controller.hasPreview && controller.previewStale
                            width: staleLabel.implicitWidth + Theme.snapshot.spaceSm * 2
                            height: staleLabel.implicitHeight + Theme.snapshot.spaceXs * 2
                            radius: Theme.snapshot.radiusSm
                            color: Theme.snapshot.colorSurface
                            border.color: Theme.snapshot.colorWarning
                            ThemeControls.ThemeLabel {
                                id: staleLabel
                                anchors.centerIn: parent
                                text: "参数已更改 · 结果待更新"
                                color: Theme.snapshot.colorWarning
                                font.weight: Theme.snapshot.fontWeightEmphasis
                            }
                        }
                    }
                }
            }

            Rectangle {
                id: parametersRail
                objectName: "appearanceParametersRail"
                Layout.fillHeight: true
                Layout.preferredWidth: workspace.parametersExpanded
                                       ? Math.max(300, Math.min(370, resultArea.width * 0.34)) : 44
                Layout.minimumWidth: workspace.parametersExpanded ? 300 : 44
                Layout.maximumWidth: workspace.parametersExpanded ? 370 : 44
                radius: Theme.snapshot.radiusMd
                color: Theme.snapshot.colorSurface
                border.color: Theme.snapshot.colorBorder

                ThemeControls.ThemeDoubleChevron {
                    objectName: "appearanceParametersExpandButton"
                    anchors.top: parent.top
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.topMargin: Theme.snapshot.spaceSm
                    width: 28
                    visible: !workspace.parametersExpanded
                    text: "‹"
                    direction: "left"
                    Accessible.name: "展开外观参数栏"
                    ToolTip.visible: hovered
                    ToolTip.text: "展开外观参数栏（F4）"
                    onClicked: workspace.toggleParameters()
                }

                ColumnLayout {
                    id: parametersSidebar
                    objectName: "appearanceParametersSidebar"
                    anchors.fill: parent
                    anchors.margins: Theme.snapshot.spaceSm
                    visible: workspace.parametersExpanded
                    spacing: Theme.snapshot.spaceSm
                    RowLayout {
                        Layout.fillWidth: true
                        ThemeControls.ThemeLabel {
                            text: "外观设置"
                            heading: true
                        }
                        ThemeControls.ThemeDoubleChevron {
                            objectName: "appearanceParametersCollapseButton"
                            text: "收起"
                            direction: "right"
                            preserveTextMetrics: true
                            Accessible.name: "收起外观参数栏"
                            ToolTip.visible: hovered
                            ToolTip.text: "收起外观参数栏（F4）"
                            onClicked: workspace.toggleParameters()
                        }
                        Item { Layout.fillWidth: true }
                    }
                    ThemeControls.ThemeLabel {
                        objectName: "appearanceValidationMessage"
                        text: controller.validationMessage
                        color: controller.errorText.length > 0 ? Theme.snapshot.colorError
                             : controller.canStart || controller.running
                               ? Theme.snapshot.colorTextMuted : Theme.snapshot.colorWarning
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        maximumLineCount: 3
                        elide: Text.ElideRight
                        font.pointSize: Theme.snapshot.fontSizeSmall
                        Accessible.description: text
                        ToolTip.visible: validationHover.hovered
                        ToolTip.text: text
                        HoverHandler { id: validationHover }
                    }
                    GridLayout {
                        id: appearanceCategoryNavigation
                        objectName: "appearanceCategoryNavigation"
                        Layout.fillWidth: true
                        columns: 2
                        rowSpacing: Theme.snapshot.spaceXs
                        columnSpacing: Theme.snapshot.spaceXs
                        ThemeControls.ThemeButton {
                            objectName: "appearanceCategory_asset"
                            Layout.fillWidth: true
                            text: "资产"
                            checkable: true
                            checked: workspace.selectedCategory === "asset"
                            Accessible.name: "资产参数类别"
                            onClicked: workspace.categorySelectionRequested("asset")
                        }
                        ThemeControls.ThemeButton {
                            objectName: "appearanceCategory_light"
                            Layout.fillWidth: true
                            text: "光源"
                            checkable: true
                            checked: workspace.selectedCategory === "light"
                            Accessible.name: "光源参数类别"
                            onClicked: workspace.categorySelectionRequested("light")
                        }
                        ThemeControls.ThemeButton {
                            objectName: "appearanceCategory_camera"
                            Layout.fillWidth: true
                            text: "相机"
                            checkable: true
                            checked: workspace.selectedCategory === "camera"
                            Accessible.name: "相机参数类别"
                            onClicked: workspace.categorySelectionRequested("camera")
                        }
                        ThemeControls.ThemeButton {
                            objectName: "appearanceCategory_compute"
                            Layout.fillWidth: true
                            text: "计算"
                            checkable: true
                            checked: workspace.selectedCategory === "compute"
                            Accessible.name: "计算参数类别"
                            onClicked: workspace.categorySelectionRequested("compute")
                        }
                    }
                    StackLayout {
                        id: appearanceCategoryStack
                        objectName: "appearanceCategoryStack"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.minimumHeight: 0
                        currentIndex: workspace.selectedCategory === "asset" ? 0
                                      : workspace.selectedCategory === "light" ? 1
                                      : workspace.selectedCategory === "camera" ? 2 : 3
                    ScrollView {
                        id: appearanceScrollAsset
                        objectName: "appearanceScroll_asset"
                        clip: true
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        contentWidth: availableWidth
                        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                        ColumnLayout {
                            width: appearanceScrollAsset.availableWidth
                            spacing: Theme.snapshot.spaceSm
                        ThemeControls.ThemeSection {
                                                Layout.fillWidth: true
                                                objectName: "appearanceGeometrySection"
                                                title: "资产 · 基片与厚度来源"
                                                expanded: true
                                                ColumnLayout {
                                                    Layout.fillWidth: true
                                                    spacing: Theme.snapshot.spaceXs
                                                    Label {
                                                        text: controller.appearanceThicknessLabel
                                                        color: Theme.snapshot.colorText
                                                        font.weight: Theme.snapshot.fontWeightEmphasis
                                                    }
                                                    Label {
                                                        text: "膜层厚度取自所选沉积资产的逐步捕获数据，按冻结膜系的膜层标识映射。"
                                                        color: Theme.snapshot.colorTextMuted
                                                        wrapMode: Text.WordWrap
                                                        Layout.fillWidth: true
                                                    }
                                                    NumberField {
                                                        host: controller
                                                        label: "实体厚度 mm"
                                                        fieldKey: "geometry_thickness_mm"
                                                    }
                                                    ThemeControls.ThemeButton {
                                                        objectName: "appearanceInheritedThicknessButton"
                                                        text: "使用膜系基片厚度"
                                                        enabled: controller.canResetGeometryThicknessToInherited
                                                        onClicked: controller.resetGeometryThicknessToInherited()
                                                    }
                                                    Label {
                                                        text: controller.geometryThicknessSourceText
                                                        color: controller.geometryThicknessMode === "missing"
                                                              ? Theme.snapshot.colorWarning : Theme.snapshot.colorTextMuted
                                                        wrapMode: Text.WordWrap
                                                        Layout.fillWidth: true
                                                        font.pointSize: Theme.snapshot.fontSizeSmall
                                                    }
                                                }
                                            }
                        }
                    }
                    ScrollView {
                        id: appearanceScrollLight
                        objectName: "appearanceScroll_light"
                        clip: true
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        contentWidth: availableWidth
                        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                        ColumnLayout {
                            width: appearanceScrollLight.availableWidth
                            spacing: Theme.snapshot.spaceSm
                        ThemeControls.ThemeSection {
                                                Layout.fillWidth: true
                                                objectName: "appearanceAreaLightSection"
                                                title: "光源 · 矩形面光源"
                                                expanded: true
                                                ColumnLayout {
                                                    Layout.fillWidth: true
                                                    spacing: Theme.snapshot.spaceXs
                                                    CheckBox {
                                                        objectName: "appearanceAreaEnabled"
                                                        text: "启用面光源"
                                                        checked: controller.draft.area_enabled
                                                        onToggled: controller.setFlag("area_enabled", checked)
                                                    }
                                                    VectorFields { host: controller; label: "位置"; prefix: "area_" }
                                                    VectorFields { host: controller; label: "瞄准点"; prefix: "area_target_" }
                                                    VectorFields { host: controller; label: "上方向"; prefix: "area_up_" }
                                                    NumberField { host: controller; label: "宽 mm"; fieldKey: "area_size_x" }
                                                    NumberField { host: controller; label: "高 mm"; fieldKey: "area_size_y" }
                                                    ColumnLayout {
                                                        Layout.fillWidth: true
                                                        ThemeControls.ThemeButton {
                                                            objectName: "appearanceLoadAreaSpectrumButton"
                                                            text: "导入面光源 SPD"
                                                            onClicked: { workspace.spectrumTarget = "area"; spectrumDialog.open() }
                                                        }
                                                        Label {
                                                            text: controller.areaSpectrumSummary
                                                            color: Theme.snapshot.colorTextMuted
                                                            wrapMode: Text.WordWrap
                                                            Layout.fillWidth: true
                                                        }
                                                    }
                                                    ColumnLayout {
                                                        Layout.fillWidth: true
                                                        ThemeControls.ThemeComboBox {
                                                            accessibleLabel: "面光源光谱单位"
                                                            model: ["相对量", "绝对量"]
                                                            currentIndex: controller.draft.area_kind === "absolute" ? 1 : 0
                                                            Layout.fillWidth: true
                                                            onActivated: controller.setSpectrumKind("area", index === 0 ? "relative" : "absolute")
                                                        }
                                                        NumberField { host: controller; label: "强度尺度"; fieldKey: "area_scale" }
                                                    }
                                                    CheckBox {
                                                        text: "已按绝对辐射量校准"
                                                        checked: controller.draft.area_absolute_calibration
                                                        enabled: controller.draft.area_kind === "absolute"
                                                        onToggled: controller.setFlag("area_absolute_calibration", checked)
                                                    }
                                                }
                                            }
                        ThemeControls.ThemeSection {
                                                Layout.fillWidth: true
                                                objectName: "appearanceEnvironmentSection"
                                                title: "光源 · 均匀环境光谱"
                                                expanded: false
                                                ColumnLayout {
                                                    Layout.fillWidth: true
                                                    spacing: Theme.snapshot.spaceXs
                                                    CheckBox {
                                                        objectName: "appearanceEnvironmentEnabled"
                                                        text: "启用均匀环境"
                                                        checked: controller.draft.environment_enabled
                                                        onToggled: controller.setFlag("environment_enabled", checked)
                                                    }
                                                    ColumnLayout {
                                                        Layout.fillWidth: true
                                                        ThemeControls.ThemeButton {
                                                            objectName: "appearanceLoadEnvironmentSpectrumButton"
                                                            text: "导入环境光谱"
                                                            onClicked: { workspace.spectrumTarget = "environment"; spectrumDialog.open() }
                                                        }
                                                        Label {
                                                            text: controller.environmentSpectrumSummary
                                                            color: Theme.snapshot.colorTextMuted
                                                            wrapMode: Text.WordWrap
                                                            Layout.fillWidth: true
                                                        }
                                                    }
                                                    ColumnLayout {
                                                        Layout.fillWidth: true
                                                        ThemeControls.ThemeComboBox {
                                                            accessibleLabel: "环境光谱单位"
                                                            model: ["相对量", "绝对量"]
                                                            currentIndex: controller.draft.environment_kind === "absolute" ? 1 : 0
                                                            Layout.fillWidth: true
                                                            onActivated: controller.setSpectrumKind("environment", index === 0 ? "relative" : "absolute")
                                                        }
                                                        NumberField { host: controller; label: "强度尺度"; fieldKey: "environment_scale" }
                                                    }
                                                    CheckBox {
                                                        text: "已按绝对辐射量校准"
                                                        checked: controller.draft.environment_absolute_calibration
                                                        enabled: controller.draft.environment_kind === "absolute"
                                                        onToggled: controller.setFlag("environment_absolute_calibration", checked)
                                                    }
                                                }
                                            }
                        }
                    }
                    ScrollView {
                        id: appearanceScrollCamera
                        objectName: "appearanceScroll_camera"
                        clip: true
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        contentWidth: availableWidth
                        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                        ColumnLayout {
                            width: appearanceScrollCamera.availableWidth
                            spacing: Theme.snapshot.spaceSm
                        ThemeControls.ThemeSection {
                                                Layout.fillWidth: true
                                                objectName: "appearanceCameraSection"
                                                title: "相机 · 局部坐标 mm"
                                                ColumnLayout {
                                                    Layout.fillWidth: true
                                                    spacing: Theme.snapshot.spaceXs
                                                    VectorFields { host: controller; label: "位置"; prefix: "camera_" }
                                                    VectorFields { host: controller; label: "目标"; prefix: "target_" }
                                                    VectorFields { host: controller; label: "上方向"; prefix: "up_" }
                                                    NumberField { host: controller; label: "垂直视场角 °"; fieldKey: "fov_y_deg" }
                                                }
                                            }
                        }
                    }
                    ScrollView {
                        id: appearanceScrollCompute
                        objectName: "appearanceScroll_compute"
                        clip: true
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        contentWidth: availableWidth
                        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                        ColumnLayout {
                            width: appearanceScrollCompute.availableWidth
                            spacing: Theme.snapshot.spaceSm
                        ThemeControls.ThemeSection {
                                                Layout.fillWidth: true
                                                objectName: "appearanceRenderSection"
                                                title: "计算 · 渲染参数"
                                                ColumnLayout {
                                                    Layout.fillWidth: true
                                                    spacing: Theme.snapshot.spaceXs
                                                    ThemeControls.ThemeLabel {
                                                        text: "计算设备"
                                                        muted: true
                                                    }
                                                    ThemeControls.ThemeComboBox {
                                                        objectName: "appearanceBackendSelector"
                                                        Layout.fillWidth: true
                                                        visible: controller.computeTarget.hostId === "local"
                                                        accessibleLabel: "外观渲染计算设备"
                                                        model: ["CPU · 本地标量", "GPU · NVIDIA CUDA"]
                                                        currentIndex: controller.backend === "gpu" ? 1 : 0
                                                        enabled: !controller.running
                                                                 && controller.computeTarget.hostId === "local"
                                                        onActivated: controller.setBackend(index === 1 ? "gpu" : "cpu")
                                                    }
                                                    ThemeControls.ThemeLabel {
                                                        objectName: "appearanceRemoteDeviceSummary"
                                                        Layout.fillWidth: true
                                                        visible: controller.computeTarget.hostId !== "local"
                                                        text: "远端计算设备由上方的主机与设备选择器决定；仅可提交服务端当前实测可用的设备。"
                                                        wrapMode: Text.WordWrap
                                                        muted: true
                                                    }
                                                    ThemeControls.ThemeLabel {
                                                        objectName: "appearanceGpuCapabilityMessage"
                                                        Layout.fillWidth: true
                                                        visible: controller.computeTarget.hostId === "local" && controller.backend === "gpu"
                                                        text: "仅支持 NVIDIA CUDA。" + controller.gpuCapabilityMessage
                                                        color: controller.gpuAvailable
                                                               ? Theme.snapshot.colorTextMuted
                                                               : Theme.snapshot.colorWarning
                                                        wrapMode: Text.WordWrap
                                                        font.pointSize: Theme.snapshot.fontSizeSmall
                                                    }
                                                    ThemeControls.ThemeLabel {
                                                        objectName: "appearanceGpuPrecisionLabel"
                                                        text: "GPU 计算精度"
                                                        muted: true
                                                        visible: controller.backend === "gpu"
                                                    }
                                                    ThemeControls.ThemeComboBox {
                                                        objectName: "appearanceGpuPrecisionSelector"
                                                        Layout.fillWidth: true
                                                        visible: controller.backend === "gpu"
                                                        enabled: !controller.running
                                                                 && (controller.computeTarget.hostId !== "local"
                                                                     ? controller.computeTarget.canSubmit
                                                                     : controller.gpuAvailable)
                                                        accessibleLabel: "GPU 计算精度"
                                                        model: ["float32", "mixed"]
                                                        currentIndex: controller.precision === "mixed" ? 1 : 0
                                                        onActivated: controller.setPrecision(index === 1 ? "mixed" : "float32")
                                                    }
                                                    ThemeControls.ThemeLabel {
                                                        objectName: "appearanceGpuQualificationWarning"
                                                        Layout.fillWidth: true
                                                        visible: controller.backend === "gpu"
                                                        text: "GPU 结果尚未通过精度与一致性验证，仅供预览；请勿用于研究结论或验收。"
                                                        color: Theme.snapshot.colorWarning
                                                        wrapMode: Text.WordWrap
                                                        font.pointSize: Theme.snapshot.fontSizeSmall
                                                    }
                                                    NumberField { host: controller; label: "宽 px"; fieldKey: "width" }
                                                    NumberField { host: controller; label: "高 px"; fieldKey: "height" }
                                                    NumberField { host: controller; label: "每像素采样"; fieldKey: "spp" }
                                                    NumberField { host: controller; label: "随机种子"; fieldKey: "seed" }
                                                    NumberField { host: controller; label: "起始 nm"; fieldKey: "wavelength_start_nm" }
                                                    NumberField { host: controller; label: "终止 nm"; fieldKey: "wavelength_end_nm" }
                                                    NumberField { host: controller; label: "步长 nm"; fieldKey: "wavelength_step_nm" }
                                                }
                                            }
                        }
                    }
                    }
                }
            }
        }
    }
}
