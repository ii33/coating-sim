pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Window
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "."
import "theme_controls" as ThemeControls
import "theme_controls/NumericFormat.js" as NumericFormat

AppWindow {
    id: window
    required property var controller
    property var projectResources: null
    readonly property var geometrySettings: controller.geometrySettings
    title: "膜系设计" + (controller && controller.uiState.dirty ? " *" : "")
    caption: title
    readonly property string thicknessSummaryDetail: {
        const summary = String(controller.thicknessSummaryText || "")
        const description = String(controller.thicknessSummaryDescription || "")
        return summary + (description.length > 0 ? " · " + description : "")
    }
    statusText: controller.uiState.error
                ? controller.uiState.error
                : controller.expressionBusy ? "正在处理表达式…"
                : controller.uiState.catalogWarning
                  ? "全局材料库读取失败；工程已保存材料仍可用"
                : String(controller.thicknessSummaryText || "")
                  + (controller.uiState.message
                     ? " · " + controller.uiState.message : "")
    statusSeverity: controller.uiState.error ? "error"
                    : controller.expressionBusy ? "busy"
                    : controller.uiState.catalogWarning ? "warning"
                    : controller.uiState.message ? "info" : "info"
    statusDetail: controller.uiState.error
                  ? controller.uiState.error + (window.thicknessSummaryDetail
                                                ? " · " + window.thicknessSummaryDetail : "")
                  : controller.expressionBusy
                    ? "正在处理表达式…" + (window.thicknessSummaryDetail
                                           ? " · " + window.thicknessSummaryDetail : "")
                    : controller.uiState.catalogWarning
                      ? controller.uiState.catalogWarning
                        + (window.thicknessSummaryDetail
                           ? " · " + window.thicknessSummaryDetail : "")
                    : window.thicknessSummaryDetail
                      + (controller.uiState.message
                         ? " · " + controller.uiState.message : "")
    readonly property rect screenArea: Qt.rect(Screen.virtualX, Screen.virtualY,
                                                Screen.desktopAvailableWidth,
                                                Screen.desktopAvailableHeight)
    readonly property real screenWidthForLayout: screenArea.width > 0
                                                 ? screenArea.width
                                                 : (Screen.width > 0 ? Screen.width : 1280)
    readonly property real screenHeightForLayout: screenArea.height > 0
                                                  ? screenArea.height
                                                  : (Screen.height > 0 ? Screen.height : 800)
    minimumWidth: Math.min(screenWidthForLayout,
                           Math.max(320, Math.min(960, screenWidthForLayout - 32)))
    minimumHeight: Math.min(screenHeightForLayout,
                            Math.max(320, Math.min(620, screenHeightForLayout - 32)))
    width: Math.min(screenWidthForLayout, Math.max(minimumWidth, 1100))
    height: Math.min(screenHeightForLayout, Math.max(minimumHeight, 760))
    initialMaximized: false
    pageManagesWindowGeometry: true
    color: Theme.snapshot.colorWindow
    property bool allowClose: false
    property bool sidebarExpanded: width >= 1030
    property string activeSidebarSection: "expression"
    property var sidebarFocusItems: ({})
    property int sidebarFocusRequestSerial: 0
    // Keep the layer editor and the feature rail from competing for horizontal
    // space at tablet-sized windows; at 720 px the side panel must stack below.
    readonly property bool sidebarUsesBottomLayout: width < 900
        || (sidebarExpanded && width < 1030)
    property string activeFilmSide: "front"
    property string pendingOverwritePath: ""
    signal simulationRequested(var design)
    onActiveFocusItemChanged: {
        if (window.focusIsInsideSidebar(activeFocusItem)) {
            if (window.sidebarFocusItems[window.activeSidebarSection] === activeFocusItem)
                return
            const focusCopy = Object.assign({}, window.sidebarFocusItems || {})
            focusCopy[window.activeSidebarSection] = activeFocusItem
            window.sidebarFocusItems = focusCopy
        }
    }
    onSidebarUsesBottomLayoutChanged: {
        if (sidebarUsesBottomLayout && focusIsInsideSidebar(activeFocusItem))
            pageTabs.forceActiveFocus()
    }

    function commitExpressionDraft(normalize) {
        return expressionPanel.commitDraft(normalize)
    }
    function runDesignShortcut(callback) {
        pageTabs.forceActiveFocus()
        Qt.callLater(function() {
            if (!window.commitExpressionDraft(true)) return
            callback()
        })
    }
    function openDocumentFile(url) {
        const chosenFile = url
        window.runDesignShortcut(function() {
            if (window.controller.projectMode) window.controller.requestImportFile(chosenFile)
            else window.controller.requestOpenFile(chosenFile)
        })
    }
    function focusIsInsideSidebar(item) {
        let current = item
        while (current) {
            if (current === sidebarStack) return true
            current = current.parent
        }
        return false
    }
    function rememberSidebarFocus(section) {
        if (!focusIsInsideSidebar(activeFocusItem)) return
        const focusCopy = Object.assign({}, sidebarFocusItems || {})
        focusCopy[section] = activeFocusItem
        sidebarFocusItems = focusCopy
    }
    function restoreSidebarFocusLater(section) {
        const request = ++sidebarFocusRequestSerial
        Qt.callLater(function() {
            if (request !== window.sidebarFocusRequestSerial
                    || !window.sidebarExpanded
                    || window.activeSidebarSection !== section)
                return
            const target = window.sidebarFocusItems[section]
            if (target && target.visible && target.forceActiveFocus)
                target.forceActiveFocus()
        })
    }
    function toggleSidebar() {
        if (sidebarExpanded) {
            rememberSidebarFocus(activeSidebarSection)
            if (focusIsInsideSidebar(activeFocusItem)) pageTabs.forceActiveFocus()
            ++sidebarFocusRequestSerial
            sidebarExpanded = false
        } else {
            sidebarExpanded = true
            restoreSidebarFocusLater(activeSidebarSection)
        }
    }
    function openSidebarSection(section) {
        rememberSidebarFocus(activeSidebarSection)
        if (focusIsInsideSidebar(activeFocusItem)) pageTabs.forceActiveFocus()
        if (activeSidebarSection === "generator" && section !== "generator")
            generatorPanel.clearPreview()
        if (activeSidebarSection === "library" && section !== "library")
            referencePanel.clearPreview()
        activeSidebarSection = section
        sidebarExpanded = true
        if (section === "library") controller.refreshSavedDesigns()
        restoreSidebarFocusLater(section)
    }
    function restoreWindowGeometry() {
        const area = window.screenArea
        if (area.width <= 0 || area.height <= 0) return
        const restoredWidth = geometrySettings.savedWidth > 0 ? geometrySettings.savedWidth : width
        const restoredHeight = geometrySettings.savedHeight > 0 ? geometrySettings.savedHeight : height
        width = Math.min(area.width, Math.max(minimumWidth, restoredWidth))
        height = Math.min(area.height, Math.max(minimumHeight, restoredHeight))
        const fallbackX = area.x + Math.round((area.width - width) / 2)
        const fallbackY = area.y + Math.round((area.height - height) / 2)
        x = Math.max(area.x, Math.min(geometrySettings.savedX >= 0 ? geometrySettings.savedX : fallbackX,
                                     area.x + area.width - width))
        y = Math.max(area.y, Math.min(geometrySettings.savedY >= 0 ? geometrySettings.savedY : fallbackY,
                                     area.y + area.height - height))
    }
    function saveWindowGeometry() {
        if (visibility !== Window.Windowed || width <= 0 || height <= 0) return
        geometrySettings.save(x, y, width, height)
    }
    Timer {
        id: geometrySaveTimer
        interval: 350
        repeat: false
        onTriggered: window.saveWindowGeometry()
    }

    Shortcut {
        sequence: "F4"
        context: Qt.WindowShortcut
        onActivated: window.toggleSidebar()
    }
    Shortcut {
        sequence: "F5"
        context: Qt.WindowShortcut
        onActivated: window.runDesignShortcut(function() {
            window.controller.openSimulationAndCalculate()
        })
    }
    Shortcut {
        sequence: "F6"
        context: Qt.WindowShortcut
        onActivated: window.runDesignShortcut(function() {
            window.controller.openOptimizationAndStart()
        })
    }

    Component.onCompleted: window.restoreWindowGeometry()
    onXChanged: geometrySaveTimer.restart()
    onYChanged: geometrySaveTimer.restart()
    onWidthChanged: geometrySaveTimer.restart()
    onHeightChanged: geometrySaveTimer.restart()

    palette.window: Theme.snapshot.colorWindow
    palette.windowText: Theme.snapshot.colorText
    palette.base: Theme.snapshot.colorSurface
    palette.alternateBase: Theme.snapshot.colorSurfaceRaised
    palette.text: Theme.snapshot.colorText
    palette.button: Theme.snapshot.colorSurfaceRaised
    palette.buttonText: Theme.snapshot.colorText
    palette.highlight: Theme.snapshot.colorAccent
    palette.highlightedText: Theme.snapshot.colorTextOnAccent
    palette.mid: Theme.snapshot.colorBorder
    palette.placeholderText: Theme.snapshot.colorTextMuted

    function indexFor(options, value) {
        for (let i = 0; i < options.length; ++i)
            if (String(options[i].id || "") === String(value || "")) return i
        return -1
    }
    function selectedOptionId(options, index) {
        if (!options || index < 0 || index >= options.length) return ""
        return String(options[index].id || "")
    }
    function activeSide() {
        if (pageTabs.currentIndex === 0) return "front"
        if (pageTabs.currentIndex === 1) return "back"
        return window.activeFilmSide
    }
    function mediumForSide(side) {
        return (controller.designMeta.first_surface || "front") === side
               ? "incident_medium" : "exit_medium"
    }
    function mediumRoleForSide(side) {
        return (controller.designMeta.first_surface || "front") === side ? "入射介质" : "出射介质"
    }
    function commitSubstrateSetSelection(index) {
        const options = window.controller.opticalSetsForSubstrate(
            window.controller.designMeta.substrate.material_id || "")
        const selectedId = window.selectedOptionId(options, index)
        if (selectedId !== window.controller.designMeta.substrate.optical_set_id)
            window.controller.setSubstrateValue("optical_set_id", String(selectedId || ""))
    }
    function ensureSubstratePage() {
        if (substratePageLoader.source.toString().length === 0) {
            substratePageLoader.setSource(Qt.resolvedUrl("FilmDesignSubstratePage.qml"), {
                editorWindow: window,
                pageTabs: pageTabs
            })
        }
        substratePageLoader.active = true
    }
    function saveOrChoosePath() {
        if (!window.commitExpressionDraft(true)) return
        if (controller.projectMode || controller.uiState.path) controller.saveCurrent()
        else saveDialog.open()
    }

    onClosing: function(close) {
        saveWindowGeometry()
        if (!allowClose && !controller.initializationReady) {
            controller.cancelInitialization()
            close.accepted = true
            controller.markSourceClosed()
            return
        }
        if (!allowClose && !window.commitExpressionDraft(true)) {
            close.accepted = false
            return
        }
        if (!allowClose && controller.dirty) {
            close.accepted = false
            controller.requestClose()
        }
        if (close.accepted) controller.markSourceClosed()
    }
    Connections {
        target: window.controller
        function onConfirmationRequested(action, message) {
            confirmationDialog.action = action
            confirmationDialog.message = message
            confirmationDialog.open()
        }
        function onOverwriteRequested(path) {
            window.pendingOverwritePath = path
            overwriteDialog.open()
        }
        function onCloseConfirmed() {
            window.allowClose = true
            Qt.callLater(window.close)
        }
        function onErrorChanged() {
            if (window.controller.error && !errorDialog.visible)
                errorDialog.open()
        }
    }

    FileDialog {
        id: openDialog
        title: window.controller.projectMode ? "导入膜系到当前项目" : "打开膜系设计"
        currentFolder: window.controller.defaultDirectory
        fileMode: FileDialog.OpenFile
        nameFilters: ["膜系设计 / TFCalc 明文 (*.cfilm *.tfd)", "Film design (*.cfilm)", "TFCalc 明文 (*.tfd)"]
        onAccepted: window.openDocumentFile(selectedFile)
    }
    TfcalcImportDialog {
        id: tfcalcImportDialog
        controller: window.controller
        hostWidth: window.width
        hostHeight: window.height
        anchors.centerIn: parent
    }
    Connections {
        target: window.controller
        function onTfcalcImportRequested() {
            tfcalcImportDialog.resetInputs()
            tfcalcImportDialog.open()
        }
        function onTfcalcImportApplied() { tfcalcImportDialog.close() }
    }
    FileDialog {
        id: saveDialog
        title: "膜系设计另存为"
        currentFolder: window.controller.defaultDirectory
        fileMode: FileDialog.SaveFile
        options: FileDialog.DontConfirmOverwrite
        nameFilters: ["Film design (*.cfilm)"]
        defaultSuffix: "cfilm"
        onAccepted: if (window.commitExpressionDraft(true)) window.controller.saveFile(selectedFile, false)
    }
    ThemeControls.ThemeDialogBase {
        id: confirmationDialog
        objectName: "filmDesignReplacementConfirmation"
        property string action: ""
        property string message: ""
        title: "确认替换膜系？"
        modal: true
        anchors.centerIn: parent
        width: Math.min(520, window.width - Theme.snapshot.spaceXl * 2)
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Label {
            text: confirmationDialog.message
            color: Theme.snapshot.colorText
            wrapMode: Text.WordWrap
            padding: Theme.snapshot.spaceMd
        }
        onAccepted: {
            window.controller.resolvePendingAction(true)
        }
        onRejected: window.controller.resolvePendingAction(false)
    }
    ThemeControls.ThemeDialogBase {
        id: overwriteDialog
        title: "覆盖已有文件？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Label {
            text: "The target .cfilm file already exists. Replace it with the current design?"
            color: Theme.snapshot.colorText
            wrapMode: Text.WordWrap
            padding: Theme.snapshot.spaceMd
        }
        onAccepted: {
            window.controller.saveFilePath(window.pendingOverwritePath, true)
            window.pendingOverwritePath = ""
        }
        onRejected: {
            window.controller.resolvePendingAction(false)
            window.pendingOverwritePath = ""
        }
    }
    ThemeControls.ThemeDialogBase {
        id: errorDialog
        title: "膜系设计错误"
        modal: true
        anchors.centerIn: parent
        width: Math.min(600, window.width - Theme.snapshot.spaceXl * 2)
        standardButtons: Dialog.Ok
        contentItem: Label {
            text: window.controller.uiState.error
            color: Theme.snapshot.colorError
            wrapMode: Text.WordWrap
            padding: Theme.snapshot.spaceMd
        }
        onAccepted: window.controller.clearError()
        onRejected: window.controller.clearError()
    }

    ThemeControls.ThemeDialogBase {
        id: templateSaveDialog
        title: "保存表达式模板"
        modal: true
        anchors.centerIn: parent
        width: Math.min(560, window.width - Theme.snapshot.spaceXl * 2)
        standardButtons: Dialog.Save | Dialog.Cancel
        contentItem: ColumnLayout {
            spacing: Theme.snapshot.spaceSm
            ThemeControls.ThemeField { id: templateName; Layout.fillWidth: true; placeholderText: "模板名称"; maximumLength: 120 }
            TextArea {
                id: templateNote
                Layout.fillWidth: true
                Layout.preferredHeight: 96
                placeholderText: "备注（可选）"
                wrapMode: TextEdit.Wrap
            }
        }
        onAccepted: {
            if (!window.commitExpressionDraft(true) ||
                    !window.controller.saveExpressionTemplateAsync(templateName.text, templateNote.text))
                open()
        }
        onRejected: { templateName.clear(); templateNote.clear() }
        onOpened: { templateName.clear(); templateNote.clear() }
    }
    ThemeControls.ThemeDialogBase {
        id: templateLoadDialog
        title: "加载表达式模板"
        modal: true
        anchors.centerIn: parent
        width: Math.min(650, window.width - Theme.snapshot.spaceXl * 2)
        standardButtons: Dialog.NoButton
        onOpened: window.controller.refreshExpressionTemplatesAsync()
        contentItem: ColumnLayout {
            spacing: Theme.snapshot.spaceMd
            Label {
                Layout.fillWidth: true
                text: "模板只替换表达式文本和符号表；点击“展开到当前膜堆”后才会改变层序。"
                color: Theme.snapshot.colorTextMuted
                wrapMode: Text.WordWrap
            }
            Label {
                Layout.fillWidth: true
                visible: window.controller.expressionTemplates.length === 0
                text: "尚未保存表达式模板。可先关闭此窗口并使用“保存模板…”创建模板。"
                color: Theme.snapshot.colorInfo
                wrapMode: Text.WordWrap
            }
            ComboBox {
                id: templateBox
                Layout.fillWidth: true
                model: window.controller.expressionTemplates
                textRole: "name"
                valueRole: "template_id"
                delegate: ItemDelegate {
                    required property var modelData
                    width: templateBox.width
                    text: modelData.name || ""
                    ToolTip.visible: hovered && !!modelData.note
                    ToolTip.text: modelData.note || ""
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ThemeControls.ThemeButton { text: "取消"; onClicked: templateLoadDialog.close() }
                ThemeControls.ThemeButton {
                    text: "加载所选"
                    enabled: templateBox.currentIndex >= 0 && !window.controller.expressionBusy
                    onClicked: {
                        if (window.commitExpressionDraft(true) &&
                                window.controller.applyExpressionTemplateAsync(templateBox.currentValue))
                            templateLoadDialog.close()
                    }
                }
            }
        }
    }

    ThemeControls.ThemeDialogBase {
        id: schematicDialog
        title: "膜堆结构示意"
        modal: false
        anchors.centerIn: parent
        width: Math.min(1260, window.width - Theme.snapshot.spaceXl * 2)
        height: Math.min(540, window.height - Theme.snapshot.spaceXl * 2)
        standardButtons: Dialog.Close
        contentItem: ColumnLayout {
            spacing: Theme.snapshot.spaceMd
            Label {
                Layout.fillWidth: true
                text: "从左到右：正面介质与膜堆、基片、背面膜堆与背面介质。介质角色按文档的入射面标记；膜层宽度按物理厚度比例示意。"
                color: Theme.snapshot.colorTextMuted
                wrapMode: Text.WordWrap
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: Theme.snapshot.spaceXs
                Label {
                    Layout.preferredWidth: 116
                    text: "正面侧 · " + window.mediumRoleForSide("front") + "\n"
                          + window.controller.mediumLabel(window.mediumForSide("front"))
                    wrapMode: Text.WordWrap
                    color: Theme.snapshot.colorText
                }
                Repeater {
                    model: window.controller.schematicLayers("front").slice().reverse()
                    delegate: Rectangle {
                        required property var modelData
                        required property int index
                        Layout.fillHeight: true
                        Layout.preferredWidth: Math.max(2, Number(modelData.displayWidth))
                        radius: Theme.snapshot.radiusSm
                        color: Theme.snapshot.colorAccent
                        Label {
                            anchors.centerIn: parent
                            width: Math.max(28, parent.width - Theme.snapshot.spaceSm)
                            text: "第" + (index + 1) + "层\n" + modelData.name + "\n" + (modelData.physicalNmValue === null || modelData.physicalNmValue === undefined ? modelData.physicalNm : NumericFormat.format(modelData.physicalNmValue, Qt.locale(), 3)) + " nm"
                            wrapMode: Text.WordWrap
                            horizontalAlignment: Text.AlignHCenter
                            color: Theme.snapshot.colorTextOnAccent
                        font.pointSize: Theme.snapshot.fontSizeSmall
                        }
                        ToolTip.visible: frontLayerHover.hovered
                        ToolTip.text: "第" + (index + 1) + "层：" + modelData.name + " / " + modelData.setName +
                                      (modelData.sourceRange ? "\n来源波段：" + modelData.sourceRange : "") +
                                      "\n物理厚度：" + (modelData.physicalNmValue === null || modelData.physicalNmValue === undefined ? modelData.physicalNm : NumericFormat.format(modelData.physicalNmValue, Qt.locale(), 3)) + " nm"
                        HoverHandler { id: frontLayerHover }
                    }
                }
                Rectangle {
                    Layout.preferredWidth: 220
                    Layout.fillHeight: true
                    radius: Theme.snapshot.radiusMd
                    color: Theme.snapshot.colorSurfaceRaised
                    border.color: Theme.snapshot.colorBorderStrong
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: Theme.snapshot.spaceMd
                        Label {
                            Layout.fillWidth: true
                            text: "基片\n" + window.controller.substrateLabel()
                            wrapMode: Text.WordWrap
                            horizontalAlignment: Text.AlignHCenter
                            color: Theme.snapshot.colorText
                            font.weight: Theme.snapshot.fontWeightEmphasis
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "\u5b9e\u9645\u539a\u5ea6\uff1a" + NumericFormat.format(window.controller.designMeta.substrate.thickness.value, Qt.locale(), 3) + " " + window.controller.designMeta.substrate.thickness.unit
                            wrapMode: Text.WordWrap
                            horizontalAlignment: Text.AlignHCenter
                            color: Theme.snapshot.colorTextMuted
                        }
                    }
                }
                Repeater {
                    model: window.controller.designMeta.substrate.sides === "double"
                           ? window.controller.schematicLayers("back") : []
                    delegate: Rectangle {
                        required property var modelData
                        required property int index
                        Layout.fillHeight: true
                        Layout.preferredWidth: Math.max(2, Number(modelData.displayWidth))
                        radius: Theme.snapshot.radiusSm
                        color: Theme.snapshot.colorSuccess
                        Label {
                            anchors.centerIn: parent
                            width: Math.max(28, parent.width - Theme.snapshot.spaceSm)
                            text: "第" + (index + 1) + "层\n" + modelData.name + "\n" + (modelData.physicalNmValue === null || modelData.physicalNmValue === undefined ? modelData.physicalNm : NumericFormat.format(modelData.physicalNmValue, Qt.locale(), 3)) + " nm"
                            wrapMode: Text.WordWrap
                            horizontalAlignment: Text.AlignHCenter
                            color: Theme.snapshot.colorTextOnAccent
                        font.pointSize: Theme.snapshot.fontSizeSmall
                        }
                        ToolTip.visible: backLayerHover.hovered
                        ToolTip.text: "第" + (index + 1) + "层：" + modelData.name + " / " + modelData.setName +
                                      (modelData.sourceRange ? "\n来源波段：" + modelData.sourceRange : "") +
                                      "\n物理厚度：" + (modelData.physicalNmValue === null || modelData.physicalNmValue === undefined ? modelData.physicalNm : NumericFormat.format(modelData.physicalNmValue, Qt.locale(), 3)) + " nm"
                        HoverHandler { id: backLayerHover }
                    }
                }
                Label {
                    Layout.preferredWidth: 116
                    text: "背面侧 · " + window.mediumRoleForSide("back") + "\n"
                          + window.controller.mediumLabel(window.mediumForSide("back"))
                          + (window.controller.designMeta.substrate.sides === "single"
                             ? "\n单面模式忽略" : "")
                    wrapMode: Text.WordWrap
                    color: Theme.snapshot.colorText
                }
            }
            Label {
                Layout.fillWidth: true
                text: "参考波长：" + NumericFormat.format(window.controller.designMeta.reference_wavelength_nm, Qt.locale(), 3) + " nm"
                horizontalAlignment: Text.AlignHCenter
                color: Theme.snapshot.colorTextMuted
            }
        }
    }

    ThemeControls.ThemeDialogBase {
        id: noteEditDialog
        objectName: "filmDesignNoteEditDialog"
        property string draftText: ""
        title: "编辑膜系备注"
        modal: true
        anchors.centerIn: parent
        width: Math.min(520, window.width - Theme.snapshot.spaceXl * 2)
        height: Math.min(420, window.height - Theme.snapshot.spaceXl * 2)
        standardButtons: Dialog.Ok | Dialog.Cancel
        onOpened: {
            draftText = window.controller.designMeta.note || ""
            Qt.callLater(function() { noteDraftField.forceActiveFocus() })
        }
        onAccepted: {
            const currentNote = window.controller.designMeta.note || ""
            if (draftText !== currentNote)
                window.controller.setDesignValue("note", draftText)
        }
        onRejected: draftText = window.controller.designMeta.note || ""
        contentItem: ColumnLayout {
            spacing: Theme.snapshot.spaceSm
            Label {
                Layout.fillWidth: true
                text: "确认后才会更新当前设计的备注。取消会丢弃本次输入。"
                color: Theme.snapshot.colorTextMuted
                wrapMode: Text.WordWrap
            }
            ScrollView {
                id: noteDraftScroll
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: availableWidth
                ScrollBar.vertical: ScrollBar {
                    policy: ScrollBar.AsNeeded
                    background: Rectangle {
                        implicitWidth: Theme.snapshot.controlHeightSm / 3
                        radius: width / 2
                        color: Theme.snapshot.colorSurfaceRaised
                    }
                    contentItem: Rectangle {
                        implicitWidth: Theme.snapshot.controlHeightSm / 3
                        radius: width / 2
                        color: Theme.snapshot.colorBorderStrong
                    }
                }
                ScrollBar.horizontal: ScrollBar { policy: ScrollBar.AlwaysOff }
                TextArea {
                    id: noteDraftField
                    objectName: "filmDesignNoteDraft"
                    width: noteDraftScroll.availableWidth
                    height: Math.max(noteDraftScroll.availableHeight,
                                     contentHeight + topPadding + bottomPadding + Theme.snapshot.spaceSm)
                    placeholderText: "输入备注"
                    selectByMouse: true
                    wrapMode: TextEdit.Wrap
                    font.family: Theme.snapshot.fontFamily
                    font.pointSize: Theme.snapshot.fontSizeBody
                    color: Theme.snapshot.colorText
                    placeholderTextColor: Theme.snapshot.colorTextMuted
                    selectionColor: Theme.snapshot.colorSelection
                    selectedTextColor: Theme.snapshot.colorText
                    palette.window: Theme.snapshot.colorWindow
                    palette.windowText: Theme.snapshot.colorText
                    palette.base: Theme.snapshot.colorSurface
                    palette.alternateBase: Theme.snapshot.colorSurfaceRaised
                    palette.text: Theme.snapshot.colorText
                    palette.buttonText: Theme.snapshot.colorText
                    palette.highlight: Theme.snapshot.colorSelection
                    palette.highlightedText: Theme.snapshot.colorText
                    background: Rectangle {
                        color: Theme.snapshot.colorSurface
                        radius: Theme.snapshot.radiusSm
                        border.width: noteDraftField.activeFocus
                                      ? Theme.snapshot.focusRingWidth : Theme.snapshot.borderWidth
                        border.color: noteDraftField.activeFocus
                                      ? Theme.snapshot.colorFocusRing : Theme.snapshot.colorBorder
                    }
                    text: noteEditDialog.draftText
                    onTextChanged: {
                        if (text.length > 20000) text = text.slice(0, 20000)
                        noteEditDialog.draftText = text
                    }
                }
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: Theme.snapshot.spaceMd
        anchors.rightMargin: Theme.snapshot.spaceMd
        anchors.topMargin: window.contentTopMargin + Theme.snapshot.spaceMd
        anchors.bottomMargin: Theme.snapshot.spaceMd
        spacing: Theme.snapshot.spaceSm

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 0
            ResponsiveOperationBar {
                id: filmDesignHeaderFlow
                objectName: "filmDesignHeaderFlow"
                Accessible.name: "操作栏"
                Layout.fillWidth: true
                Layout.preferredHeight: implicitHeight
                spacing: Theme.snapshot.spaceXs
                height: implicitHeight
                ProjectModuleSelector {
                    id: projectModuleSelector
                    navigation: window.controller
                    currentModuleKey: "film_design"
                    showSelector: window.controller.projectMode
                    selectorObjectName: "filmDesignProjectModuleSelector"
                    projectNameObjectName: "filmDesignProjectName"
                    selectorAccessibleName: "当前项目模块"
                }
                ProjectResourceControls {
                    projectResources: window.projectResources
                    width: visible ? Math.min(520, filmDesignHeaderFlow.width) : 0
                    beforeDocumentAction: function(action, documentId) {
                        pageTabs.forceActiveFocus()
                        return window.commitExpressionDraft(true)
                    }
                }
                ThemeControls.ThemeField {
                    id: nameField
                    visible: !window.projectResources
                    objectName: "filmDesignNameField"
                    width: 210
                    accessibleLabel: "膜系名称"
                    placeholderText: "膜系名称"
                    maximumLength: 120
                    selectByMouse: true
                    text: window.controller.designMeta.name || ""
                    onTextEdited: window.controller.markEditingDirty()
                    onEditingFinished: window.controller.setDesignValue("name", text)
                }
                ThemeControls.ThemeButton { visible: !window.controller.projectMode; text: "新建"; onClicked: if (window.commitExpressionDraft(true)) window.controller.requestNewDesign() }
                ThemeControls.ThemeButton { visible: !window.controller.projectMode; text: "打开"; onClicked: window.runDesignShortcut(function() { openDialog.open() }) }
                ThemeControls.ThemeButton { visible: !window.controller.projectMode; text: "保存"; onClicked: window.saveOrChoosePath() }
                ThemeControls.ThemeButton { visible: !window.controller.projectMode; text: "另存为"; onClicked: if (window.commitExpressionDraft(true)) saveDialog.open() }
                ThemeControls.ThemeButton { text: "结构示意"; onClicked: schematicDialog.open() }
                ThemeControls.ThemeButton {
                    objectName: "filmDesignOptimizeButton"
                    visible: !window.controller.projectMode
                    text: "厚度优化"
                    Accessible.name: "打开膜系厚度优化窗口"
                    onClicked: if (window.commitExpressionDraft(true)) window.controller.openOptimization()
                }
                ThemeControls.ThemeButton {
                    id: noteButton
                    visible: !window.projectResources
                    objectName: "filmDesignNoteButton"
                    text: "备注"
                    Accessible.name: "编辑膜系备注"
                    ToolTip.visible: hovered
                    ToolTip.text: window.controller.designMeta.note
                                  ? "当前备注：\n" + window.controller.designMeta.note
                                  : "当前备注：暂无"
                    onClicked: noteEditDialog.open()
                }
            }
        }

        GridLayout {
            id: workspaceGrid
            objectName: "filmDesignWorkspaceGrid"
            columns: window.sidebarUsesBottomLayout ? 1 : 2
            Layout.fillWidth: true
            Layout.fillHeight: true
            rowSpacing: Theme.snapshot.spaceMd
            columnSpacing: Theme.snapshot.spaceMd

            ColumnLayout {
                objectName: "filmDesignDesignColumn"
                Layout.column: 0
                Layout.row: 0
                Layout.fillWidth: true
                Layout.fillHeight: !window.sidebarUsesBottomLayout
                                   || !window.sidebarExpanded
                Layout.preferredHeight: window.sidebarUsesBottomLayout
                                       && window.sidebarExpanded ? 200 : 0
                Layout.minimumHeight: window.sidebarUsesBottomLayout ? 200 : 0
                // The editor owns the remaining workspace width. Its children's
                // current geometry must not become a maximum on later resizes.
                Layout.minimumWidth: 0
                Layout.preferredWidth: 0
                Layout.maximumWidth: Number.POSITIVE_INFINITY
                spacing: Theme.snapshot.spaceSm
                FilmDesignLayerColumnMetrics {
                    id: filmLayerMetrics
                    themeTokens: window.themeColors
                    availableWidth: frontView.width
                    horizontalPadding: Theme.snapshot.spaceXs
                    columnGap: Theme.snapshot.spaceXs
                }
                TabBar {
                    id: pageTabs
                    objectName: "filmDesignPageTabs"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    Layout.maximumWidth: Number.POSITIVE_INFINITY
                    clip: true
                    onCurrentIndexChanged: {
                        if (currentIndex === 0) window.activeFilmSide = "front"
                        else if (currentIndex === 1) window.activeFilmSide = "back"
                        else if (currentIndex === 2) window.ensureSubstratePage()
                    }
                    TabButton {
                        objectName: "filmDesignTabFront"
                        enabled: window.controller.initializationReady
                        Layout.fillWidth: true
                        text: "正面膜堆（" + window.controller.designMeta.front_layer_count + "）"
                        implicitHeight: 42
                        padding: Theme.snapshot.spaceSm
                        font.pointSize: Theme.snapshot.fontSizeBody
                    }
                    TabButton {
                        id: backTab
                        objectName: "filmDesignTabBack"
                        Layout.fillWidth: true
                        text: "背面膜堆（" + window.controller.designMeta.back_layer_count + "）"
                        implicitHeight: 42
                        padding: Theme.snapshot.spaceSm
                        font.pointSize: Theme.snapshot.fontSizeBody
                        enabled: window.controller.designMeta.substrate.sides === "double"
                        contentItem: Label {
                            text: backTab.text
                            objectName: "filmDesignTabBackText"
                            color: backTab.enabled ? Theme.snapshot.colorText : Theme.snapshot.colorTextMuted
                            font.pointSize: Theme.snapshot.fontSizeBody
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                            elide: Text.ElideRight
                        }
                    }
                    TabButton {
                        objectName: "filmDesignTabSubstrate"
                        Layout.fillWidth: true
                        text: "基片与介质"
                        implicitHeight: 42
                        padding: Theme.snapshot.spaceSm
                        font.pointSize: Theme.snapshot.fontSizeBody
                    }
                }
                RowLayout {
                    objectName: "filmDesignLayerHeader"
                    visible: pageTabs.currentIndex < 2
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    Layout.maximumWidth: Number.POSITIVE_INFINITY
                    Layout.leftMargin: filmLayerMetrics.horizontalPadding
                    Layout.rightMargin: filmLayerMetrics.horizontalPadding
                    spacing: filmLayerMetrics.columnGap
                    ThemeControls.ThemeLabel {
                        objectName: "filmDesignHeaderLayerNumber"
                        text: "层号"
                        Layout.preferredWidth: filmLayerMetrics.indexWidth
                        Layout.minimumWidth: filmLayerMetrics.indexWidth
                        Layout.maximumWidth: filmLayerMetrics.indexWidth
                        color: Theme.snapshot.colorTextMuted
                        horizontalAlignment: Text.AlignHCenter
                    }
                    ThemeControls.ThemeLabel {
                        objectName: "filmDesignHeaderMaterial"
                        text: "膜材质"
                        Layout.preferredWidth: filmLayerMetrics.materialWidth
                        Layout.minimumWidth: filmLayerMetrics.materialWidth
                        Layout.maximumWidth: filmLayerMetrics.materialWidth
                        color: Theme.snapshot.colorTextMuted
                        elide: Text.ElideRight
                    }
                    ThemeControls.ThemeLabel {
                        objectName: "filmDesignHeaderDataset"
                        text: "光学数据集"
                        Layout.preferredWidth: filmLayerMetrics.datasetWidth
                        Layout.minimumWidth: filmLayerMetrics.datasetWidth
                        Layout.maximumWidth: filmLayerMetrics.datasetWidth
                        color: Theme.snapshot.colorTextMuted
                        elide: Text.ElideRight
                    }
                    ThemeControls.ThemeLabel {
                        objectName: "filmDesignHeaderQw"
                        text: "光学厚度\nQW"
                        Layout.preferredWidth: filmLayerMetrics.qwWidth
                        Layout.minimumWidth: filmLayerMetrics.qwWidth
                        Layout.maximumWidth: filmLayerMetrics.qwWidth
                        Layout.minimumHeight: implicitHeight
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        wrapMode: Text.WordWrap
                        color: Theme.snapshot.colorTextMuted
                    }
                    ThemeControls.ThemeLabel {
                        objectName: "filmDesignHeaderNm"
                        text: "物理厚度\nnm"
                        Layout.preferredWidth: filmLayerMetrics.nmWidth
                        Layout.minimumWidth: filmLayerMetrics.nmWidth
                        Layout.maximumWidth: filmLayerMetrics.nmWidth
                        Layout.minimumHeight: implicitHeight
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        wrapMode: Text.WordWrap
                        color: Theme.snapshot.colorTextMuted
                    }
                    ThemeControls.ThemeLabel {
                        objectName: "filmDesignHeaderInsert"
                        text: "+"
                        Layout.preferredWidth: filmLayerMetrics.insertWidth
                        Layout.minimumWidth: filmLayerMetrics.insertWidth
                        Layout.maximumWidth: filmLayerMetrics.insertWidth
                        color: Theme.snapshot.colorTextMuted
                        horizontalAlignment: Text.AlignHCenter
                    }
                    ThemeControls.ThemeLabel {
                        objectName: "filmDesignHeaderDelete"
                        text: "×"
                        Layout.preferredWidth: filmLayerMetrics.deleteWidth
                        Layout.minimumWidth: filmLayerMetrics.deleteWidth
                        Layout.maximumWidth: filmLayerMetrics.deleteWidth
                        color: Theme.snapshot.colorTextMuted
                        horizontalAlignment: Text.AlignHCenter
                    }
                }
                StackLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumWidth: 0
                    Layout.maximumWidth: Number.POSITIVE_INFINITY
                    clip: true
                    id: filmDesignPropertyPages
                    currentIndex: pageTabs.currentIndex

            
                
                
                
                    
                    ListView {
                        id: frontView
                        objectName: "filmDesignFrontView"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.minimumWidth: 0
                        Layout.maximumWidth: Number.POSITIVE_INFINITY
                        clip: true
                        reuseItems: true
                        cacheBuffer: 48
                        spacing: Theme.snapshot.spaceXs
                        model: window.controller.frontLayers
                        delegate: FilmDesignLayerRow {
                            width: frontView.width
                            controller: window.controller
                            side: "front"
                            columnMetrics: filmLayerMetrics
                            onColumnFocusRequested: function(columnName, rowDelta) {
                                const targetIndex = index + rowDelta
                                if (targetIndex < 0 || targetIndex >= frontView.count) return
                                frontView.positionViewAtIndex(targetIndex, ListView.Contain)
                                frontView.forceLayout()
                                const target = frontView.itemAtIndex(targetIndex)
                                if (target) target.focusColumnField(columnName)
                            }
                            onRemoveRequested: function(id) { window.controller.deleteLayer("front", id) }
                            onInsertRequested: function(id) { window.controller.insertLayerAfter("front", id) }
                        }
                        footer: RowLayout {
                            width: frontView.width
                            visible: frontView.count === 0
                            spacing: Theme.snapshot.spaceSm
                            Label {
                                Layout.fillWidth: true
                                text: "正面膜堆为空。"
                                color: Theme.snapshot.colorTextMuted
                            }
                            ThemeControls.ThemeButton {
                                objectName: "filmDesignEmptyAddFront"
                                text: "添加膜层"
                                onClicked: window.controller.addLayer("front")
                            }
                        }
                    }
                    ListView {
                        id: backView
                        objectName: "filmDesignBackView"
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.minimumWidth: 0
                        Layout.maximumWidth: Number.POSITIVE_INFINITY
                        clip: true
                        reuseItems: true
                        cacheBuffer: 48
                        spacing: Theme.snapshot.spaceXs
                        model: window.controller.backLayers
                        delegate: FilmDesignLayerRow {
                            width: backView.width
                            controller: window.controller
                            side: "back"
                            columnMetrics: filmLayerMetrics
                            onColumnFocusRequested: function(columnName, rowDelta) {
                                const targetIndex = index + rowDelta
                                if (targetIndex < 0 || targetIndex >= backView.count) return
                                backView.positionViewAtIndex(targetIndex, ListView.Contain)
                                backView.forceLayout()
                                const target = backView.itemAtIndex(targetIndex)
                                if (target) target.focusColumnField(columnName)
                            }
                            onRemoveRequested: function(id) { window.controller.deleteLayer("back", id) }
                            onInsertRequested: function(id) { window.controller.insertLayerAfter("back", id) }
                        }
                        footer: RowLayout {
                            width: backView.width
                            visible: backView.count === 0
                            spacing: Theme.snapshot.spaceSm
                            Label {
                                Layout.fillWidth: true
                                text: "背面膜堆为空。单面模式下会保留背面设计，但不参与计算。"
                                color: Theme.snapshot.colorTextMuted
                                wrapMode: Text.WordWrap
                            }
                            ThemeControls.ThemeButton {
                                objectName: "filmDesignEmptyAddBack"
                                text: "添加膜层"
                                onClicked: window.controller.addLayer("back")
                            }
                        }
                    }
                
                


            

Item {
                objectName: "filmDesignSubstratePageContainer"
                Layout.fillWidth: true
                Layout.fillHeight: true
                Loader {
                    id: substratePageLoader
                    objectName: "filmDesignSubstratePageLoader"
                    anchors.fill: parent
                    active: false
                    asynchronous: false
                }
                ColumnLayout {
                    anchors.centerIn: parent
                    visible: pageTabs.currentIndex === 2
                             && substratePageLoader.status === Loader.Loading
                    spacing: Theme.snapshot.spaceSm
                    BusyIndicator { running: parent.visible; Layout.alignment: Qt.AlignHCenter }
                    Label { text: "正在加载基片设置…"; color: Theme.snapshot.colorTextMuted }
                }
            }
        
}
            }
            GridLayout {
                id: expressionSidebar
                objectName: "filmDesignExpressionSidebar"
                columns: window.sidebarUsesBottomLayout ? 1 : 2
                Layout.column: window.sidebarUsesBottomLayout ? 0 : 1
                Layout.row: window.sidebarUsesBottomLayout ? 1 : 0
                Layout.fillWidth: window.sidebarUsesBottomLayout
                Layout.fillHeight: !window.sidebarUsesBottomLayout
                                   || window.sidebarExpanded
                Layout.preferredWidth: window.sidebarUsesBottomLayout ? workspaceGrid.width
                                      : window.sidebarExpanded
                                        ? Math.min(640, Math.max(520, Math.round(workspaceGrid.width * 0.42))) : 64
                Layout.minimumWidth: window.sidebarUsesBottomLayout ? 0
                                    : window.sidebarExpanded ? 520 : 64
                Layout.maximumWidth: window.sidebarUsesBottomLayout ? 10000
                                    : window.sidebarExpanded
                                      ? Math.min(640, Math.max(520, Math.round(workspaceGrid.width * 0.42))) : 64
                Layout.preferredHeight: window.sidebarUsesBottomLayout
                                       && !window.sidebarExpanded ? 64 : 0
                Layout.minimumHeight: window.sidebarUsesBottomLayout
                                     ? (window.sidebarExpanded ? 200 : 64) : 0
                rowSpacing: 0
                columnSpacing: 0
                GridLayout {
                    id: sidebarRail
                    objectName: "filmDesignSidebarRail"
                    columns: window.sidebarUsesBottomLayout ? 4 : 1
                    rows: window.sidebarUsesBottomLayout ? 1 : 5
                    Layout.column: 0
                    Layout.row: 0
                    Layout.preferredWidth: window.sidebarUsesBottomLayout ? 0 : 64
                    Layout.minimumWidth: window.sidebarUsesBottomLayout ? 0 : 64
                    Layout.maximumWidth: window.sidebarUsesBottomLayout ? 10000 : 64
                    Layout.preferredHeight: window.sidebarUsesBottomLayout ? 64 : 0
                    Layout.fillWidth: window.sidebarUsesBottomLayout
                    Layout.fillHeight: !window.sidebarUsesBottomLayout
                    rowSpacing: Theme.snapshot.spaceXs
                    columnSpacing: Theme.snapshot.spaceXs
                    function choose(section) { window.openSidebarSection(section) }
                    ToolButton {
                        objectName: "filmDesignSidebarExpression"
                        Layout.column: 0
                        Layout.row: 0
                        Layout.fillWidth: true
                        Layout.fillHeight: window.sidebarUsesBottomLayout
                        Layout.preferredHeight: window.sidebarUsesBottomLayout ? 64 : 56
                        text: "表达式"
                        checkable: true
                        checked: window.activeSidebarSection === "expression"
                        Accessible.name: "表达式"
                        ToolTip.visible: hovered
                        ToolTip.text: "表达式面板"
                        contentItem: Label {
                            text: "表达式"
                            wrapMode: Text.WordWrap
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                            color: parent.checked ? Theme.snapshot.colorTextOnAccent
                                                   : Theme.snapshot.colorText
                            font.pointSize: Theme.snapshot.fontSizeBody
                        }
                        background: Rectangle {
                            color: parent.checked ? Theme.snapshot.colorAccent : "transparent"
                            radius: Theme.snapshot.radiusSm
                        }
                        onClicked: sidebarRail.choose("expression")
                    }
                    ToolButton {
                        objectName: "filmDesignSidebarGenerator"
                        Layout.column: window.sidebarUsesBottomLayout ? 1 : 0
                        Layout.row: window.sidebarUsesBottomLayout ? 0 : 1
                        Layout.fillWidth: true
                        Layout.fillHeight: window.sidebarUsesBottomLayout
                        Layout.preferredHeight: window.sidebarUsesBottomLayout ? 64 : 56
                        checkable: true
                        checked: window.activeSidebarSection === "generator"
                        Accessible.name: "参数设计"
                        ToolTip.visible: hovered
                        ToolTip.text: "参数化膜系设计"
                        contentItem: Label {
                            text: "参数设计"
                            wrapMode: Text.WordWrap
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                            color: parent.checked ? Theme.snapshot.colorTextOnAccent
                                                   : Theme.snapshot.colorText
                            font.pointSize: Theme.snapshot.fontSizeBody
                        }
                        background: Rectangle {
                            color: parent.checked ? Theme.snapshot.colorAccent : "transparent"
                            radius: Theme.snapshot.radiusSm
                        }
                        onClicked: sidebarRail.choose("generator")
                    }
                    ToolButton {
                        objectName: "filmDesignSidebarLibrary"
                        Layout.column: window.sidebarUsesBottomLayout ? 2 : 0
                        Layout.row: window.sidebarUsesBottomLayout ? 0 : 2
                        Layout.fillWidth: true
                        Layout.fillHeight: window.sidebarUsesBottomLayout
                        Layout.preferredHeight: window.sidebarUsesBottomLayout ? 64 : 56
                        checkable: true
                        checked: window.activeSidebarSection === "library"
                        Accessible.name: "设计库"
                        ToolTip.visible: hovered
                        ToolTip.text: "内置模板与用户设计库"
                        contentItem: Label {
                            text: "设计库"
                            wrapMode: Text.WordWrap
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                            color: parent.checked ? Theme.snapshot.colorTextOnAccent
                                                   : Theme.snapshot.colorText
                            font.pointSize: Theme.snapshot.fontSizeBody
                        }
                        background: Rectangle {
                            color: parent.checked ? Theme.snapshot.colorAccent : "transparent"
                            radius: Theme.snapshot.radiusSm
                        }
                        onClicked: sidebarRail.choose("library")
                    }
                    Item {
                        objectName: "filmDesignSidebarSpacer"
                        visible: !window.sidebarUsesBottomLayout
                        Layout.column: 0
                        Layout.row: 4
                        Layout.fillHeight: true
                    }
                    ThemeControls.ThemeDoubleChevron {
                        objectName: "filmDesignSidebarToggle"
                        Layout.column: window.sidebarUsesBottomLayout ? 3 : 0
                        Layout.row: window.sidebarUsesBottomLayout ? 0 : 3
                        Layout.alignment: Qt.AlignHCenter
                        Layout.preferredWidth: 24
                        Layout.minimumWidth: 24
                        Layout.maximumWidth: 24
                        Layout.fillHeight: window.sidebarUsesBottomLayout
                        Layout.preferredHeight: 42
                        text: window.sidebarExpanded ? "折叠当前侧栏功能" : "展开当前侧栏功能"
                        direction: window.sidebarUsesBottomLayout
                                   ? window.sidebarExpanded ? "down" : "up"
                                   : window.sidebarExpanded ? "right" : "left"
                        Accessible.name: window.sidebarExpanded ? "折叠当前侧栏功能（F4）" : "展开当前侧栏功能（F4）"
                        ToolTip.visible: hovered
                        ToolTip.text: Accessible.name
                        onClicked: window.toggleSidebar()
                    }
                }
                Item {
                    id: sidebarStack
                    objectName: "filmDesignSidebarStack"
                    Layout.column: window.sidebarUsesBottomLayout ? 0 : 1
                    Layout.row: window.sidebarUsesBottomLayout ? 1 : 0
                    visible: window.sidebarExpanded
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumWidth: 0
                    property int currentIndex: window.activeSidebarSection === "expression" ? 0
                                               : window.activeSidebarSection === "generator" ? 1 : 2

                    Item {
                        objectName: "filmDesignExpressionPage"
                        anchors.fill: parent
                        visible: sidebarStack.currentIndex === 0
                        ScrollView {
                            id: expressionScroll
                            objectName: "filmDesignExpressionScroll"
                            anchors.fill: parent
                            clip: true
                            contentWidth: availableWidth
                            ScrollBar.vertical: ScrollBar {
                                id: expressionVerticalBar
                                objectName: "filmDesignExpressionVerticalScrollBar"
                            }
                            FilmDesignExpressionPanel {
                                id: expressionPanel
                                themeTokens: window.themeColors
                                // Reserve the overlaid scrollbar using its actual
                                // themed width, rather than enlarging child rows.
                                width: Math.max(0, expressionScroll.availableWidth
                                    - expressionVerticalBar.width - Theme.snapshot.spaceXs)
                                controller: window.controller
                                targetSide: window.activeSide()
                                onSaveTemplateRequested: templateSaveDialog.open()
                                onLoadTemplateRequested: templateLoadDialog.open()
                            }
                        }
                    }
                    Item {
                        objectName: "filmDesignGeneratorPage"
                        anchors.fill: parent
                        visible: sidebarStack.currentIndex === 1
                        FilmDesignGeneratorPanel {
                            id: generatorPanel
                            objectName: "filmDesignGeneratorPanel"
                            anchors.fill: parent
                            controller: window.controller
                        }
                    }
                    Item {
                        objectName: "filmDesignLibraryPage"
                        anchors.fill: parent
                        visible: sidebarStack.currentIndex === 2
                        ScrollView {
                            id: designLibraryScroll
                            objectName: "filmDesignLibraryScroll"
                            anchors.fill: parent
                            clip: true
                            contentWidth: availableWidth
                            ColumnLayout {
                                // Reserve the width of the overlaid native vertical scrollbar.
                                width: Math.max(0, designLibraryScroll.availableWidth - 18)
                                spacing: Theme.snapshot.spaceSm
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    Label {
                                        Layout.fillWidth: true
                                        text: window.controller.projectMode ? "项目膜系" : "設計庫"
                                        color: Theme.snapshot.colorText
                                        font.pointSize: Theme.snapshot.fontSizeHeading
                                        font.weight: Theme.snapshot.fontWeightEmphasis
                                    }
                                    ThemeControls.ThemeButton {
                                        Layout.fillWidth: true
                                        text: "刷新"
                                        onClicked: window.controller.refreshSavedDesigns()
                                    }
                                    ThemeControls.ThemeButton {
                                        objectName: "filmDesignSaveToLibrary"
                                        Layout.fillWidth: true
                                        text: window.controller.projectMode ? "保存项目膜系" : "保存当前设计到库"
                                        Accessible.name: window.controller.projectMode ? "将当前膜系设计保存到项目" : "将当前膜系设计保存到 SQLite 用户设计库"
                                        enabled: window.controller.initializationReady
                                        onClicked: {
                                            if (window.commitExpressionDraft(true)
                                                    && window.controller.saveToLibrary())
                                                window.controller.refreshSavedDesigns()
                                        }
                                    }
                                }
                                FilmDesignReferencePanel {
                                    id: referencePanel
                                    controller: window.controller
                                    Layout.fillWidth: true
                                }
                                Label {
                                    Layout.fillWidth: true
                                    text: "内置经典模板"
                                    color: Theme.snapshot.colorText
                                    font.weight: Theme.snapshot.fontWeightEmphasis
                                }
                                Repeater {
                                    model: window.controller.builtinDesignTemplates
                                    delegate: Rectangle {
                                        required property var modelData
                                        Layout.fillWidth: true
                                        implicitHeight: templateCard.implicitHeight + Theme.snapshot.spaceMd * 2
                                        color: Theme.snapshot.colorSurfaceRaised
                                        border.color: Theme.snapshot.colorBorder
                                        radius: Theme.snapshot.radiusSm
                                        ColumnLayout {
                                            id: templateCard
                                            anchors.fill: parent
                                            anchors.margins: Theme.snapshot.spaceMd
                                            spacing: Theme.snapshot.spaceXs
                                            Label {
                                                Layout.fillWidth: true
                                                text: String(modelData.title || "")
                                                wrapMode: Text.WordWrap
                                                color: Theme.snapshot.colorText
                                                font.weight: Theme.snapshot.fontWeightEmphasis
                                            }
                                            Label {
                                                Layout.fillWidth: true
                                                text: String(modelData.description || "")
                                                wrapMode: Text.WordWrap
                                                color: Theme.snapshot.colorTextMuted
                                            }
                                            Label {
                                                Layout.fillWidth: true
                                                text: String(modelData.reference_section || "") + "\n" + String(modelData.source_url || "")
                                                wrapMode: Text.WordWrap
                                                color: Theme.snapshot.colorTextMuted
                                            }
                                            ThemeControls.ThemeButton {
                                                Layout.fillWidth: true
                                                text: "打开参数设计"
                                                onClicked: {
                                                    generatorPanel.changeTemplate(String(modelData.stable_id || ""))
                                                    window.openSidebarSection("generator")
                                                }
                                            }
                                        }
                                    }
                                }
                                Label {
                                    Layout.fillWidth: true
                                    text: "用户保存设计"
                                    color: Theme.snapshot.colorText
                                    font.weight: Theme.snapshot.fontWeightEmphasis
                                }
                                Label {
                                    Layout.fillWidth: true
                                    visible: window.controller.savedDesigns.length === 0
                                    text: "SQLite 用户设计库为空。保存操作可从膜系设计页发起。"
                                    wrapMode: Text.WordWrap
                                    color: Theme.snapshot.colorTextMuted
                                }
                                Repeater {
                                    model: window.controller.savedDesigns
                                    delegate: Rectangle {
                                        required property var modelData
                                        Layout.fillWidth: true
                                        implicitHeight: savedCard.implicitHeight + Theme.snapshot.spaceMd * 2
                                        color: Theme.snapshot.colorSurfaceRaised
                                        border.color: Theme.snapshot.colorBorder
                                        radius: Theme.snapshot.radiusSm
                                        ColumnLayout {
                                            id: savedCard
                                            anchors.fill: parent
                                            anchors.margins: Theme.snapshot.spaceMd
                                            spacing: Theme.snapshot.spaceXs
                                            Label {
                                                Layout.fillWidth: true
                                                text: String(modelData.name || "未命名设计")
                                                wrapMode: Text.WordWrap
                                                color: Theme.snapshot.colorText
                                                font.weight: Theme.snapshot.fontWeightEmphasis
                                            }
                                            Label {
                                                Layout.fillWidth: true
                                                text: String(modelData.note || "")
                                                wrapMode: Text.WordWrap
                                                color: Theme.snapshot.colorTextMuted
                                            }
                                            ThemeControls.ThemeButton {
                                                Layout.fillWidth: true
                                                text: "加载用户设计"
                                                onClicked: window.controller.requestLoadSavedDesign(String(modelData.design_id || ""))
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

    }

    Rectangle {
        id: initializationOverlay
        objectName: "filmDesignInitializationOverlay"
        anchors.fill: parent
        visible: !window.controller.initializationReady
        enabled: visible
        focus: visible
        color: Theme.snapshot.colorWindow
        Keys.onPressed: function(event) { event.accepted = true }

        MouseArea {
            anchors.fill: parent
            z: 1
            acceptedButtons: Qt.AllButtons
            preventStealing: true
            onPressed: function(mouse) { mouse.accepted = true }
        }

        Rectangle {
            anchors.centerIn: parent
            z: 2
            width: Math.min(480, Math.max(260, initializationOverlay.width - Theme.snapshot.spaceXl * 2))
            implicitHeight: initializationContent.implicitHeight + Theme.snapshot.spaceXl * 2
            radius: Theme.snapshot.radiusMd
            color: Theme.snapshot.colorSurface
            border.color: Theme.snapshot.colorBorder

            ColumnLayout {
                id: initializationContent
                anchors.fill: parent
                anchors.margins: Theme.snapshot.spaceXl
                spacing: Theme.snapshot.spaceMd

                BusyIndicator {
                    Layout.alignment: Qt.AlignHCenter
                    running: window.controller.initializationInProgress
                    visible: running
                }
                Label {
                    Layout.fillWidth: true
                    text: window.controller.initializationInProgress
                          ? "正在读取材料目录并准备膜系编辑器…"
                          : window.controller.initializationState === "cancelled"
                            ? "初始化已取消"
                            : "膜系编辑器初始化失败"
                    color: window.controller.initializationState === "failed"
                           ? Theme.snapshot.colorError : Theme.snapshot.colorText
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                    font.pointSize: Theme.snapshot.fontSizeTitle
                }
                Label {
                    Layout.fillWidth: true
                    visible: !!window.controller.initializationError
                    text: window.controller.initializationError
                    color: Theme.snapshot.colorTextMuted
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                }
                RowLayout {
                    Layout.alignment: Qt.AlignHCenter
                    visible: !window.controller.initializationInProgress
                    spacing: Theme.snapshot.spaceSm
                    ThemeControls.ThemeButton {
                        objectName: "filmDesignInitializationRetry"
                        text: "重试"
                        onClicked: window.controller.retryInitialization()
                    }
                    ThemeControls.ThemeButton {
                        text: "关闭"
                        onClicked: window.close()
                    }
                }
            }
        }
    }
}
