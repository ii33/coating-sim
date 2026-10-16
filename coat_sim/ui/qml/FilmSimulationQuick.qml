import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "."
import "theme_controls" as ThemeControls

AppWindow {
    id: window
    // FilmSimulation is injected by the application as a Python context property.
    readonly property var fileDialogController: FilmSimulation // qmllint disable unqualified
    property bool projectCloseAuthorized: false
    property var projectNavigation: null
    property var projectResources: null
    title: "光谱仿真"
    caption: title + (FilmSimulation.hasDesign && FilmSimulation.designName.length > 0
                      ? " — " + FilmSimulation.designName : "")
    statusText: FilmSimulation.status
    statusSeverity: FilmSimulation.busy ? "busy"
                    : FilmSimulation.validationError.length > 0 ? "error"
                    : FilmSimulation.stale && FilmSimulation.hasResult ? "warning" : "info"
    statusDetail: FilmSimulation.status
    property bool compactActions: width < 1000
    property bool statisticsExpanded: true
    property bool diagnosticsExpanded: false
    property string activeSidebarSection: "statistics"
    readonly property bool sidebarExpanded: statisticsExpanded || diagnosticsExpanded
    property string pendingConfirmationKind: ""
    property string pendingConfirmationMessage: ""
    property bool closeAuthorized: false
    color: window.themeColors.colorWindow
    property int themeRevision: Theme.revision

    readonly property real diagnosticsPanelWidth:
        Math.min(520, Math.max(window.width < 900 ? 280 : 384,
                               Math.round(resultHost.width * 0.42)))

    onDiagnosticsExpandedChanged: {
        if (diagnosticsExpanded) {
            activeSidebarSection = "diagnostics"
            if (statisticsExpanded)
                statisticsExpanded = false
        }
    }
    onStatisticsExpandedChanged: {
        if (statisticsExpanded) {
            activeSidebarSection = "statistics"
            if (diagnosticsExpanded)
                diagnosticsExpanded = false
        }
    }

    function recalculateFromShortcut() {
        calculateButton.forceActiveFocus()
        Qt.callLater(function() {
            FilmSimulation.requestCalculation()
        })
    }
    function optimizeFromShortcut() {
        if (window.projectNavigation) return
        optimizeButton.forceActiveFocus()
        Qt.callLater(function() { FilmSimulation.openOptimizationAndStart() })
    }
    function toggleStatistics() {
        if (activeSidebarSection === "diagnostics") {
            if (diagnosticsExpanded)
                calculateButton.forceActiveFocus()
            diagnosticsExpanded = !diagnosticsExpanded
            return
        }
        if (statisticsExpanded)
            calculateButton.forceActiveFocus()
        statisticsExpanded = !statisticsExpanded
    }
    function toggleDiagnostics() {
        openSidebarSection("diagnostics")
    }
    function openSidebarSection(section) {
        activeSidebarSection = section
        if (section === "statistics") {
            diagnosticsExpanded = false
            statisticsExpanded = true
        } else {
            statisticsExpanded = false
            diagnosticsExpanded = true
        }
    }
    function toggleSidebar() {
        if (activeSidebarSection === "diagnostics") {
            if (diagnosticsExpanded)
                calculateButton.forceActiveFocus()
            diagnosticsExpanded = !diagnosticsExpanded
        } else {
            if (statisticsExpanded)
                calculateButton.forceActiveFocus()
            statisticsExpanded = !statisticsExpanded
        }
    }

    onClosing: function(close) {
        if (window.closeAuthorized || window.projectCloseAuthorized) {
            window.closeAuthorized = false
            FilmSimulation.close()
            return
        }
        if (!FilmSimulation.requestClose()) {
            close.accepted = false
            return
        }
        FilmSimulation.close()
    }

    Shortcut {
        sequence: "F5"
        context: Qt.WindowShortcut
        enabled: FilmSimulation.hasDesign
        onActivated: window.recalculateFromShortcut()
    }
    Shortcut {
        sequence: "F6"
        context: Qt.WindowShortcut
        enabled: FilmSimulation.hasDesign
        onActivated: window.optimizeFromShortcut()
    }
    Shortcut {
        sequence: "F4"
        context: Qt.WindowShortcut
        onActivated: window.toggleStatistics()
    }
    Shortcut {
        sequence: "Escape"
        context: Qt.WindowShortcut
        enabled: window.sidebarExpanded
        onActivated: window.toggleSidebar()
    }

    Connections {
        target: FilmSimulation
        ignoreUnknownSignals: true
        function onConfirmationRequested(kind, message) {
            window.pendingConfirmationKind = String(kind || "")
            window.pendingConfirmationMessage = String(message || "")
            replacementConfirmation.open()
        }
        function onCloseConfirmed() {
            window.closeAuthorized = true
            Qt.callLater(window.close)
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: window.themeColors.spaceMd
        anchors.rightMargin: window.themeColors.spaceMd
        anchors.topMargin: window.contentTopMargin
        anchors.bottomMargin: window.themeColors.spaceMd
        spacing: window.themeColors.spaceMd

        Rectangle {
            id: simulationToolbar
            objectName: "filmSimulationToolbar"
            Layout.fillWidth: true
            Layout.preferredHeight: Math.max(58, actionBar.implicitHeight + window.themeColors.spaceSm * 2)
            radius: window.themeColors.radiusMd
            color: window.themeColors.colorSurface
            border.color: window.themeColors.colorBorder
            ResponsiveOperationBar {
                id: actionBar
                objectName: "filmSimulationActionBar"
                Accessible.name: "操作栏"
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: window.themeColors.spaceSm
                spacing: window.themeColors.spaceXs
                ProjectModuleSelector {
                    id: simulationProjectModuleSelector
                    navigation: window.projectNavigation
                    currentModuleKey: "film_simulation"
                    selectorObjectName: "filmSimulationProjectModuleSelector"
                    projectNameObjectName: "filmSimulationProjectName"
                    selectorAccessibleName: "当前项目模块"
                }
                ProjectResourceControls {
                    projectResources: window.projectResources
                    width: visible ? Math.min(520, actionBar.width) : 0
                    beforeDocumentAction: function(action, documentId) {
                        return !window.fileDialogController.busy
                    }
                    extraTransferActions: [
                        {key: "spectrum_csv", text: "结果：导出光谱 CSV…", enabled: window.fileDialogController.canExport},
                        {key: "spectrum_png", text: "结果：导出光谱图 PNG…", enabled: window.fileDialogController.canExport}
                    ]
                    onExtraTransferActionRequested: function(key) {
                        if (key === "spectrum_csv") csvDialog.open()
                        else if (key === "spectrum_png") pngDialog.open()
                    }
                }
                ThemeControls.ThemeButton {
                    visible: !window.projectResources
                    objectName: "filmSimulationLoadButton"
                    text: window.projectNavigation ? "项目膜系" : "加载膜系"
                    Accessible.name: text
                    width: Math.max(58, implicitWidth)
                    height: window.themeColors.controlHeightLg
                    leftPadding: window.compactActions ? window.themeColors.spaceSm : window.themeColors.spaceMd
                    rightPadding: window.compactActions ? window.themeColors.spaceSm : window.themeColors.spaceMd
                    ToolTip.visible: hovered && window.compactActions
                    ToolTip.text: "加载膜系"
                    onClicked: {
                        if (window.projectNavigation) window.projectNavigation.openProjectModule("film_design")
                        else designDialog.open()
                    }
                }
                ThemeControls.ThemeButton {
                    visible: !window.projectResources
                    objectName: "filmSimulationSaveButton"
                    text: window.compactActions ? "保存" : "保存膜系"
                    Accessible.name: "保存当前膜系"
                    width: Math.max(50, implicitWidth)
                    height: window.themeColors.controlHeightLg
                    leftPadding: window.compactActions ? window.themeColors.spaceSm : window.themeColors.spaceMd
                    rightPadding: window.compactActions ? window.themeColors.spaceSm : window.themeColors.spaceMd
                    ToolTip.visible: hovered && window.compactActions
                    ToolTip.text: "保存当前膜系和仿真设置"
                    enabled: FilmSimulation.hasDesign
                    onClicked: {
                        if (window.projectNavigation) {
                            window.projectNavigation.saveCurrent()
                            return
                        }
                        if (!FilmSimulation.saveCurrent()
                                && FilmSimulation.designPath.length === 0)
                            saveDesignDialog.open()
                    }
                }
                ThemeControls.ThemeButton {
                    id: parametersButton
                    objectName: "filmSimulationParametersButton"
                    text: "仿真参数"
                    Accessible.name: "打开仿真参数"
                    width: Math.max(58, implicitWidth)
                    height: window.themeColors.controlHeightLg
                    leftPadding: window.compactActions ? window.themeColors.spaceSm : window.themeColors.spaceMd
                    rightPadding: window.compactActions ? window.themeColors.spaceSm : window.themeColors.spaceMd
                    ToolTip.visible: hovered && window.compactActions
                    ToolTip.text: "仿真参数"
                    onClicked: parametersDialog.open()
                }
                ThemeControls.ThemeButton {
                    id: exportButton
                    visible: !window.projectResources
                    objectName: "filmSimulationExportButton"
                    text: "导出"
                    Accessible.name: "导出光谱数据或图表"
                    width: Math.max(58, implicitWidth)
                    height: window.themeColors.controlHeightLg
                    leftPadding: window.compactActions ? window.themeColors.spaceSm : window.themeColors.spaceMd
                    rightPadding: window.compactActions ? window.themeColors.spaceSm : window.themeColors.spaceMd
                    ToolTip.visible: hovered && window.compactActions
                    ToolTip.text: "导出 CSV 或 PNG"
                    enabled: FilmSimulation.canExport
                    onClicked: exportMenu.open()
                }
                ThemeControls.ThemeButton {
                    id: optimizeButton
                    visible: !window.projectNavigation
                    objectName: "filmSimulationOptimizeButton"
                    text: window.compactActions ? "优化" : "厚度优化"
                    Accessible.name: "从当前仿真设计打开厚度优化"
                    width: Math.max(58, implicitWidth)
                    height: window.themeColors.controlHeightLg
                    leftPadding: window.compactActions ? window.themeColors.spaceSm : window.themeColors.spaceMd
                    rightPadding: window.compactActions ? window.themeColors.spaceSm : window.themeColors.spaceMd
                    ToolTip.visible: hovered && window.compactActions
                    ToolTip.text: "厚度优化"
                    enabled: FilmSimulation.hasDesign && !FilmSimulation.busy
                    onClicked: FilmSimulation.openOptimization()
                }
                ThemeControls.ThemeButton {
                    id: calculateButton
                    objectName: "filmSimulationCalculateButton"
                    text: window.compactActions ? "计算" : "计算并绘图"
                    Accessible.name: "计算并绘图"
                    width: Math.max(58, implicitWidth)
                    height: window.themeColors.controlHeightLg
                    leftPadding: window.compactActions ? window.themeColors.spaceSm : window.themeColors.spaceMd
                    rightPadding: window.compactActions ? window.themeColors.spaceSm : window.themeColors.spaceMd
                    ToolTip.visible: hovered && window.compactActions
                    ToolTip.text: "计算并绘图"
                    enabled: FilmSimulation.hasDesign && !FilmSimulation.busy
                    primary: true
                    onClicked: FilmSimulation.requestCalculation()
                }
            }
        }

        Item {
            id: resultHost
            objectName: "filmSimulationResultHost"
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: Math.max(80, Math.min(240,
                window.height - window.contentTopMargin - simulationToolbar.height
                - window.themeColors.spaceMd * 2))

            RowLayout {
                id: resultArea
                anchors.fill: parent
                spacing: window.themeColors.spaceMd

                FilmSpectrumPlot {
                    id: spectrumPlot
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumWidth: window.width < 900 ? 220 : 360
                    Layout.minimumHeight: Math.min(240, resultHost.height)
                    controller: FilmSimulation
                    themeColors: window.themeColors
                    floatingControlsRightEdge: spectrumPlot.width
                }

                RowLayout {
                    id: sidebarSlot
                    objectName: "filmSimulationSidebarSlot"
                    Layout.fillHeight: true
                    Layout.preferredWidth: 64 + (window.sidebarExpanded
                                               ? window.diagnosticsPanelWidth : 0)
                    Layout.minimumWidth: 64 + (window.sidebarExpanded
                                              ? window.diagnosticsPanelWidth : 0)
                    Layout.maximumWidth: 64 + (window.sidebarExpanded
                                              ? window.diagnosticsPanelWidth : 0)
                    spacing: 0

                    Rectangle {
                        objectName: "filmSimulationSidebarRail"
                        Layout.preferredWidth: 64
                        Layout.minimumWidth: 64
                        Layout.maximumWidth: 64
                        Layout.fillHeight: true
                        radius: window.themeColors.radiusMd
                        color: window.themeColors.colorSurface
                        border.color: window.themeColors.colorBorder

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: window.themeColors.spaceXs
                            spacing: window.themeColors.spaceXs
                            ThemeControls.ThemeButton {
                                id: statisticsToggle
                                objectName: "filmSimulationStatisticsToggle"
                                Layout.fillWidth: true
                                Layout.preferredHeight: 60
                                text: "光谱\n统计"
                                Accessible.name: "显示光谱统计"
                                checkable: true
                                checked: window.activeSidebarSection === "statistics"
                                         && window.statisticsExpanded
                                ToolTip.visible: hovered
                                ToolTip.text: "光谱统计"
                                contentItem: Label {
                                    text: "光谱\n统计"
                                    wrapMode: Text.WordWrap
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                    color: statisticsToggle.checked ? window.themeColors.colorTextOnAccent
                                                                    : window.themeColors.colorText
                                    font.pointSize: window.themeColors.fontSizeBody
                                }
                                background: Rectangle {
                                    color: statisticsToggle.checked ? window.themeColors.colorAccent : "transparent"
                                    radius: window.themeColors.radiusSm
                                }
                                onClicked: window.openSidebarSection("statistics")
                            }
                            ThemeControls.ThemeButton {
                                id: diagnosticsButton
                                objectName: "filmSimulationDiagnosticsButton"
                                Layout.fillWidth: true
                                Layout.preferredHeight: 60
                                text: "数值\n诊断"
                                Accessible.name: "显示数值诊断"
                                checkable: true
                                checked: window.activeSidebarSection === "diagnostics"
                                         && window.diagnosticsExpanded
                                ToolTip.visible: hovered
                                ToolTip.text: "数值诊断"
                                contentItem: Label {
                                    text: "数值\n诊断"
                                    wrapMode: Text.WordWrap
                                    horizontalAlignment: Text.AlignHCenter
                                    verticalAlignment: Text.AlignVCenter
                                    color: diagnosticsButton.checked ? window.themeColors.colorTextOnAccent
                                                                     : window.themeColors.colorText
                                    font.pointSize: window.themeColors.fontSizeBody
                                }
                                background: Rectangle {
                                    color: diagnosticsButton.checked ? window.themeColors.colorAccent : "transparent"
                                    radius: window.themeColors.radiusSm
                                }
                                onClicked: window.openSidebarSection("diagnostics")
                            }
                            ThemeControls.ThemeDoubleChevron {
                                objectName: "filmSimulationDiagnosticsToggle"
                                Layout.alignment: Qt.AlignHCenter
                                Layout.preferredWidth: 28
                                Layout.minimumWidth: 28
                                Layout.maximumWidth: 28
                                Layout.preferredHeight: 42
                                text: window.sidebarExpanded ? "折叠当前侧栏" : "展开当前侧栏"
                                direction: window.sidebarExpanded ? "right" : "left"
                                Accessible.name: window.sidebarExpanded
                                                ? "折叠当前侧栏" : "展开当前侧栏"
                                ToolTip.visible: hovered
                                ToolTip.text: Accessible.name
                                onClicked: window.toggleSidebar()
                            }
                            Item { Layout.fillHeight: true }
                        }
                    }

                    ThemeControls.ThemeSidebar {
                        id: diagnosticsSidebar
                        objectName: "filmSimulationDiagnosticsSidebar"
                        Layout.fillHeight: true
                        Layout.preferredWidth: window.sidebarExpanded
                                              ? window.diagnosticsPanelWidth : 0
                        Layout.minimumWidth: window.sidebarExpanded
                                             ? window.diagnosticsPanelWidth : 0
                        Layout.maximumWidth: window.sidebarExpanded
                                             ? window.diagnosticsPanelWidth : 0
                        title: window.activeSidebarSection === "statistics"
                               ? "光谱统计" : "数值诊断"
                        sidebarWidth: window.diagnosticsPanelWidth
                        visible: window.sidebarExpanded

                        FilmSimulationCalculationItems {
                            objectName: "filmSimulationCalculationItems"
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            visible: window.statisticsExpanded
                            controller: FilmSimulation
                            expanded: window.statisticsExpanded
                            sidebarManaged: true
                            onToggleRequested: window.toggleStatistics()
                        }

                        TextArea {
                            objectName: "filmSimulationResultSummary"
                            visible: window.diagnosticsExpanded
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            implicitHeight: Math.max(120, contentHeight + topPadding + bottomPadding)
                            readOnly: true
                            selectByMouse: true
                            wrapMode: TextEdit.Wrap
                            text: FilmSimulation.resultSummary
                            color: window.themeColors.colorTextMuted
                            font.family: window.themeColors.fontFamily
                            font.pointSize: window.themeColors.fontSizeSmall
                            background: null
                        }
                    }
                }
            }
        }
    }

    ThemeControls.ThemeDialogBase {
        id: replacementConfirmation
        objectName: "filmSimulationReplacementConfirmation"
        title: window.pendingConfirmationKind === "replace_design"
               ? "确认替换膜系？"
               : window.pendingConfirmationKind === "replace_document"
                 ? "确认切换仿真文档？"
                 : window.pendingConfirmationKind === "discard_changes"
                   ? "确认关闭仿真？"
                   : window.pendingConfirmationKind === "overwrite"
                     ? "确认覆盖膜系文件？" : "确认操作"
        modal: true
        anchors.centerIn: parent
        width: Math.min(620, window.width - window.themeColors.spaceXl * 2)
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Label {
            objectName: "filmSimulationReplacementConfirmationMessage"
            text: window.pendingConfirmationMessage
            color: window.themeColors.colorText
            wrapMode: Text.WordWrap
            padding: window.themeColors.spaceMd
        }
        onAccepted: {
            FilmSimulation.resolvePendingAction(true)
            window.pendingConfirmationKind = ""
            window.pendingConfirmationMessage = ""
        }
        onRejected: {
            FilmSimulation.resolvePendingAction(false)
            window.pendingConfirmationKind = ""
            window.pendingConfirmationMessage = ""
        }
    }

    FileDialog {
        id: designDialog
        objectName: "filmSimulationDesignFileDialog"
        title: "加载膜系"
        fileMode: FileDialog.OpenFile
        defaultSuffix: "cfilm"
        nameFilters: ["膜系设计 (*.cfilm)", "全部文件 (*)"]
        onAccepted: {
            const path = window.fileDialogController.localFilePath(selectedFile)
            if (path)
                window.fileDialogController.loadDesignFromPath(path)
        }
    }
    FileDialog {
        id: saveDesignDialog
        objectName: "filmSimulationSaveDesignFileDialog"
        title: "保存膜系设计"
        fileMode: FileDialog.SaveFile
        defaultSuffix: "cfilm"
        options: FileDialog.DontConfirmOverwrite
        nameFilters: ["膜系设计 (*.cfilm)", "全部文件 (*)"]
        onAccepted: {
            const path = window.fileDialogController.localFilePath(selectedFile)
            if (path)
                window.fileDialogController.saveFilePath(path, false)
        }
    }
    FileDialog {
        id: csvDialog
        objectName: "filmSimulationCsvFileDialog"
        title: "导出光谱数据"
        fileMode: FileDialog.SaveFile
        defaultSuffix: "csv"
        nameFilters: ["CSV (*.csv)"]
        onAccepted: {
            const path = window.fileDialogController.localFilePath(selectedFile)
            if (path)
                window.fileDialogController.exportCsvToPath(path)
        }
    }
    FileDialog {
        id: pngDialog
        objectName: "filmSimulationPngFileDialog"
        title: "导出光谱图"
        fileMode: FileDialog.SaveFile
        defaultSuffix: "png"
        nameFilters: ["PNG (*.png)"]
        onAccepted: {
            const path = window.fileDialogController.localFilePath(selectedFile)
            if (path)
                window.fileDialogController.exportPngToPath(path)
        }
    }

    Menu {
        id: exportMenu
        objectName: "filmSimulationExportMenu"
        MenuItem {
            objectName: "filmSimulationExportCsvAction"
            text: "导出 CSV"
            enabled: exportButton.enabled
            onTriggered: csvDialog.open()
        }
        MenuItem {
            objectName: "filmSimulationExportPngAction"
            text: "导出 PNG"
            enabled: exportButton.enabled
            onTriggered: pngExportSettings.open()
        }
    }

    ThemeControls.ThemeDialogBase {
        id: parametersDialog
        objectName: "filmSimulationParametersDialog"
        title: "仿真参数"
        modal: true
        standardButtons: Dialog.NoButton
        width: Overlay.overlay
               ? Math.max(240, Math.min(940,
                    Overlay.overlay.width - window.themeColors.spaceXl * 2))
               : 940
        height: Overlay.overlay
                ? Math.max(180, Math.min(700,
                    Overlay.overlay.height - window.themeColors.spaceXl * 2))
                : 700
        anchors.centerIn: Overlay.overlay
        property string localError: ""
        function applySettings() {
            var simulationDraft = parametersForm.draftPayload()
            if (!simulationDraft) {
                localError = parametersForm.draftError
                plotParametersForm.localError = localError
                return false
            }
            var accepted = parametersForm.controller.setSimulationAndPlotSettings(
                simulationDraft, plotParametersForm.settingsPayload())
            localError = accepted ? "" : parametersForm.controller.validationError
            plotParametersForm.localError = localError
            return accepted
        }
        onOpened: {
            localError = ""
            settingsTabs.currentIndex = 0
            parametersForm.loadDraft()
            plotParametersForm.loadFromController()
        }
        onRejected: {
            localError = ""
            parametersForm.loadDraft()
            plotParametersForm.loadFromController()
        }
        onClosed: Qt.callLater(function() {
            if (window.visible) parametersButton.forceActiveFocus()
        })

        contentItem: ColumnLayout {
            spacing: window.themeColors.spaceMd
            Accessible.role: Accessible.Dialog
            Accessible.name: parametersDialog.title
            TabBar {
                id: settingsTabs
                objectName: "filmSimulationSettingsTabs"
                Layout.fillWidth: true
                TabButton {
                    objectName: "filmSimulationCalculationTab"
                    text: "计算参数"
                    Accessible.name: text
                }
                TabButton {
                    objectName: "filmSimulationPlotTab"
                    text: "绘图参数"
                    Accessible.name: text
                }
            }
            StackLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                currentIndex: settingsTabs.currentIndex
                ScrollView {
                    objectName: "filmSimulationCalculationPage"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    contentWidth: availableWidth
                    FilmSimulationParameters {
                        id: parametersForm
                        objectName: "filmSimulationParametersForm"
                        width: parent.width
                        controller: FilmSimulation
                    }
                }
                ScrollView {
                    objectName: "filmSimulationPlotPage"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    contentWidth: availableWidth
                    FilmPlotSettingsForm {
                        id: plotParametersForm
                        objectName: "filmPlotSettingsForm"
                        width: parent.width
                        controller: FilmSimulation
                        themeTokens: window.themeColors
                    }
                }
            }
            Label {
                objectName: "filmSimulationCombinedSettingsError"
                Layout.fillWidth: true
                visible: parametersDialog.localError.length > 0
                text: parametersDialog.localError
                color: window.themeColors.colorError
                wrapMode: Text.WordWrap
            }
            RowLayout {
                objectName: "filmSimulationParametersActionRow"
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignRight
                spacing: window.themeColors.spaceSm
                ThemeControls.ThemeButton {
                    objectName: "filmSimulationParametersCancelButton"
                    text: "取消"
                    Accessible.name: "取消仿真参数"
                    onClicked: parametersDialog.reject()
                }
                ThemeControls.ThemeButton {
                    objectName: "filmSimulationParametersApplyButton"
                    text: "应用参数"
                    primary: true
                    onClicked: if (parametersDialog.applySettings()) parametersDialog.close()
                }
            }
        }
    }

    FilmPlotExportSettings {
        id: pngExportSettings
        objectName: "filmSimulationPngExportSettingsDialog"
        controller: FilmSimulation
        onExportRequested: pngDialog.open()
    }

    Connections {
        target: Theme
        function onSnapshotChanged() {
            window.themeRevision = Theme.revision
            FilmSimulation.setPlotTheme(Theme.mode)
        }
    }
    Component.onCompleted: FilmSimulation.setPlotTheme(Theme.mode)
}
