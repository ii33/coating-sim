pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Window
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "."
import "theme_controls" as ThemeControls
import "theme_controls/NumericFormat.js" as NumericFormat

AppWindow {
    id: window
    property bool projectCloseAuthorized: false
    property bool synthesisPreviewHighlighted: false
    onActiveChanged: {
        if (active && controller.searchMode === "synthesis" && !controller.busy)
            controller.refreshSynthesisMaterials()
    }
    property var projectNavigation: null
    property var projectResources: null
    readonly property var themeTokens: window.themeColors
    required property var controller
    titleBase: window.controller.searchMode === "synthesis" ? "膜系自动合成" : (window.controller.searchMode === "reduction" ? "减层 / 减厚优化" : "膜系厚度优化")
    captionBase: titleBase
    projectNameForTitle: projectNavigation ? projectNavigation.projectName : ""
    statusText: controller.statusText
    statusSeverity: controller.loading || controller.busy ? "busy"
                     : controller.error.length > 0 ? "error"
                     : controller.hasCandidate
                       ? (controller.targetsMet ? "success" : "warning")
                     : controller.configurationRestoreNotice.length > 0 ? "warning" : "info"
    statusDetail: controller.statusText
                  + (controller.error.length > 0 && controller.statusText.indexOf(controller.error) < 0
                     ? "\n\n" + controller.error : "")
                  + (controller.configurationRestoreNotice.length > 0
                     ? "\n\n" + controller.configurationRestoreNotice : "")
                  + (controller.busy && controller.progressText !== controller.statusText
                     ? "\n\n" + controller.progressText : "")
                  + (controller.hasCandidate ? "\n\n" + controller.resultSummary
                     + "\n" + controller.resultMessage
                     + "\n" + controller.resultValidationSummary
                     + (controller.searchMode === "fixed_topology"
                        ? "\n" + controller.resultRunMetadataSummary : "") : "")
                  + (controller.structureResultSummary.length > 0
                     ? "\n" + controller.structureResultSummary : "")
                  + (controller.structureConfigurationError.length > 0
                     ? "\n" + controller.structureConfigurationError : "")
    color: window.themeTokens.colorWindow
    property bool allowClose: false
    property string pendingOverwritePath: ""
    property int themeRevision: Theme.revision
    property bool detailsExpanded: false
    readonly property bool detailsDocked: detailsExpanded && width >= 1100
    property var nativeF4Combos: []

    // Repeater.itemAt is statically QQuickItem; this parameter is the dynamic
    // material-row delegate contract, including its modelData and selector alias.
    function restoreSynthesisMaterialPopup(row: var, rowId: string): bool {
        if (!row || row.modelData.id !== rowId) return false
        row.materialSelector.forceActiveFocus()
        row.materialSelector.popup.open()
        return true
    }
    function openSynthesisMaterialPopup(rowId) {
        for (let i = 0; i < synthesisMaterialRows.count; ++i) {
            if (window.restoreSynthesisMaterialPopup(synthesisMaterialRows.itemAt(i), rowId)) return
        }
    }

    function toggleDetails() {
        if (detailsExpanded)
            shortcutFocusSink.forceActiveFocus()
        detailsExpanded = !detailsExpanded
    }
    function registerNativeF4Combo(combo) {
        nativeF4Combos = nativeF4Combos.concat([combo])
    }
    function isNativeComboItem(item: var): bool {
        // Native wrappers expose these optional properties dynamically.
        return item.nativeF4Popup === true
                || (item.popup !== undefined && item.model !== undefined)
    }
    function nativeComboOwnsF4() {
        let item = window.activeFocusItem
        while (item) {
            if (isNativeComboItem(item))
                return true
            item = item.parent
        }
        for (const combo of nativeF4Combos)
            if (combo && combo.popup && combo.popup.visible) return true
        return false
    }
    Shortcut {
        objectName: "filmOptimizationSidebarShortcut"
        sequence: "F4"
        context: Qt.WindowShortcut
        enabled: !window.nativeComboOwnsF4()
        onActivated: window.toggleDetails()
    }

    Item {
        id: shortcutFocusSink
        objectName: "filmOptimizationShortcutFocusSink"
        x: 0
        y: 0
        width: 1
        height: 1
        opacity: 0
        activeFocusOnTab: false
        Accessible.ignored: true
    }

    // Owns guarded dynamic draft commit for editors whose concrete type is not
    // known at compile time. The parameter is intentionally untyped (var) so the
    // method lookup stays dynamic instead of assumed on every QQuickItem.
    function commitEditorDraft(editor) {
        if (typeof editor.commitDraft === "function")
            editor.commitDraft()
        else if (typeof editor.editingFinished === "function")
            editor.editingFinished()
    }

    function commitActiveEditor() {
        let editor = window.activeFocusItem
        let fallbackEditor = editor
        while (editor && editor !== window && editor !== shortcutFocusSink) {
            if (editor.numericValueEdited !== undefined
                    || editor.commitDraft !== undefined) {
                fallbackEditor = editor
                break
            }
            if (fallbackEditor && fallbackEditor.commitDraft === undefined
                    && typeof fallbackEditor.editingFinished !== "function"
                    && typeof editor.editingFinished === "function")
                fallbackEditor = editor
            editor = editor.parent
        }
        editor = fallbackEditor
        if (editor && editor !== shortcutFocusSink) {
            // Explicitly submit ThemeField drafts before moving focus. Qt may
            // suppress editingFinished for an empty/invalid validator buffer.
            window.commitEditorDraft(editor)
            editor.focus = false
        }
        shortcutFocusSink.forceActiveFocus()
    }

    function startFromShortcut() {
        commitActiveEditor()
        Qt.callLater(function() { window.controller.startOptimizationFromShortcut() })
    }

    function startFromButton() {
        commitActiveEditor()
        Qt.callLater(function() { window.controller.startOptimization() })
    }

    function continueFromButton() {
        // Flush a focused draft first. The controller rechecks the captured source;
        // this must never rebuild the old request from the current editors.
        commitActiveEditor()
        Qt.callLater(function() {
            if (window.controller.canContinueOptimization)
                continuationBudgetDialog.open()
        })
    }

    function selectObjectiveFromPointer(index) {
        const selectedIndex = Number(index)
        commitActiveEditor()
        window.controller.selectObjective(selectedIndex)
    }

    ThemeControls.ThemeDialogBase {
        id: continuationBudgetDialog
        objectName: "filmOptimizationContinueBudgetDialog"
        property string sourceMode: ""
        property string submissionError: ""
        readonly property bool synthesisMode: sourceMode === "synthesis"
        readonly property string budgetError: validateBudget()
        readonly property bool canSubmit: window.controller.canContinueOptimization
                                          && budgetError.length === 0
        title: "从保留结果开始新尝试"
        modal: true
        anchors.centerIn: parent
        width: Math.min(620, window.width - window.themeTokens.spaceLg * 2)
        height: Math.min(650, window.height - window.themeTokens.spaceLg * 2)
        closePolicy: Popup.CloseOnEscape

        function defaultText(value) {
            return value === null || value === undefined ? "" : String(value)
        }
        function validPositiveInteger(text) {
            const value = Number(text)
            return /^[1-9][0-9]*$/.test(text) && Number.isSafeInteger(value) && value > 0
        }
        function validateBudget() {
            if (!synthesisMode) {
                if (!validPositiveInteger(continuationIterations.text)
                        || !validPositiveInteger(continuationEvaluations.text))
                    return "新尝试的最大迭代数与最大评估数均须为正整数。"
                return ""
            }
            if ((continuationCycles.text.length > 0
                    && !validPositiveInteger(continuationCycles.text))
                    || (continuationEvaluations.text.length > 0
                        && !validPositiveInteger(continuationEvaluations.text)))
                return "最大轮次与最大评估数须为正整数，或留空不设此限额。"
            if (continuationSeconds.text.length > 0
                    && (!isFinite(Number(continuationSeconds.text))
                        || Number(continuationSeconds.text) <= 0
                        || !/^([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?$/.test(continuationSeconds.text)))
                return "最长计算时间须为大于 0 的秒数，或留空不设此限额。"
            if (continuationCycles.text.length === 0
                    && continuationEvaluations.text.length === 0
                    && continuationSeconds.text.length === 0)
                return "请为新尝试至少明确设置一项预算限制。"
            return ""
        }
        function newBudget() {
            if (synthesisMode) {
                return {
                    max_cycles: continuationCycles.text.length > 0
                                ? Number(continuationCycles.text) : null,
                    max_evaluations: continuationEvaluations.text.length > 0
                                     ? Number(continuationEvaluations.text) : null,
                    max_seconds: continuationSeconds.text.length > 0
                                 ? Number(continuationSeconds.text) : null
                }
            }
            return {
                max_iterations: Number(continuationIterations.text),
                max_evaluations: Number(continuationEvaluations.text)
            }
        }
        function submitNewAttempt() {
            if (!canSubmit) return
            submissionError = ""
            if (window.controller.continueOptimization(newBudget()))
                accept()
            else
                submissionError = window.controller.error.length > 0
                    ? window.controller.error : "未能开始新尝试，请检查继续条件与新预算。"
        }
        onAboutToShow: {
            // These values belong to the retained frozen request, not to the
            // editable algorithm controls. Refresh them on every opening.
            const defaults = window.controller.continuationBudgetDefaults
            sourceMode = String(defaults.mode ?? "")
            continuationIterations.text = defaultText(defaults.max_iterations)
            continuationCycles.text = defaultText(defaults.max_cycles)
            continuationEvaluations.text = defaultText(defaults.max_evaluations)
            continuationSeconds.text = defaultText(defaults.max_seconds)
            submissionError = ""
        }
        onOpened: {
            if (synthesisMode) continuationCycles.forceActiveFocus()
            else continuationIterations.forceActiveFocus()
        }
        onClosed: {
            if (optimizationContinueButton.enabled && optimizationContinueButton.visible)
                optimizationContinueButton.forceActiveFocus()
        }
        contentItem: ScrollView {
            id: continuationBudgetScroll
            clip: true
            contentWidth: availableWidth
            Accessible.role: Accessible.Dialog
            Accessible.name: continuationBudgetDialog.title
            ColumnLayout {
                width: continuationBudgetScroll.availableWidth
                spacing: window.themeTokens.spaceMd
                Label {
                    objectName: "filmOptimizationContinueExplanation"
                    Layout.fillWidth: true
                    text: "以保留候选为热启动，创建独立的新尝试并重新启动求解器。保留原冻结目标、材料和选项，不使用当前编辑设置；不保留求解器内部状态或完整随机数状态，不是精确续算。"
                    color: window.themeTokens.colorText
                    wrapMode: Text.WordWrap
                }
                Label {
                    objectName: "filmOptimizationContinuePreviousSpent"
                    Layout.fillWidth: true
                    text: window.controller.previousSpentText
                    visible: text.length > 0
                    color: window.themeTokens.colorTextMuted
                    wrapMode: Text.WordWrap
                }
                Label {
                    Layout.fillWidth: true
                    text: "以下限额仅用于这一次新尝试，独立计量，不是累计预算，也不扣除原尝试已耗额度。已预填原限制，请明确确认或修改。"
                    color: window.themeTokens.colorText
                    wrapMode: Text.WordWrap
                }
                Label {
                    Layout.fillWidth: true
                    visible: continuationBudgetDialog.synthesisMode
                    text: "自动合成至少填写一项限制；留空表示新尝试不设该项限额。"
                    color: window.themeTokens.colorTextMuted
                    wrapMode: Text.WordWrap
                }
                GridLayout {
                    Layout.fillWidth: true
                    columns: 2
                    columnSpacing: window.themeTokens.spaceMd
                    rowSpacing: window.themeTokens.spaceSm
                    Label {
                        visible: !continuationBudgetDialog.synthesisMode
                        text: "新最大迭代数"
                        color: window.themeTokens.colorText
                    }
                    ThemeControls.ThemeField {
                        id: continuationIterations
                        objectName: "filmOptimizationContinueMaxIterationsField"
                        visible: !continuationBudgetDialog.synthesisMode
                        Layout.fillWidth: true
                        accessibleLabel: "新尝试最大迭代数"
                        inputMethodHints: Qt.ImhDigitsOnly
                        validator: RegularExpressionValidator { regularExpression: /^[1-9][0-9]*$/ }
                    }
                    Label {
                        visible: continuationBudgetDialog.synthesisMode
                        text: "新最大轮次"
                        color: window.themeTokens.colorText
                    }
                    ThemeControls.ThemeField {
                        id: continuationCycles
                        objectName: "filmOptimizationContinueMaxCyclesField"
                        visible: continuationBudgetDialog.synthesisMode
                        Layout.fillWidth: true
                        accessibleLabel: "新尝试最大轮次，留空不设此限额"
                        placeholderText: "不设此限额"
                        inputMethodHints: Qt.ImhDigitsOnly
                        validator: RegularExpressionValidator { regularExpression: /^([1-9][0-9]*)?$/ }
                    }
                    Label { text: "新最大评估数"; color: window.themeTokens.colorText }
                    ThemeControls.ThemeField {
                        id: continuationEvaluations
                        objectName: "filmOptimizationContinueMaxEvaluationsField"
                        Layout.fillWidth: true
                        accessibleLabel: continuationBudgetDialog.synthesisMode
                                         ? "新尝试最大评估数，留空不设此限额" : "新尝试最大评估数"
                        placeholderText: continuationBudgetDialog.synthesisMode ? "不设此限额" : "正整数"
                        inputMethodHints: Qt.ImhDigitsOnly
                        validator: RegularExpressionValidator { regularExpression: /^([1-9][0-9]*)?$/ }
                    }
                    Label {
                        visible: continuationBudgetDialog.synthesisMode
                        text: "新最长时间（秒）"
                        color: window.themeTokens.colorText
                    }
                    ThemeControls.ThemeField {
                        id: continuationSeconds
                        objectName: "filmOptimizationContinueMaxSecondsField"
                        visible: continuationBudgetDialog.synthesisMode
                        Layout.fillWidth: true
                        accessibleLabel: "新尝试最长计算时间，单位秒，留空不设此限额"
                        placeholderText: "不设此限额"
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        validator: RegularExpressionValidator {
                            regularExpression: /^(([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?)?$/
                        }
                    }
                }
                Label {
                    objectName: "filmOptimizationContinueBudgetError"
                    Layout.fillWidth: true
                    text: continuationBudgetDialog.budgetError
                    visible: text.length > 0
                    color: window.themeTokens.colorError
                    wrapMode: Text.WordWrap
                }
                Label {
                    objectName: "filmOptimizationContinueDialogDisabledReason"
                    Layout.fillWidth: true
                    text: window.controller.continueDisabledReason
                    visible: !window.controller.canContinueOptimization && text.length > 0
                    color: window.themeTokens.colorError
                    wrapMode: Text.WordWrap
                }
                Label {
                    objectName: "filmOptimizationContinueDialogFallback"
                    Layout.fillWidth: true
                    text: window.controller.priorRunFallbackText
                    visible: text.length > 0
                    color: window.themeTokens.colorTextMuted
                    wrapMode: Text.WordWrap
                }
                Label {
                    objectName: "filmOptimizationContinueSubmissionError"
                    Layout.fillWidth: true
                    text: continuationBudgetDialog.submissionError
                    visible: text.length > 0
                    color: window.themeTokens.colorError
                    wrapMode: Text.WordWrap
                }
            }
        }
        footer: DialogButtonBox {
            ThemeControls.ThemeButton {
                objectName: "filmOptimizationContinueBudgetCancelButton"
                text: "取消"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
            ThemeControls.ThemeButton {
                objectName: "filmOptimizationContinueBudgetSubmitButton"
                text: "开始新尝试"
                primary: true
                enabled: continuationBudgetDialog.canSubmit
                DialogButtonBox.buttonRole: DialogButtonBox.ActionRole
                onClicked: continuationBudgetDialog.submitNewAttempt()
            }
        }
    }

    ThemeControls.ThemeDialogBase {
        id: sensitivityDialog
        objectName: "filmSensitivityDialog"
        title: "膜厚敏感度与预期误差"
        modal: true
        anchors.centerIn: parent
        width: Math.min(920, window.width - window.themeTokens.spaceLg * 2)
        height: Math.min(780, window.height - window.themeTokens.spaceLg * 2)
        standardButtons: Dialog.Close
        closePolicy: Popup.CloseOnEscape
        contentItem: FilmSensitivityWorkflowPanel {
            controller: window.controller.sensitivityController
            themeTokens: window.themeTokens
            onApplyRequested: sensitivityApplyDialog.open()
            onSaveRequested: sensitivityReportDialog.open()
        }
    }
    ThemeControls.ThemeDialogBase {
        id: sensitivityApplyDialog
        objectName: "filmSensitivityApplyConfirmDialog"
        title: "应用敏感度优化候选？"
        modal: true
        anchors.centerIn: parent
        width: Math.min(520, window.width - 40)
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Label {
            objectName: "filmSensitivityApplySummaryLabel"
            text: window.controller.sensitivityController.idealApplyConfirmationSummary
            color: window.themeTokens.colorText
            wrapMode: Text.WordWrap
        }
        onAccepted: window.controller.sensitivityController.applySelectedBest()
    }
    FileDialog {
        id: sensitivityReportDialog
        title: "保存完整敏感性报告"
        fileMode: FileDialog.SaveFile
        nameFilters: ["JSON report (*.json)"]
        defaultSuffix: "json"
        onAccepted: window.controller.sensitivityController.saveReportUrl(selectedFile, false)
    }
    Connections {
        target: window.controller.sensitivityController
        function onOverwriteRequested(path) {
            sensitivityOverwriteDialog.targetPath = path
            sensitivityOverwriteDialog.open()
        }
    }
    ThemeControls.ThemeDialogBase {
        id: sensitivityOverwriteDialog
        property string targetPath: ""
        title: "覆盖已有敏感性报告？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Label {
            text: "确认替换：" + sensitivityOverwriteDialog.targetPath
            color: window.themeTokens.colorText
            wrapMode: Text.WordWrap
        }
        onAccepted: window.controller.sensitivityController.saveReport(targetPath, true)
    }

    Shortcut {
        objectName: "filmOptimizationSimulationShortcut"
        sequence: "F5"
        context: Qt.WindowShortcut
        onActivated: {
            window.commitActiveEditor()
            Qt.callLater(function() { window.controller.openSimulationAndCalculate() })
        }
    }

    Shortcut {
        objectName: "filmOptimizationStartShortcut"
        sequence: "F6"
        context: Qt.WindowShortcut
        enabled: window.controller.canStartFromShortcut && window.controller.computeTarget.canSubmit
        onActivated: window.startFromShortcut()
    }

    onClosing: function(close) {
        if (!allowClose && !projectCloseAuthorized) {
            close.accepted = false
            if (objectiveAngleEditor.dirty && !objectiveAngleEditor.commitDraft()) return
            controller.requestClose()
        } else {
            controller.shutdown(1000)
        }
    }

    Connections {
        target: window.controller
        function onCloseConfirmed() {
            window.allowClose = true
            Qt.callLater(window.close)
        }
        function onCloseConfirmationRequested() { closeDialog.open() }
        function onChanged() {
            if (!window.controller.hasCandidate) window.synthesisPreviewHighlighted = false
        }
        function onSynthesisPreviewRequested() {
            window.synthesisPreviewHighlighted = true
            comparisonCanvas.forceActiveFocus()
            comparisonCanvas.requestPaint()
        }
        function onApplyConfirmationRequested(message) {
            applyDialog.message = message
            applyDialog.open()
        }
        function onSourceReuseConfirmationRequested() { sourceReuseDialog.open() }
        function onOverwriteRequested(path) {
            window.pendingOverwritePath = path
            overwriteDialog.open()
        }
    }

    FileDialog {
        id: saveDialog
        title: "优化候选另存为"
        currentFolder: window.controller.defaultDirectory
        fileMode: FileDialog.SaveFile
        options: FileDialog.DontConfirmOverwrite
        nameFilters: ["Film design (*.cfilm)"]
        defaultSuffix: "cfilm"
        onAccepted: window.controller.saveCandidate(selectedFile, false)
    }

    ThemeControls.ThemeDialogBase {
        id: applyDialog
        objectName: "filmOptimizationApplyConfirmation"
        property string message: ""
        title: window.controller.searchMode === "synthesis" ? "应用合成候选？" : "应用未达标候选？"
        modal: true
        anchors.centerIn: parent
        width: Math.min(520, window.width - window.themeTokens.spaceXl * 2)
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Label {
            text: applyDialog.message
            color: window.themeTokens.colorText
            wrapMode: Text.WordWrap
            padding: window.themeTokens.spaceMd
        }
        onAccepted: window.controller.confirmApplyCandidate()
    }

    FilmManufacturingRepairDialog {
        id: manufacturingDialog
        controller: window.controller.manufacturingController
        onClosed: {
            if (manufacturingOpenButton.visible && manufacturingOpenButton.enabled)
                manufacturingOpenButton.forceActiveFocus()
        }
    }

    ThemeControls.ThemeDialogBase {
        id: overwriteDialog
        title: "覆盖已有文件？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Label {
            text: "目标 .cfilm 文件已经存在。要用这个优化候选替换它吗？"
            color: window.themeTokens.colorText
            wrapMode: Text.WordWrap
            padding: window.themeTokens.spaceMd
        }
        onAccepted: {
            window.controller.saveCandidatePath(window.pendingOverwritePath, true)
            window.pendingOverwritePath = ""
        }
        onRejected: window.pendingOverwritePath = ""
    }

    ThemeControls.ThemeDialogBase {
        id: closeDialog
        title: "丢弃尚未保存的候选？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Label {
            text: "关闭窗口会丢弃尚未应用或另存的优化候选。"
            color: window.themeTokens.colorText
            wrapMode: Text.WordWrap
            padding: window.themeTokens.spaceMd
        }
        onAccepted: window.controller.resolveClose(true)
        onRejected: window.controller.resolveClose(false)
    }

    ThemeControls.ThemeDialogBase {
        id: sourceReuseDialog
        title: "切换优化来源？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.No
        contentItem: Label {
            text: "切换来源会丢弃尚未应用或另存的优化候选。要继续吗？"
            color: window.themeTokens.colorText
            wrapMode: Text.WordWrap
            padding: window.themeTokens.spaceMd
        }
        onAccepted: window.controller.resolveSourceReuse(true)
        onRejected: window.controller.resolveSourceReuse(false)
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: window.themeTokens.spaceMd
        anchors.rightMargin: window.themeTokens.spaceMd
        anchors.topMargin: window.contentTopMargin + window.themeTokens.spaceMd
        anchors.bottomMargin: window.themeTokens.spaceMd
        spacing: window.themeTokens.spaceSm

        Rectangle {
            id: optimizationToolbar
            objectName: "filmOptimizationToolbar"
            Layout.fillWidth: true
            Layout.preferredHeight: toolbarFlow.implicitHeight + window.themeTokens.spaceSm * 2
            radius: window.themeTokens.radiusMd
            color: window.themeTokens.colorSurface
            border.color: window.themeTokens.colorBorder
            ResponsiveOperationBar {
                id: toolbarFlow
                objectName: "filmOptimizationOperationBar"
                Accessible.name: "操作栏"
                anchors.fill: parent
                anchors.margins: window.themeTokens.spaceSm
                spacing: window.themeTokens.spaceSm
                ProjectModuleSelector {
                    navigation: window.projectNavigation
                    currentModuleKey: "film_optimization"
                }
                ProjectResourceControls {
                    projectResources: window.projectResources
                    width: visible ? Math.min(520, toolbarFlow.width) : 0
                    beforeDocumentAction: function(action, documentId) {
                        window.commitActiveEditor()
                        return !window.controller.busy
                    }
                    extraTransferActions: [
                        {key: "save_candidate", text: "候选结果：保存为工程膜系…", enabled: window.controller.canSave}
                    ]
                    onExtraTransferActionRequested: function(key) {
                        if (key === "save_candidate") window.controller.saveCandidateToProject()
                    }
                }
                ThemeControls.ThemeButton {
                    objectName: "filmOptimizationRefreshSourceButton"
                    text: "刷新设计"
                    visible: window.controller.sourceStale
                    onClicked: window.controller.refreshDesign()
                }
                Label {
                    objectName: "filmOptimizationProgress"
                    property bool operationBarFlexible: true
                    text: window.controller.progressStatistics
                    color: window.themeTokens.colorTextMuted
                    elide: Text.ElideRight
                    Accessible.name: text
                    ToolTip.visible: hovered
                    ToolTip.text: text + "\n" + window.controller.progressText
                    property bool hovered: progressHover.hovered
                    HoverHandler { id: progressHover }
                }
                ComputeTargetSelector {
                    objectName: "filmOptimizationComputeTarget"
                    width: Math.min(170, Math.max(130, window.width * 0.2))
                    compact: true
                    target: window.controller.computeTarget
                    registerNativeF4Combo: window.registerNativeF4Combo
                }
                ThemeControls.ThemeButton {
                    id: optimizationStartButton
                    objectName: "filmOptimizationStartButton"
                    text: "开始优化"
                    enabled: window.controller.canStart && window.controller.computeTarget.canSubmit
                    primary: true
                    onClicked: window.startFromButton()
                }
                ThemeControls.ThemeButton {
                    id: optimizationContinueButton
                    objectName: "filmOptimizationContinueButton"
                    text: "从保留结果继续（重新启动求解器）"
                    enabled: window.controller.canContinueOptimization
                    visible: !window.controller.hasSynthesisSearchProgress
                    onClicked: window.continueFromButton()
                }
                ThemeControls.ThemeButton {
                    objectName: "filmSynthesisContinueSearchButton"
                    text: "继续搜索"
                    visible: window.controller.hasSynthesisSearchProgress
                    enabled: window.controller.canContinueSynthesisSearch
                    onClicked: {
                        window.commitActiveEditor()
                        Qt.callLater(function() { window.controller.continueSynthesisSearch() })
                    }
                }
                ThemeControls.ThemeButton {
                    objectName: "filmSensitivityOpenButton"
                    text: "敏感性…"
                    enabled: !window.controller.busy
                    onClicked: {
                        window.commitActiveEditor()
                        window.controller.sensitivityController.prepare()
                        sensitivityDialog.open()
                    }
                }
                ThemeControls.ThemeButton {
                    id: manufacturingOpenButton
                    objectName: "filmManufacturingOpenButton"
                    text: "工艺薄层修复…"
                    enabled: !window.controller.busy && !window.controller.loading && !window.controller.sourceStale
                    onClicked: {
                        window.commitActiveEditor()
                        window.controller.manufacturingController.prepare()
                        manufacturingDialog.open()
                    }
                }
                ThemeControls.ThemeButton {
                    objectName: "filmOptimizationCancelButton"
                    text: "取消"
                    enabled: window.controller.canCancel
                    onClicked: window.controller.cancelOptimization()
                }
                ThemeControls.ThemeButton {
                    objectName: "filmSynthesisPreviewButton"
                    visible: window.controller.synthesisAuditSummary.length > 0
                    text: window.controller.synthesisPreviewLabel
                    enabled: window.controller.canSave
                    onClicked: window.controller.previewSynthesisCandidate()
                }
                ThemeControls.ThemeButton {
                    objectName: "filmOptimizationApplyButton"
                    text: window.controller.searchMode === "reduction" ? "显式应用减量候选" : "应用"
                    enabled: window.controller.canApply
                    onClicked: window.controller.applyCandidate()
                }
                ThemeControls.ThemeButton {
                    visible: !window.projectResources
                    objectName: "filmOptimizationSaveButton"
                    text: window.controller.projectMode ? "保存项目候选" : "另存候选"
                    enabled: window.controller.canSave
                    onClicked: {
                        if (window.controller.projectMode)
                            window.controller.saveCandidateToProject()
                        else
                            saveDialog.open()
                    }
                }
            }
        }

        Label {
            objectName: "filmOptimizationContinueDisabledReason"
            Layout.fillWidth: true
            text: window.controller.continueDisabledReason
            visible: !window.controller.hasSynthesisSearchProgress && !window.controller.canContinueOptimization && text.length > 0
            color: window.themeTokens.colorTextMuted
            wrapMode: Text.WordWrap
            font.pointSize: window.themeTokens.fontSizeSmall
        }

        Label {
            objectName: "filmSynthesisSearchPhase"
            Layout.fillWidth: true
            text: window.controller.synthesisPhase
            visible: text.length > 0
            font.bold: true
            color: window.themeTokens.colorText
        }
        Label {
            objectName: "filmSynthesisSearchDisabledReason"
            Layout.fillWidth: true
            text: window.controller.synthesisSearchDisabledReason
            visible: window.controller.hasSynthesisSearchProgress && !window.controller.canContinueSynthesisSearch && text.length > 0
            color: window.themeTokens.colorTextMuted
            wrapMode: Text.WordWrap
        }
        Label {
            objectName: "filmOptimizationContinuePriorRunFallback"
            Layout.fillWidth: true
            text: window.controller.priorRunFallbackText
            visible: text.length > 0
            color: window.themeTokens.colorTextMuted
            wrapMode: Text.WordWrap
            font.pointSize: window.themeTokens.fontSizeSmall
        }

        RowLayout {
            id: workArea
            Layout.rightMargin: Math.max(0,
                (window.detailsDocked ? optimizationSidebar.width : 64)
                + window.themeTokens.spaceSm * 2 - window.themeTokens.spaceMd)
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: window.themeTokens.spaceMd

            Rectangle {
                Layout.preferredWidth: Math.max(350, Math.min(430, workArea.width * 0.40))
                Layout.minimumWidth: 330
                Layout.fillHeight: true
                radius: window.themeTokens.radiusMd
                color: window.themeTokens.colorSurface
                border.color: window.themeTokens.colorBorder

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: window.themeTokens.spaceSm
                    spacing: window.themeTokens.spaceSm

                    TabBar {
                        id: setupTabs
                        Layout.fillWidth: true
                        TabButton { text: "目标"; objectName: "filmOptimizationTargetsTab" }
                        TabButton { text: "约束"; objectName: "filmOptimizationConstraintsTab" }
                        TabButton { text: "算法"; objectName: "filmOptimizationAlgorithmTab" }
                    }

                    StackLayout {
                        objectName: "filmOptimizationSetupContent"
                        enabled: !window.controller.busy
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        currentIndex: setupTabs.currentIndex

                        ColumnLayout {
                            spacing: window.themeTokens.spaceXs
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeButton {
                                    objectName: "filmOptimizationAddObjectiveButton"
                                    text: "添加目标"
                                    onClicked: window.controller.addObjective()
                                }
                                ThemeControls.ThemeButton {
                                    objectName: "filmOptimizationDuplicateObjectiveButton"
                                    text: "复制所选"
                                    enabled: window.controller.objectives.length > 0
                                    onClicked: window.controller.duplicateSelectedObjective()
                                }
                                ThemeControls.ThemeButton {
                                    objectName: "filmOptimizationRemoveObjectiveButton"
                                    text: "删除所选"
                                    enabled: window.controller.objectives.length > 0
                                    onClicked: window.controller.removeSelectedObjective()
                                }
                                Item { Layout.fillWidth: true }
                            }
                            Label {
                                Layout.fillWidth: true
                                text: "点数与采样精度自动换算，范围两端都包含。精度表示采样间距，不保证光学计算精度或连续角度性能。"
                                color: window.themeTokens.colorTextMuted
                                wrapMode: Text.WordWrap
                                font.pointSize: window.themeTokens.fontSizeSmall
                            }
                            ListView {
                                id: objectiveList
                                objectName: "filmOptimizationObjectiveList"
                                Layout.fillWidth: true
                                Layout.preferredHeight: 116
                                clip: true
                                spacing: window.themeTokens.spaceXs
                                model: window.controller.objectives
                                delegate: ThemeControls.ThemeButton {
                                    required property var modelData
                                    width: objectiveList.width
                                    text: modelData.display
                                    checkable: true
                                    checked: modelData.selected
                                    Accessible.name: "选择" + modelData.display
                                    focusPolicy: Qt.NoFocus
                                    onPressed: window.selectObjectiveFromPointer(modelData.index)
                                }
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 1
                                color: window.themeTokens.colorBorder
                            }
                            ScrollView {
                                id: objectiveEditorScroll
                                objectName: "filmOptimizationObjectiveEditorScroll"
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                clip: true
                                contentWidth: availableWidth
                                ScrollBar.vertical.policy: ScrollBar.AsNeeded
                                ColumnLayout {
                                    width: objectiveEditorScroll.availableWidth
                                    spacing: window.themeTokens.spaceXs
                                    Label {
                                        visible: window.controller.objectives.length === 0
                                        Layout.fillWidth: true
                                        text: "还没有目标。添加目标后设置波长范围、角度、偏振和 R/T 条件。"
                                        color: window.themeTokens.colorInfo
                                        wrapMode: Text.WordWrap
                                    }
                                    GridLayout {
                                        visible: window.controller.objectives.length > 0
                                        Layout.fillWidth: true
                                        columns: 2
                                        columnSpacing: window.themeTokens.spaceSm
                                        rowSpacing: window.themeTokens.spaceXs
                                        Label { text: "波段 (nm)"; color: window.themeTokens.colorText }
                                        RowLayout {
                                            Layout.fillWidth: true
                                            spacing: window.themeTokens.spaceXs
                                            ThemeControls.ThemeField {
                                                objectName: "filmOptimizationWavelengthMinField"
                                                Layout.fillWidth: true
                                                preserveNumericDisplayText: true
                                                numericDisplayValue: window.controller.selectedObjective.wavelength_min_nm ?? ""
                                                onTextEdited: window.controller.setObjectiveBoundsDraft("wavelength_min_nm", text)
                                                function commitDraft() {
                                                    if (!numericValueEdited) return
                                                    window.controller.commitObjectiveBoundsDraft("wavelength_min_nm", numericEditText)
                                                    numericValueEdited = false
                                                }
                                                onAccepted: commitDraft()
                                                onEditingFinished: commitDraft()
                                            }
                                            Label { text: "-"; color: window.themeTokens.colorTextMuted }
                                            ThemeControls.ThemeField {
                                                objectName: "filmOptimizationWavelengthMaxField"
                                                Layout.fillWidth: true
                                                preserveNumericDisplayText: true
                                                numericDisplayValue: window.controller.selectedObjective.wavelength_max_nm ?? ""
                                                onTextEdited: window.controller.setObjectiveBoundsDraft("wavelength_max_nm", text)
                                                function commitDraft() {
                                                    if (!numericValueEdited) return
                                                    window.controller.commitObjectiveBoundsDraft("wavelength_max_nm", numericEditText)
                                                    numericValueEdited = false
                                                }
                                                onAccepted: commitDraft()
                                                onEditingFinished: commitDraft()
                                            }
                                        }
                                        Label { text: "波长采样数"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeField {
                                            objectName: "filmOptimizationSamplesField"
                                            Layout.fillWidth: true
                                            numericDisplayValue: window.controller.selectedObjective.wavelength_samples ?? ""
                                            validator: RegularExpressionValidator { regularExpression: /^[1-9][0-9]*$/ }
                                            function commitDraft() {
                                                if (!numericValueEdited) return
                                                window.controller.setObjectiveField("wavelength_samples", numericEditText)
                                                numericValueEdited = false
                                            }
                                            onEditingFinished: commitDraft()
                                        }
                                        Label { text: "采样精度 (nm)"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeField {
                                            objectName: "filmOptimizationWavelengthStepField"
                                            Layout.fillWidth: true
                                            enabled: window.controller.selectedObjective.wavelength_min_nm
                                                     !== window.controller.selectedObjective.wavelength_max_nm
                                            numericDisplayValue: window.controller.selectedObjective.wavelength_step_nm ?? ""
                                            validator: DoubleValidator {
                                                bottom: 0; decimals: 17
                                                notation: DoubleValidator.ScientificNotation
                                            }
                                            function commitDraft() {
                                                if (!numericValueEdited) return
                                                window.controller.setObjectiveField("wavelength_step_nm", numericEditText)
                                                numericValueEdited = false
                                            }
                                            onEditingFinished: commitDraft()
                                        }
                                        Label {
                                            objectName: "filmOptimizationSamplingSummary"
                                            Layout.columnSpan: 2
                                            Layout.fillWidth: true
                                            text: window.controller.samplingSummary
                                            color: window.themeTokens.colorTextMuted
                                            wrapMode: Text.WordWrap
                                            font.pointSize: window.themeTokens.fontSizeSmall
                                        }
                                        FilmOptimizationAngleRangeEditor {
                                            id: objectiveAngleEditor
                                            Layout.columnSpan: 2
                                            Layout.fillWidth: true
                                            controller: window.controller
                                            sourceObjective: window.controller.selectedObjective
                                            themeTokens: window.themeTokens
                                        }
                                        Label {
                                            objectName: "filmOptimizationAngleSamplingSummary"
                                            Layout.columnSpan: 2
                                            Layout.fillWidth: true
                                            text: window.controller.angleSamplingSummary
                                            color: window.themeTokens.colorTextMuted
                                            wrapMode: Text.WordWrap
                                            font.pointSize: window.themeTokens.fontSizeSmall
                                        }
                                        Label {
                                            objectName: "filmOptimizationAngleRangeWarning"
                                            Layout.columnSpan: 2
                                            Layout.fillWidth: true
                                            visible: window.controller.selectedObjective.angle_sampling_mode === "points"
                                            text: "此目标保存了显式角度配置："
                                                  + String(window.controller.selectedObjective.angles_text || "")
                                                  + "。原角度尚未改变；请确认当前范围与精度后再开始优化。非均匀角度不能自动替换为均匀范围。"
                                            wrapMode: Text.WordWrap
                                            color: window.themeTokens.colorWarning
                                        }
                                        Label { text: "偏振"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeComboBox {
                                            objectName: "filmOptimizationPolarizationBox"
                                            Layout.fillWidth: true
                                            model: ["非偏振", "s 偏振", "p 偏振"]
                                            currentIndex: Math.max(0, ["unpolarized", "s", "p"].indexOf(window.controller.selectedObjective.polarization))
                                            onActivated: window.controller.setObjectiveField("polarization", ["unpolarized", "s", "p"][currentIndex])
                                        }
                                        Label { text: "指标"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeComboBox {
                                            objectName: "filmOptimizationMetricBox"
                                            Layout.fillWidth: true
                                            model: ["R", "T"]
                                            currentIndex: Math.max(0, model.indexOf(window.controller.selectedObjective.metric))
                                            onActivated: window.controller.setObjectiveField("metric", currentText)
                                        }
                                        CheckBox {
                                            objectName: "filmOptimizationMeanEnabledCheck"
                                            text: "平均值 (%)"
                                            checked: window.controller.selectedObjective.mean?.enabled ?? false
                                            onToggled: {
                                                window.controller.setObjectiveConstraintEnabled("mean", checked)
                                                checked = Qt.binding(() => window.controller.selectedObjective.mean?.enabled ?? false)
                                            }
                                        }
                                        RowLayout {
                                            objectName: "filmOptimizationMeanTargetBoundsRow"
                                            Layout.fillWidth: true
                                            spacing: window.themeTokens.spaceXs
                                            ThemeControls.ThemeField {
                                                accessibleLabel: "平均值下限（%）"
                                                objectName: "filmOptimizationMeanLowerBoundField"
                                                enabled: window.controller.selectedObjective.mean?.enabled ?? false
                                                Layout.fillWidth: true
                                                Layout.minimumWidth: 64
                                                preserveNumericDisplayText: true
                                                numericDisplayValue: window.controller.selectedObjective.mean?.lower_bound_percent ?? ""
                                                onTextEdited: window.controller.setObjectiveBoundsDraft("mean.lower_bound_percent", text)
                                                function commitDraft() {
                                                    if (!numericValueEdited) return
                                                    window.controller.commitObjectiveBoundsDraft("mean.lower_bound_percent", numericEditText)
                                                    numericValueEdited = false
                                                }
                                                onAccepted: commitDraft()
                                                onEditingFinished: commitDraft()
                                            }
                                            Label { text: "–"; color: window.themeTokens.colorTextMuted }
                                            ThemeControls.ThemeField {
                                                accessibleLabel: "平均值上限（%）"
                                                objectName: "filmOptimizationMeanUpperBoundField"
                                                enabled: window.controller.selectedObjective.mean?.enabled ?? false
                                                Layout.fillWidth: true
                                                Layout.minimumWidth: 64
                                                preserveNumericDisplayText: true
                                                numericDisplayValue: window.controller.selectedObjective.mean?.upper_bound_percent ?? ""
                                                onTextEdited: window.controller.setObjectiveBoundsDraft("mean.upper_bound_percent", text)
                                                function commitDraft() {
                                                    if (!numericValueEdited) return
                                                    window.controller.commitObjectiveBoundsDraft("mean.upper_bound_percent", numericEditText)
                                                    numericValueEdited = false
                                                }
                                                onAccepted: commitDraft()
                                                onEditingFinished: commitDraft()
                                            }
                                        }
                                        CheckBox {
                                            objectName: "filmOptimizationEnabledCheck"
                                            text: "绝对值（逐点） (%)"
                                            checked: window.controller.selectedObjective.pointwise?.enabled ?? false
                                            onToggled: {
                                                window.controller.setObjectiveConstraintEnabled("pointwise", checked)
                                                checked = Qt.binding(() => window.controller.selectedObjective.pointwise?.enabled ?? false)
                                            }
                                        }
                                        RowLayout {
                                            objectName: "filmOptimizationTargetBoundsRow"
                                            Layout.fillWidth: true
                                            spacing: window.themeTokens.spaceXs
                                            ThemeControls.ThemeField {
                                                accessibleLabel: "绝对值（逐点）下限（%）"
                                                objectName: "filmOptimizationLowerBoundField"
                                                enabled: window.controller.selectedObjective.pointwise?.enabled ?? false
                                                Layout.fillWidth: true
                                                Layout.minimumWidth: 64
                                                preserveNumericDisplayText: true
                                                numericDisplayValue: window.controller.selectedObjective.pointwise?.lower_bound_percent ?? ""
                                                onTextEdited: window.controller.setObjectiveBoundsDraft("pointwise.lower_bound_percent", text)
                                                function commitDraft() {
                                                    if (!numericValueEdited) return
                                                    window.controller.commitObjectiveBoundsDraft("pointwise.lower_bound_percent", numericEditText)
                                                    numericValueEdited = false
                                                }
                                                onAccepted: commitDraft()
                                                onEditingFinished: commitDraft()
                                            }
                                            Label { text: "–"; color: window.themeTokens.colorTextMuted }
                                            ThemeControls.ThemeField {
                                                accessibleLabel: "绝对值（逐点）上限（%）"
                                                objectName: "filmOptimizationUpperBoundField"
                                                enabled: window.controller.selectedObjective.pointwise?.enabled ?? false
                                                Layout.fillWidth: true
                                                Layout.minimumWidth: 64
                                                preserveNumericDisplayText: true
                                                numericDisplayValue: window.controller.selectedObjective.pointwise?.upper_bound_percent ?? ""
                                                onTextEdited: window.controller.setObjectiveBoundsDraft("pointwise.upper_bound_percent", text)
                                                function commitDraft() {
                                                    if (!numericValueEdited) return
                                                    window.controller.commitObjectiveBoundsDraft("pointwise.upper_bound_percent", numericEditText)
                                                    numericValueEdited = false
                                                }
                                                onAccepted: commitDraft()
                                                onEditingFinished: commitDraft()
                                            }
                                        }
                                        Label {
                                            objectName: "filmOptimizationTargetBoundsWarning"
                                            Layout.columnSpan: 2
                                            Layout.fillWidth: true
                                            visible: window.controller.objectiveBoundsError.length > 0
                                            text: window.controller.objectiveBoundsError
                                            color: window.themeTokens.colorWarning
                                            wrapMode: Text.WordWrap
                                        }
                                        CheckBox {
                                            objectName: "filmSynthesisStrictUpperBoundCheck"
                                            Layout.columnSpan: 2
                                            Layout.fillWidth: true
                                            visible: window.controller.searchMode === "synthesis"
                                            enabled: !window.controller.busy && (window.controller.selectedObjective.pointwise?.enabled ?? false)
                                            text: "逐点约束使用严格上界 <（等于上限不通过）"
                                            checked: window.controller.selectedObjective.pointwise?.strict_upper ?? false
                                            onToggled: {
                                                window.controller.setSynthesisStrictUpperBound(checked)
                                                checked = Qt.binding(() => window.controller.selectedObjective.pointwise?.strict_upper ?? false)
                                            }
                                        }
                                        Label { text: "目标权重"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeField {
                                            objectName: "filmOptimizationWeightField"
                                            Layout.fillWidth: true
                                            numericDisplayValue: window.controller.selectedObjective.weight ?? ""
                                            validator: DoubleValidator { bottom: 0.000001; decimals: 6 }
                                            function commitDraft() {
                                                if (!numericValueEdited) return
                                                window.controller.setObjectiveField("weight", numericEditText)
                                                numericValueEdited = false
                                            }
                                            onEditingFinished: commitDraft()
                                        }
                                        Label {
                                            text: window.controller.searchMode === "synthesis" ? "求解可接受容差 (百分点)" : "达标容限 (百分点)"
                                            color: window.themeTokens.colorText
                                        }
                                        ThemeControls.ThemeField {
                                            objectName: "filmOptimizationToleranceField"
                                            Layout.fillWidth: true
                                            numericDisplayValue: window.controller.acceptableErrorPercent
                                            validator: DoubleValidator { bottom: 0; top: 100; decimals: 5 }
                                            function commitDraft() {
                                                if (!numericValueEdited) return
                                                var raw = numericEditText.trim()
                                                window.controller.setAcceptableErrorPercent(
                                                            raw === "" ? NaN : Number(raw))
                                                numericValueEdited = false
                                            }
                                            onEditingFinished: commitDraft()
                                        }
                                        Label {
                                            objectName: "filmSynthesisToleranceExplanation"
                                            Layout.columnSpan: 2
                                            Layout.fillWidth: true
                                            visible: window.controller.searchMode === "synthesis"
                                            text: "求解容差只表示数值上可接受；原门限名义验收单独判定，不放宽目标，不添加制造余量。未勾选严格上界时使用 ≤。"
                                            color: window.themeTokens.colorTextMuted
                                            wrapMode: Text.WordWrap
                                        }
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        visible: window.controller.error.length > 0
                                        text: window.controller.error
                                        color: window.themeTokens.colorError
                                        wrapMode: Text.WordWrap
                                    }
                                }
                            }
                        }

                        ColumnLayout {
                            spacing: window.themeTokens.spaceXs
                            Label {
                                Layout.fillWidth: true
                                text: "厚度边界使用真空纳米 (nm)；原设计中的 QW 厚度会按参考波长和材料 n 转换。默认选择全部有效膜层，可手动选择子集。"
                                color: window.themeTokens.colorTextMuted
                                wrapMode: Text.WordWrap
                                font.pointSize: window.themeTokens.fontSizeSmall
                            }
                            Label {
                                objectName: "filmOptimizationResourceRiskSummary"
                                Layout.fillWidth: true
                                text: window.controller.resourceRiskSummary
                                color: window.themeTokens.colorWarning
                                wrapMode: Text.WordWrap
                                font.pointSize: window.themeTokens.fontSizeSmall
                            }
                            Label {
                                visible: window.controller.variables.length === 0
                                Layout.fillWidth: true
                                text: "当前膜堆没有可优化的膜层。请先在设计窗口添加固定材料层。"
                                color: window.themeTokens.colorInfo
                                wrapMode: Text.WordWrap
                            }
                            Label {
                                objectName: "filmReductionComparisonLabel"
                                visible: window.controller.searchMode === "reduction" || window.controller.hasReductionResult
                                Layout.fillWidth: true
                                text: window.controller.reductionComparison
                                color: window.themeTokens.colorInfo
                                wrapMode: Text.WordWrap
                                font.pointSize: window.themeTokens.fontSizeSmall
                            }
                            ScrollView {
                                id: variableScroll
                                objectName: "filmOptimizationVariableScroll"
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                clip: true
                                ScrollBar.vertical.policy: ScrollBar.AsNeeded
                                ColumnLayout {
                                    width: variableScroll.availableWidth
                                    spacing: window.themeTokens.spaceXs
                                    Repeater {
                                        model: window.controller.variables
                                        delegate: Rectangle {
                                            id: variableDelegate
                                            required property var modelData
                                            Layout.fillWidth: true
                                            Layout.preferredHeight: 84
                                            radius: window.themeTokens.radiusSm
                                            color: window.themeTokens.colorSurfaceRaised
                                            border.color: window.themeTokens.colorBorder
                                            RowLayout {
                                                anchors.fill: parent
                                                anchors.margins: window.themeTokens.spaceXs
                                                spacing: window.themeTokens.spaceXs
                                                ThemeControls.ThemeButton {
                                                    Layout.preferredWidth: 68
                                                    text: modelData.enabled ? "已选择" : "选择"
                                                    checkable: true
                                                    checked: modelData.enabled
                                                    enabled: variableDelegate.modelData.available || variableDelegate.modelData.enabled
                                                    onClicked: window.controller.setVariableEnabled(modelData.side, modelData.layer_id, !modelData.enabled)
                                                }
                                                ColumnLayout {
                                                    Layout.fillWidth: true
                                                    spacing: 0
                                                    Label {
                                                        Layout.fillWidth: true
                                                        text: modelData.side_label + " · " + modelData.label
                                                        color: window.themeTokens.colorText
                                                        elide: Text.ElideRight
                                                    }
                                                    Label {
                                                        Layout.fillWidth: true
                                                        text: modelData.available
                                                              ? variableDelegate.modelData.set_name + " · 当前 " + NumericFormat.format(variableDelegate.modelData.physical_nm, Qt.locale(), window.controller.filmThicknessDecimals) + " nm"
                                                              : variableDelegate.modelData.set_name + " · 厚度或 n 数据不可用"
                                                        color: variableDelegate.modelData.available
                                                               ? window.themeTokens.colorTextMuted
                                                               : window.themeTokens.colorError
                                                        elide: Text.ElideRight
                                                        font.pointSize: window.themeTokens.fontSizeSmall
                                                    }
                                                    RowLayout {
                                                        Layout.fillWidth: true
                                                        spacing: window.themeTokens.spaceXs
                                                        Label { text: "下限"; color: window.themeTokens.colorTextMuted }
                                                        ThemeControls.ThemeField {
                                                            objectName: "filmOptimizationLower_" + modelData.layer_id
                                                            Layout.fillWidth: true
                                                            enabled: modelData.available
                                                            numericDisplayValue: modelData.lower_nm
                                                            numericDisplayDecimals: window.controller.filmThicknessDecimals
                                                            numericEditDecimals: 3
                                                            validator: DoubleValidator { bottom: 0; decimals: 3 }
                                                            function commitDraft() {
                                                                if (!numericValueEdited) return
                                                                window.controller.setVariableBound(modelData.side, modelData.layer_id, "lower_nm", numericEditText)
                                                                numericValueEdited = false
                                                            }
                                                            onEditingFinished: commitDraft()
                                                        }
                                                        Label { text: "上限"; color: window.themeTokens.colorTextMuted }
                                                        ThemeControls.ThemeField {
                                                            objectName: "filmOptimizationUpper_" + modelData.layer_id
                                                            Layout.fillWidth: true
                                                            enabled: modelData.available
                                                            numericDisplayValue: modelData.upper_nm
                                                            numericDisplayDecimals: window.controller.filmThicknessDecimals
                                                            numericEditDecimals: 3
                                                            placeholderText: "未设上限"
                                                            validator: DoubleValidator { bottom: 0; decimals: 3 }
                                                            function commitDraft() {
                                                                if (!numericValueEdited) return
                                                                window.controller.setVariableBound(modelData.side, modelData.layer_id, "upper_nm", numericEditText)
                                                                numericValueEdited = false
                                                            }
                                                            onEditingFinished: commitDraft()
                                                        }
                                                        Label { text: "nm"; color: window.themeTokens.colorTextMuted }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        ScrollView {
                            id: solverOptionsScroll
                            objectName: "filmOptimizationSolverOptionsScroll"
                            clip: true
                            contentWidth: availableWidth
                            ScrollBar.horizontal: ScrollBar {
                                objectName: "filmOptimizationOptionsHorizontalScrollBar"
                                policy: ScrollBar.AlwaysOff
                                palette: window.palette
                                contentItem: Rectangle {
                                    implicitWidth: window.themeTokens.spaceXs * 1.5
                                    radius: width / 2
                                    color: window.themeTokens.colorBorderStrong
                                    opacity: parent.active ? 0.6 : 0.3
                                }
                                background: Rectangle { color: "transparent" }
                            }
                            ScrollBar.vertical: ScrollBar {
                                objectName: "filmOptimizationOptionsVerticalScrollBar"
                                policy: ScrollBar.AsNeeded
                                palette: window.palette
                                contentItem: Rectangle {
                                    implicitWidth: window.themeTokens.spaceXs * 1.5
                                    radius: width / 2
                                    color: window.themeTokens.colorBorderStrong
                                    opacity: parent.active ? 0.6 : 0.3
                                }
                                background: Rectangle { color: "transparent" }
                            }
                            ColumnLayout {
                                objectName: "filmOptimizationSolverOptionsContent"
                                x: window.themeTokens.spaceXs
                                width: Math.max(0, solverOptionsScroll.availableWidth
                                                   - window.themeTokens.spaceXs * 2)
                                spacing: window.themeTokens.spaceSm
                                Label {
                                    Layout.fillWidth: true
                                    text: "优化方式"
                                    color: window.themeTokens.colorText
                                    font.weight: window.themeTokens.fontWeightEmphasis
                                }
                                ThemeControls.ThemeComboBox {
                                    objectName: "filmOptimizationSearchModeBox"
                                    Layout.fillWidth: true
                                    model: window.controller.searchModeOptions
                                    textRole: "label"
                                    currentIndex: Math.max(0, window.controller.searchModeOptions.findIndex(function(item) { return item.id === window.controller.searchMode }))
                                    onActivated: {
                                        const option = window.controller.searchModeOptions[currentIndex]
                                        if (option) window.controller.setSearchMode(option.id)
                                    }
                                }
                                ColumnLayout {
                                    visible: window.controller.searchMode === "reduction"
                                    Layout.fillWidth: true
                                    Label { text: "减量目标"; color: window.themeTokens.colorText }
                                    ThemeControls.ThemeComboBox {
                                        objectName: "filmReductionModeBox"
                                        Layout.fillWidth: true
                                        model: window.controller.reductionModeOptions
                                        textRole: "label"
                                        currentIndex: Math.max(0, window.controller.reductionModeOptions.findIndex(function(item) { return item.id === window.controller.reductionMode }))
                                        onActivated: {
                                            const option = window.controller.reductionModeOptions[currentIndex]
                                            if (option) window.controller.setReductionMode(option.id)
                                        }
                                    }
                                    Label {
                                        objectName: "filmReductionPolicyLabel"
                                        Layout.fillWidth: true
                                        text: window.controller.reductionPolicySummary
                                        color: window.themeTokens.colorInfo
                                        wrapMode: Text.WordWrap
                                    }
                                }
                                Label {
                                    visible: window.controller.searchMode === "fixed_topology"
                                    Layout.fillWidth: true
                                    text: "方法"
                                    color: window.themeTokens.colorText
                                    font.weight: window.themeTokens.fontWeightEmphasis
                                }
                                ThemeControls.ThemeComboBox {
                                    id: solverMethodBox
                                    objectName: "filmOptimizationSolverBox"
                                    visible: window.controller.searchMode === "fixed_topology"
                                    Layout.fillWidth: true
                                    model: window.controller.solverOptions
                                    textRole: "label"
                                    currentIndex: window.controller.solverIndex
                                    onActivated: {
                                        const option = window.controller.solverOptions[currentIndex]
                                        if (option) window.controller.setSolver(option.id)
                                    }
                                    delegate: ItemDelegate {
                                        id: solverOptionDelegate
                                        required property int index
                                        required property var modelData
                                        width: solverMethodBox.popup.width
                                        text: modelData.label
                                        enabled: modelData.available
                                        highlighted: solverMethodBox.highlightedIndex === index
                                        Accessible.name: text
                                        ToolTip.visible: hovered && !enabled
                                        ToolTip.text: modelData.reason
                                        contentItem: ColumnLayout {
                                            spacing: window.themeTokens.spaceXs
                                            Text {
                                                Layout.fillWidth: true
                                                text: solverOptionDelegate.modelData.label
                                                color: !solverOptionDelegate.modelData.available
                                                       ? window.themeTokens.colorDisabledText
                                                       : solverMethodBox.highlightedIndex === solverOptionDelegate.index
                                                         ? window.themeTokens.colorTextOnAccent
                                                         : window.themeTokens.colorText
                                                verticalAlignment: Text.AlignVCenter
                                                elide: Text.ElideRight
                                            }
                                            Text {
                                                visible: !solverOptionDelegate.modelData.available
                                                Layout.fillWidth: true
                                                text: solverOptionDelegate.modelData.reason
                                                color: window.themeTokens.colorError
                                                wrapMode: Text.WordWrap
                                                font.pointSize: window.themeTokens.fontSizeSmall
                                            }
                                        }
                                    }
                                }
                                Label {
                                    objectName: "filmOptimizationSolverAvailability"
                                    visible: window.controller.searchMode === "fixed_topology"
                                    Layout.fillWidth: true
                                    text: window.controller.solverAvailabilityMessage
                                    color: window.controller.solverOptions[window.controller.solverIndex].available
                                           ? window.themeTokens.colorTextMuted : window.themeTokens.colorError
                                    wrapMode: Text.WordWrap
                                    font.pointSize: window.themeTokens.fontSizeSmall
                                }
                                CheckBox {
                                    objectName: "filmDeferredBatchMode"
                                    visible: window.controller.searchMode === "fixed_topology" && window.controller.showPopulationSize
                                    Layout.fillWidth: true
                                    text: "延迟批量评估（显式启用；按代更新）"
                                    checked: window.controller.deferredBatchEnabled
                                    enabled: !window.controller.busy
                                    onToggled: window.controller.setDeferredBatchEnabled(checked)
                                    ToolTip.visible: hovered
                                    ToolTip.text: "固定拓扑差分进化专用，轨迹不同于旧串行模式。Intel XPU 还需显式检测并选择设备；评分和独立验证仍使用 CPU。"
                                }
                                ColumnLayout {
                                    visible: window.controller.deferredBatchEnabled && window.controller.searchMode === "fixed_topology" && window.controller.showPopulationSize
                                    Layout.fillWidth: true
                                    enabled: !window.controller.busy && window.controller.computeTarget.deviceId === "cpu:default"
                                    Label { text: "CPU 前向线程（默认 1）"; color: window.themeTokens.colorText }
                                    ThemeControls.ThemeSpinBox {
                                        objectName: "filmBatchCpuWorkers"
                                        Layout.fillWidth: true
                                        from: 1
                                        to: window.controller.filmCpuWorkerLimit
                                        value: window.controller.filmCpuWorkers
                                        onValueModified: window.controller.setFilmCpuWorkers(value)
                                    }
                                    ThemeControls.ThemeComboBox {
                                        objectName: "filmBatchCpuAxis"
                                        Layout.fillWidth: true
                                        model: [{label: "自动分块轴", id: "auto"}, {label: "按候选分块", id: "candidates"}, {label: "按波长分块", id: "wavelengths"}]
                                        textRole: "label"
                                        currentIndex: window.controller.filmCpuAxis === "wavelengths" ? 2 : window.controller.filmCpuAxis === "candidates" ? 1 : 0
                                        onActivated: function(index) { window.controller.setFilmCpuAxis(model[index].id) }
                                    }
                                }
                                Label {
                                    visible: window.controller.deferredBatchEnabled
                                    Layout.fillWidth: true
                                    text: "线程越多未必越快；自动轴不自动寻找最佳线程数。Intel XPU 不保证加速，CPU 评分与独立验证保持不变。"
                                    wrapMode: Text.WordWrap
                                    color: window.themeTokens.colorTextMuted
                                }
                                GridLayout {
                                    visible: window.controller.searchMode === "fixed_topology" || window.controller.searchMode === "reduction"
                                    Layout.fillWidth: true
                                    columns: 2
                                    columnSpacing: window.themeTokens.spaceSm
                                    rowSpacing: window.themeTokens.spaceXs
                                    Label { text: "最大迭代数"; color: window.themeTokens.colorText }
                                    ThemeControls.ThemeField {
                                        objectName: "filmOptimizationMaxIterationsField"
                                        Layout.fillWidth: true
                                        numericDisplayValue: window.controller.maxIterations
                                        validator: RegularExpressionValidator { regularExpression: /^[1-9][0-9]*$/ }
                                        function commitDraft() {
                                            if (!numericValueEdited) return
                                            window.controller.setSolverOption("max_iterations", numericEditText)
                                            numericValueEdited = false
                                        }
                                        onEditingFinished: commitDraft()
                                    }
                                    Label { text: "最大评估数"; color: window.themeTokens.colorText }
                                    ThemeControls.ThemeField {
                                        objectName: "filmOptimizationMaxEvaluationsField"
                                        Layout.fillWidth: true
                                        numericDisplayValue: window.controller.maxEvaluations
                                        validator: RegularExpressionValidator { regularExpression: /^[1-9][0-9]*$/ }
                                        function commitDraft() {
                                            if (!numericValueEdited) return
                                            window.controller.setSolverOption("max_evaluations", numericEditText)
                                            numericValueEdited = false
                                        }
                                        onEditingFinished: commitDraft()
                                    }
                                    Label { visible: window.controller.searchMode === "fixed_topology"; text: "随机种子"; color: window.themeTokens.colorText }
                                    ThemeControls.ThemeField {
                                        objectName: "filmOptimizationSeedField"
                                        visible: window.controller.searchMode === "fixed_topology"
                                        Layout.fillWidth: true
                                        numericDisplayValue: window.controller.seed
                                        validator: DoubleValidator { bottom: 0; top: 4294967295; decimals: 0 }
                                        function commitDraft() {
                                            if (!numericValueEdited) return
                                            window.controller.setSolverOption("seed", numericEditText)
                                            numericValueEdited = false
                                        }
                                        onEditingFinished: commitDraft()
                                    }
                                    Label { text: "留出验证倍率"; color: window.themeTokens.colorText }
                                    ThemeControls.ThemeField {
                                        objectName: "filmOptimizationValidationMultiplierField"
                                        Layout.fillWidth: true
                                        numericDisplayValue: window.controller.validationSamplesMultiplier
                                        validator: RegularExpressionValidator { regularExpression: /^[1-9][0-9]*$/ }
                                        function commitDraft() {
                                            if (!numericValueEdited) return
                                            window.controller.setSolverOption("validation_samples_multiplier", numericEditText)
                                            numericValueEdited = false
                                        }
                                        onEditingFinished: commitDraft()
                                    }
                                    Label {
                                        visible: window.controller.searchMode === "fixed_topology" && window.controller.showMultistartCount
                                        text: "Powell 起点数"
                                        color: window.themeTokens.colorText
                                    }
                                    ThemeControls.ThemeField {
                                        objectName: "filmOptimizationMultistartCountField"
                                        visible: window.controller.searchMode === "fixed_topology" && window.controller.showMultistartCount
                                        Layout.fillWidth: true
                                        numericDisplayValue: window.controller.multistartCount
                                        validator: RegularExpressionValidator { regularExpression: /^[1-9][0-9]*$/ }
                                        function commitDraft() {
                                            if (!numericValueEdited) return
                                            window.controller.setSolverOption("multistart_count", numericEditText)
                                            numericValueEdited = false
                                        }
                                        onEditingFinished: commitDraft()
                                    }
                                    Label {
                                        visible: window.controller.searchMode === "fixed_topology" && window.controller.showPopulationSize
                                        text: "差分进化群体数"
                                        color: window.themeTokens.colorText
                                    }
                                    ThemeControls.ThemeField {
                                        objectName: "filmOptimizationPopulationSizeField"
                                        visible: window.controller.searchMode === "fixed_topology" && window.controller.showPopulationSize
                                        Layout.fillWidth: true
                                        numericDisplayValue: window.controller.populationSize
                                        validator: RegularExpressionValidator { regularExpression: /^[1-9][0-9]*$/ }
                                        function commitDraft() {
                                            if (!numericValueEdited) return
                                            window.controller.setSolverOption("population_size", numericEditText)
                                            numericValueEdited = false
                                        }
                                        onEditingFinished: commitDraft()
                                    }
                                }
                                Label {
                                    visible: window.controller.searchMode === "fixed_topology"
                                    Layout.fillWidth: true
                                    text: window.controller.solverParameterGuide
                                    color: window.themeTokens.colorTextMuted
                                    wrapMode: Text.WordWrap
                                    font.pointSize: window.themeTokens.fontSizeSmall
                                }
                                RowLayout {
                                    visible: window.controller.searchMode === "fixed_topology"
                                    Layout.fillWidth: true
                                    ThemeControls.ThemeButton {
                                        objectName: "filmOptimizationAlgorithmHelpButton"
                                        text: "算法说明"
                                        onClicked: Qt.openUrlExternally(window.controller.solverHelpUrl)
                                    }
                                    ThemeControls.ThemeButton {
                                        objectName: "filmOptimizationNeedleHelpButton"
                                        visible: false
                                        text: "有限针插结构接口"
                                        onClicked: Qt.openUrlExternally(window.controller.needleHelpUrl)
                                    }
                                    Item { Layout.fillWidth: true }
                                }
                                Label {
                                    visible: false
                                    Layout.fillWidth: true
                                    text: "有限针插使用独立结构搜索请求，不属于固定层序厚度方法；当前窗口的结构方式采用另一套单轮敏感度与增长搜索。"
                                    color: window.themeTokens.colorTextMuted
                                    wrapMode: Text.WordWrap
                                    font.pointSize: window.themeTokens.fontSizeSmall
                                }
                                ColumnLayout {
                                    id: synthesisOptions
                                    objectName: "filmSynthesisOptions"
                                    visible: window.controller.searchMode === "synthesis"
                                    enabled: !window.controller.busy
                                    Layout.fillWidth: true
                                    Layout.minimumWidth: 0
                                    spacing: window.themeTokens.spaceSm
                                    Label {
                                        Layout.fillWidth: true
                                        text: "从 N=0 空栈开始。当前设计只提供物理环境；结果先审计，原设计不自动改变。空预算和空约束表示未指定。"
                                        color: window.themeTokens.colorTextMuted
                                        wrapMode: Text.WordWrap
                                    }
                                    CheckBox {
                                        objectName: "filmSynthesisUseCurrentSeedField"
                                        text: "另将当前真实处方作为显式起点（仍保留 N=0 分支）"
                                        checked: window.controller.synthesisUseCurrentSeed
                                        onToggled: window.controller.setSynthesisUseCurrentSeed(checked)
                                        Accessible.name: text
                                    }
                                    CheckBox {
                                        objectName: "filmSynthesisUnknownKZeroField"
                                        text: "未知 k 按 0 计算（无损假设）"
                                        enabled: window.controller.canEditSynthesisAssumption
                                        checked: window.controller.synthesisAssumeUnknownKZero
                                        onToggled: {
                                            if (!window.controller.setSynthesisAssumeUnknownKZero(checked))
                                                checked = Qt.binding(() => window.controller.synthesisAssumeUnknownKZero)
                                        }
                                        Accessible.name: text
                                        Accessible.description: "任务运行中或输入尚有错误时不可更改；请先修正输入或等待任务结束。"
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        text: "默认严格检查基片、入射介质和候选膜料。勾选后仅把未知消光系数 k 按 0 计算；已知吸收保留，折射率 n 与有效波段仍须合法。审计结果基于此假设，不代表真实吸收已验证。"
                                        color: window.themeTokens.colorTextMuted
                                        wrapMode: Text.WordWrap
                                    }
                                    ThemeControls.ThemeComboBox {
                                        objectName: "filmSynthesisStrategyBox"
                                        Layout.fillWidth: true
                                        model: [{id: "hybrid", label: "统一混合：结构族广筛 / 多样档案 / 批量针 / 全约束精修"},
                                                {id: "legacy", label: "历史独立路由（用于兼容重放）"}]
                                        textRole: "label"
                                        currentIndex: (window.controller.synthesisOptions.search_strategy ?? "hybrid") === "hybrid" ? 0 : 1
                                        onActivated: window.controller.setSynthesisOption("search_strategy", model[currentIndex].id)
                                    }
                                    CheckBox {
                                        objectName: "filmSynthesisThicknessReductionField"
                                        visible: (window.controller.synthesisOptions.search_strategy ?? "hybrid") === "hybrid"
                                        text: "达标后缩减总厚度"
                                        checked: window.controller.synthesisOptions.enable_thickness_reduction
                                        onToggled: {
                                            if (!window.controller.setSynthesisOption("enable_thickness_reduction", checked))
                                                checked = Qt.binding(() => window.controller.synthesisOptions.enable_thickness_reduction)
                                        }
                                        Accessible.name: text
                                        Accessible.description: "默认开启，仅用于统一混合搜索；冻结到当前任务，在原预算内尝试，不放宽任何目标或制造约束。"
                                    }
                                    CheckBox {
                                        objectName: "filmSynthesisProgressPolicyToggle"
                                        text: "目标进展交接（实验，默认关闭）"
                                        visible: (window.controller.synthesisOptions.search_strategy ?? "hybrid") === "hybrid"
                                                 || window.controller.synthesisProgressPolicyEnabled
                                        checked: window.controller.synthesisProgressPolicyEnabled
                                        onToggled: {
                                            if (!window.controller.setSynthesisProgressPolicyEnabled(checked))
                                                checked = Qt.binding(() => window.controller.synthesisProgressPolicyEnabled)
                                        }
                                        Accessible.name: text
                                    }
                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        visible: (window.controller.synthesisOptions.search_strategy ?? "hybrid") === "hybrid"
                                                 || window.controller.synthesisProgressPolicyEnabled
                                        enabled: window.controller.synthesisProgressPolicyEnabled
                                        Label {
                                            Layout.fillWidth: true
                                            text: "仅以已接受步的相对进展决定交接至既有全约束精修，不放宽原目标，也不保证更优或收敛。四项均须明确填写。相对比例不是光谱百分点：0.001 = 0.1%，1 = 100%。"
                                            color: window.themeTokens.colorTextMuted
                                            wrapMode: Text.WordWrap
                                            font.pointSize: window.themeTokens.fontSizeSmall
                                        }
                                        Repeater {
                                            model: [
                                                {key: "accepted_window_steps", label: "每窗口已接受步数"},
                                                {key: "relative_sse_progress_floor", label: "SSE 相对进展下限（比例）"},
                                                {key: "relative_worst_progress_floor", label: "最坏违规相对下限（比例）"},
                                                {key: "stagnation_windows", label: "连续停滞窗口数"}
                                            ]
                                            delegate: ColumnLayout {
                                                id: progressPolicyRow
                                                required property var modelData
                                                Layout.fillWidth: true
                                                Label {
                                                    text: progressPolicyRow.modelData.label
                                                    color: window.themeTokens.colorText
                                                    Layout.fillWidth: true
                                                    wrapMode: Text.WordWrap
                                                }
                                                ThemeControls.ThemeField {
                                                    objectName: "filmSynthesisProgress_" + progressPolicyRow.modelData.key
                                                    Layout.fillWidth: true
                                                    text: window.controller.synthesisProgressPolicyDrafts[progressPolicyRow.modelData.key]
                                                    placeholderText: "须明确填写，无默认阈值"
                                                    accessibleLabel: progressPolicyRow.modelData.label
                                                    onTextEdited: window.controller.setSynthesisProgressPolicyText(progressPolicyRow.modelData.key, text)
                                                    onAccepted: window.controller.finishSynthesisProgressPolicyEdit()
                                                    onEditingFinished: window.controller.finishSynthesisProgressPolicyEdit()
                                                }
                                            }
                                        }
                                    }
                                    Label {
                                        objectName: "filmSynthesisProgressPolicyError"
                                        Layout.fillWidth: true
                                        visible: window.controller.synthesisProgressPolicyError.length > 0
                                        text: window.controller.synthesisProgressPolicyError
                                        color: window.themeTokens.colorText
                                        wrapMode: Text.WordWrap
                                    }
                                    CheckBox {
                                        objectName: "filmSynthesisRandomizeTemplateSearchField"
                                        text: "随机化模板搜索"
                                        checked: window.controller.synthesisOptions.randomize_template_search ?? false
                                        onToggled: {
                                            if (!window.controller.setSynthesisOption("randomize_template_search", checked))
                                                checked = Qt.binding(() => window.controller.synthesisOptions.randomize_template_search ?? false)
                                        }
                                        Accessible.name: text
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        Label { text: "模板搜索种子"; Layout.preferredWidth: 155 }
                                        ThemeControls.ThemeField {
                                            objectName: "filmSynthesisStructureSearchSeedField"
                                            Layout.fillWidth: true
                                            text: window.controller.synthesisStructureSearchSeedText
                                            placeholderText: "留空自动"
                                            enabled: window.controller.synthesisOptions.randomize_template_search ?? false
                                            accessibleLabel: "模板搜索种子，留空自动"
                                            accessibleDescription: "十进制整数 0 至 4294967295；关闭随机化时保留输入，正式冻结任务时生成自动种子。"
                                            inputMethodHints: Qt.ImhDigitsOnly
                                            onTextEdited: window.controller.setSynthesisStructureSearchSeedText(text)
                                            onAccepted: window.controller.finishSynthesisStructureSearchSeedEdit()
                                            onEditingFinished: window.controller.finishSynthesisStructureSearchSeedEdit()
                                        }
                                    }
                                    Label {
                                        objectName: "filmSynthesisStructureSearchSeedError"
                                        Layout.fillWidth: true
                                        visible: window.controller.synthesisStructureSearchSeedError.length > 0
                                        text: window.controller.synthesisStructureSearchSeedError
                                        color: window.themeTokens.colorText
                                        wrapMode: Text.WordWrap
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        Label { text: "实际种子"; Layout.preferredWidth: 155 }
                                        ThemeControls.ThemeField {
                                            objectName: "filmSynthesisActualStructureSearchSeedField"
                                            Layout.fillWidth: true
                                            readOnly: true
                                            text: window.controller.synthesisActualStructureSearchSeedText
                                            placeholderText: "任务冻结后显示"
                                            accessibleLabel: "模板搜索实际种子"
                                        }
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        visible: (window.controller.synthesisOptions.search_strategy ?? "hybrid") === "hybrid"
                                        Label { text: "独立候选逻辑批次"; Layout.preferredWidth: 155 }
                                        ThemeControls.ThemeField {
                                            objectName: "filmSynthesisParallelBatchSizeField"
                                            Layout.fillWidth: true
                                            accessibleLabel: "独立候选逻辑批次，留空自动"
                                            placeholderText: "自动（冻结时按请求进程和可用CPU）"
                                            preserveNumericDisplayText: true
                                            numericDisplayValue: window.controller.synthesisOptions.parallel_batch_size ?? ""
                                            function commitDraft() {
                                                if (!numericValueEdited) return
                                                const value = numericEditText
                                                numericValueEdited = false
                                                window.controller.setSynthesisOption("parallel_batch_size", value)
                                            }
                                            onAccepted: commitDraft()
                                            onEditingFinished: commitDraft()
                                        }
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        visible: (window.controller.synthesisOptions.search_strategy ?? "hybrid") === "hybrid"
                                        text: "逻辑批次不是并发保证。实际进程仍由请求、候选数、原预算与内存准入决定；独立候选和解析针扫可多核，联合厚度精修与采纳保持串行。"
                                        color: window.themeTokens.colorTextMuted
                                        wrapMode: Text.WordWrap
                                    }
                                    CheckBox {
                                        objectName: "filmSynthesisNeedlePreLocalField"
                                        text: "每次针扫描前先局部精修当前工作分支"
                                        checked: window.controller.synthesisOptions.needle_pre_local ?? false
                                        onToggled: window.controller.setSynthesisOption("needle_pre_local", checked)
                                    }
                                    Repeater {
                                        model: [{key: "needle_batch_size", label: "一次最大针根数", fallback: 6},
                                                {key: "needle_position_refinement_rounds", label: "针位置低谷细化轮数", fallback: 1},
                                                {key: "archive_capacity", label: "共享候选档案容量", fallback: 32}]
                                        delegate: RowLayout {
                                            required property var modelData
                                            Layout.fillWidth: true
                                            Label { text: parent.modelData.label; color: window.themeTokens.colorText; Layout.preferredWidth: 155 }
                                            ThemeControls.ThemeField {
                                                Layout.fillWidth: true
                                                text: String(window.controller.synthesisOptions[parent.modelData.key] ?? parent.modelData.fallback)
                                                accessibleLabel: parent.modelData.label
                                                onEditingFinished: window.controller.setSynthesisOption(parent.modelData.key, text)
                                            }
                                        }
                                    }
                                    ThemeControls.ThemeComboBox {
                                        id: synthesisResourceGuardMode
                                        objectName: "filmSynthesisResourceGuardModeBox"
                                        Layout.fillWidth: true
                                        readonly property var requestedBytes: window.controller.synthesisOptions.max_working_set_bytes
                                        // QVariantMap null is exposed as undefined; key presence
                                        // distinguishes explicit off from an omitted auto default.
                                        readonly property bool hasConfiguredBytes: Object.prototype.hasOwnProperty.call(
                                            window.controller.synthesisOptions, "max_working_set_bytes")
                                        readonly property var configuredBytes: hasConfiguredBytes ? requestedBytes : 0
                                        property double lastManualBytes: 1610612736
                                        model: [{id: "auto", label: "数值护栏：自动，冻结时可用内存的50%"},
                                                {id: "manual", label: "数值护栏：显式字节限额"},
                                                {id: "off", label: "数值护栏：关闭估计检查，仍受系统资源限制"}]
                                        textRole: "label"
                                        readonly property int configuredModeIndex: hasConfiguredBytes && requestedBytes == null ? 2 : configuredBytes === 0 ? 0 : 1
                                        currentIndex: configuredModeIndex
                                        onConfiguredBytesChanged: if (configuredBytes > 0) lastManualBytes = configuredBytes
                                        onActivated: function(index) {
                                            window.controller.setSynthesisOption("max_working_set_bytes",
                                                index === 0 ? 0 : index === 2 ? null : lastManualBytes)
                                            // A pending edit in another field can refuse this
                                            // operation. Never display a policy not committed
                                            // by the controller; restore its live binding.
                                            currentIndex = Qt.binding(function() {
                                                return synthesisResourceGuardMode.configuredModeIndex
                                            })
                                        }
                                    }
                                    RowLayout {
                                        visible: synthesisResourceGuardMode.currentIndex === 1
                                        Layout.fillWidth: true
                                        Label {
                                            text: "显式工作集限额 bytes"
                                            color: window.themeTokens.colorText
                                            Layout.preferredWidth: 155
                                        }
                                        ThemeControls.ThemeField {
                                            objectName: "filmSynthesisResourceGuardBytesField"
                                            Layout.fillWidth: true
                                            text: String(synthesisResourceGuardMode.configuredBytes ?? "")
                                            accessibleLabel: "数值工作集显式限额（字节，0为自动，清空为关闭）"
                                            inputMethodHints: Qt.ImhDigitsOnly
                                            onEditingFinished: window.controller.setSynthesisOption("max_working_set_bytes", text)
                                        }
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        text: "严格验收始终使用原始全部约束。护栏默认自动，0=自动、正整数=显式字节限额、关闭保存为null；可用内存未知时透明回退1.5GiB。它是阶段数值估计，不是层数上限或进程硬内存保证；超限会记录未执行阶段，不冒充收敛。冻结请求保存实际限额，重放不重新测量。"
                                        color: window.themeTokens.colorTextMuted
                                        wrapMode: Text.WordWrap
                                    }
                                    Label { text: "候选膜料与光学数据集"; color: window.themeTokens.colorText }
                                    Repeater {
                                        id: synthesisMaterialRows
                                        objectName: "filmSynthesisMaterialRows"
                                        model: window.controller.synthesisMaterialRows
                                        delegate: ColumnLayout {
                                            id: materialRow
                                            required property var modelData
                                            property alias materialSelector: materialSelectorBox
                                            objectName: "filmSynthesisMaterialRow_" + modelData.id
                                            Layout.maximumWidth: synthesisOptions.width
                                            Layout.fillWidth: true
                                            Layout.minimumWidth: 0
                                            RowLayout {
                                                Layout.fillWidth: true
                                                Layout.minimumWidth: 0
                                                ThemeControls.ThemeComboBox {
                                                    id: materialSelectorBox
                                                    objectName: "filmSynthesisMaterial_" + materialRow.modelData.id
                                                    Accessible.name: "候选膜料"
                                                    Layout.fillWidth: true
                                                    Layout.minimumWidth: 0
                                                    Layout.preferredWidth: 0
                                                    model: materialRow.modelData.material_options
                                                    textRole: "name"
                                                    currentIndex: model.findIndex(item => item.id === materialRow.modelData.material_id)
                                                    displayText: currentIndex < 0 ? "选择材料" : currentText
                                                    onPressedChanged: {
                                                        if (pressed) {
                                                            const identity = materialRow.modelData.id
                                                            const wasOpen = popup.visible
                                                            const revision = window.controller.synthesisMaterialCatalogRevision
                                                            window.controller.refreshSynthesisMaterials()
                                                            if (!wasOpen && revision !== window.controller.synthesisMaterialCatalogRevision)
                                                                Qt.callLater(window.openSynthesisMaterialPopup, identity)
                                                        }
                                                    }
                                                    onActivated: {
                                                        const selected = model[currentIndex]
                                                        if (selected) window.controller.setSynthesisRowMaterial(materialRow.modelData.id, selected.id)
                                                    }
                                                }
                                                ThemeControls.ThemeButton {
                                                    objectName: "filmSynthesisRemove_" + materialRow.modelData.id
                                                    Layout.minimumWidth: window.themeTokens.controlHeightMd
                                                    Layout.preferredWidth: window.themeTokens.controlHeightMd
                                                    Layout.maximumWidth: window.themeTokens.controlHeightMd
                                                    text: "×"
                                                    Accessible.name: "删除此候选材料行"
                                                    onClicked: window.controller.removeSynthesisMaterialRow(materialRow.modelData.id)
                                                }
                                            }
                                            RowLayout {
                                                Layout.fillWidth: true
                                                Layout.minimumWidth: 0
                                                ThemeControls.ThemeComboBox {
                                                    objectName: "filmSynthesisOpticalSet_" + materialRow.modelData.id
                                                    Accessible.name: "光学数据集"
                                                    Layout.fillWidth: true
                                                    Layout.minimumWidth: 0
                                                    Layout.preferredWidth: 0
                                                    model: materialRow.modelData.optical_set_options
                                                    textRole: "name"
                                                    currentIndex: model.findIndex(item => item.id === materialRow.modelData.optical_set_id)
                                                    displayText: currentIndex < 0 ? "选择数据集" : currentText
                                                    onActivated: {
                                                        const selected = model[currentIndex]
                                                        if (selected) window.controller.setSynthesisRowOpticalSet(materialRow.modelData.id, selected.id)
                                                    }
                                                }
                                                ThemeControls.ThemeButton {
                                                    objectName: "filmSynthesisUp_" + materialRow.modelData.id
                                                    Layout.minimumWidth: window.themeTokens.controlHeightMd
                                                    Layout.preferredWidth: window.themeTokens.controlHeightMd
                                                    Layout.maximumWidth: window.themeTokens.controlHeightMd
                                                    text: "↑"
                                                    Accessible.name: "上移此候选材料行"
                                                    onClicked: window.controller.moveSynthesisMaterialRow(materialRow.modelData.id, -1)
                                                }
                                                ThemeControls.ThemeButton {
                                                    objectName: "filmSynthesisDown_" + materialRow.modelData.id
                                                    Layout.minimumWidth: window.themeTokens.controlHeightMd
                                                    Layout.preferredWidth: window.themeTokens.controlHeightMd
                                                    Layout.maximumWidth: window.themeTokens.controlHeightMd
                                                    text: "↓"
                                                    Accessible.name: "下移此候选材料行"
                                                    onClicked: window.controller.moveSynthesisMaterialRow(materialRow.modelData.id, 1)
                                                }
                                            }
                                            Label {
                                                visible: materialRow.modelData.error.length > 0
                                                text: materialRow.modelData.error
                                                Layout.fillWidth: true
                                                wrapMode: Text.WordWrap
                                                color: window.themeTokens.colorWarning
                                            }
                                        }
                                    }
                                    ThemeControls.ThemeButton {
                                        objectName: "filmSynthesisAddMaterialButton"
                                        text: "＋ 添加候选材料"
                                        onClicked: window.controller.addSynthesisMaterialRow()
                                    }
                                    Label {
                                        objectName: "filmSynthesisMaterialError"
                                        Layout.fillWidth: true
                                        visible: window.controller.synthesisMaterialError.length > 0
                                        text: window.controller.synthesisMaterialError
                                        wrapMode: Text.WordWrap
                                        color: window.themeTokens.colorWarning
                                    }
                                    Repeater {
                                        model: [
                                            {key: "max_cycles", label: "最大轮次"},
                                            {key: "max_evaluations", label: "最大候选评价次数"},
                                            {key: "max_seconds", label: "最长计算时间（秒）"},
                                            {key: "max_layers", label: "最大层数"},
                                            {key: "minimum_nonzero_thickness_nm", label: "最小非零层厚 nm"},
                                            {key: "max_layer_thickness_nm", label: "最大单层厚 nm"},
                                            {key: "max_total_thickness_nm", label: "最大总厚 nm"}
                                        ]
                                        delegate: RowLayout {
                                            id: synthesisOptionRow
                                            required property var modelData
                                            Layout.fillWidth: true
                                            Label { text: synthesisOptionRow.modelData.label; color: window.themeTokens.colorText; Layout.preferredWidth: 130 }
                                            ThemeControls.ThemeField {
                                                objectName: "filmSynthesis_" + synthesisOptionRow.modelData.key + "Field"
                                                Layout.fillWidth: true
                                                text: String(window.controller.synthesisOptions[synthesisOptionRow.modelData.key] ?? "")
                                                placeholderText: "未指定"
                                                accessibleLabel: synthesisOptionRow.modelData.label
                                                onEditingFinished: window.controller.setSynthesisOption(synthesisOptionRow.modelData.key, text)
                                            }
                                        }
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        text: "CPU 工作进程数：0=自动，1=串行，2+=请求的工作进程数。此项只控制膜系合成内部任务；调度器的 CPU 选择仍独立。"
                                        color: window.themeTokens.colorTextMuted
                                        wrapMode: Text.WordWrap
                                        font.pointSize: window.themeTokens.fontSizeSmall
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        Label {
                                            text: "CPU 工作进程数"
                                            color: window.themeTokens.colorText
                                            Layout.preferredWidth: 130
                                        }
                                        ThemeControls.ThemeField {
                                            objectName: "filmSynthesisCpuWorkersField"
                                            Layout.fillWidth: true
                                            text: String(window.controller.synthesisCpuWorkers)
                                            placeholderText: "0"
                                            accessibleLabel: "CPU 工作进程数"
                                            onEditingFinished: window.controller.setSynthesisCpuWorkers(text)
                                        }
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        Label {
                                            text: "前向设备"
                                            color: window.themeTokens.colorText
                                            Layout.preferredWidth: 130
                                        }
                                        ThemeControls.ThemeComboBox {
                                            objectName: "filmSynthesisForwardBackendBox"
                                            Layout.fillWidth: true
                                            model: window.controller.synthesisForwardBackendOptions
                                            textRole: "label"
                                            currentIndex: Math.max(0, model.findIndex(
                                                item => item.id === window.controller.synthesisForwardBackend))
                                            enabled: window.controller.canEditSynthesisAssumption
                                            onActivated: {
                                                const selected = model[currentIndex]
                                                if (selected) window.controller.setSynthesisForwardBackend(selected.id)
                                            }
                                        }
                                    }
                                    RowLayout {
                                        visible: window.controller.synthesisForwardBackend !== "cpu"
                                        Layout.fillWidth: true
                                        Label {
                                            text: "GPU 设备"
                                            color: window.themeTokens.colorText
                                            Layout.preferredWidth: 130
                                        }
                                        ThemeControls.ThemeComboBox {
                                            objectName: "filmSynthesisForwardDeviceBox"
                                            Layout.fillWidth: true
                                            model: window.controller.synthesisForwardDeviceOptions
                                            textRole: "label"
                                            currentIndex: Math.max(0, model.findIndex(item =>
                                                item.binding.index === window.controller.synthesisForwardDevice.index
                                                && item.binding.uuid === window.controller.synthesisForwardDevice.uuid))
                                            enabled: window.controller.canEditSynthesisAssumption
                                            onActivated: window.controller.setSynthesisForwardDevice(currentIndex)
                                        }
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        ThemeControls.ThemeButton {
                                            objectName: "filmSynthesisForwardProbeButton"
                                            text: "重新探测 GPU"
                                            enabled: !window.controller.busy
                                            onClicked: window.controller.probeSynthesisForwardDevices()
                                        }
                                        Label {
                                            objectName: "filmSynthesisForwardProbeStatus"
                                            Layout.fillWidth: true
                                            text: window.controller.synthesisForwardStatus
                                            color: window.themeTokens.colorTextMuted
                                            wrapMode: Text.WordWrap
                                        }
                                    }
                                    Label {
                                        Layout.fillWidth: true
                                        text: "CPU 为默认前向。GPU 探测在独立子进程中运行；选定设备身份随冻结请求复核。外层任务执行设备设置保持独立。"
                                        color: window.themeTokens.colorTextMuted
                                        wrapMode: Text.WordWrap
                                        font.pointSize: window.themeTokens.fontSizeSmall
                                    }
                                }
                                ColumnLayout {
                                    objectName: "filmOptimizationStructureOptions"
                                    visible: window.controller.searchMode === "structure"
                                    Layout.fillWidth: true
                                    spacing: window.themeTokens.spaceSm
                                    Label {
                                        Layout.fillWidth: true
                                        text: "单轮有界结构合成：先检查零厚度针插敏感度，再插入有限厚度针层、增长与局部精修；实验性方法，不保证全局最优。"
                                        color: window.themeTokens.colorTextMuted
                                        wrapMode: Text.WordWrap
                                        font.pointSize: window.themeTokens.fontSizeSmall
                                    }
                                    Label { text: "结构搜索方式"; color: window.themeTokens.colorText }
                                    ThemeControls.ThemeComboBox {
                                        objectName: "filmOptimizationStructureModeBox"
                                        Layout.fillWidth: true
                                        model: window.controller.structureModeOptions
                                        textRole: "label"
                                        currentIndex: Math.max(0, window.controller.structureModeOptions.findIndex(
                                            function(item) { return item.id === window.controller.structureMode }))
                                        onActivated: {
                                            const option = window.controller.structureModeOptions[currentIndex]
                                            if (option) window.controller.setStructureMode(option.id)
                                        }
                                    }
                                    Label {
                                        visible: window.controller.structureMaterialError.length > 0
                                        Layout.fillWidth: true
                                        text: window.controller.structureMaterialError
                                        color: window.themeTokens.colorError
                                        wrapMode: Text.WordWrap
                                    }
                                    Label {
                                        visible: window.controller.structureMode !== "growth_only"
                                        Layout.fillWidth: true
                                        text: "候选膜材 / 光学常数组（请明确勾选）"
                                        color: window.themeTokens.colorText
                                        wrapMode: Text.WordWrap
                                    }
                                    ListView {
                                        objectName: "filmOptimizationStructureMaterialList"
                                        visible: window.controller.structureMode !== "growth_only"
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: 150
                                        clip: true
                                        spacing: window.themeTokens.spaceXs
                                        model: window.controller.structureMaterialOptions
                                        delegate: ThemeControls.ThemeButton {
                                            id: materialButton
                                            required property var modelData
                                            width: ListView.view.width
                                            text: modelData.display
                                            checkable: true
                                            checked: modelData.selected
                                            Accessible.name: (checked ? "已选择 " : "选择 ") + modelData.display
                                            ToolTip.visible: hovered
                                            ToolTip.delay: 600
                                            ToolTip.text: modelData.display
                                            contentItem: Text {
                                                text: materialButton.text
                                                font: materialButton.font
                                                color: !materialButton.enabled
                                                       ? window.themeTokens.colorDisabledText
                                                       : materialButton.primary || materialButton.checked
                                                         ? window.themeTokens.colorTextOnAccent
                                                         : window.themeTokens.colorText
                                                horizontalAlignment: Text.AlignHCenter
                                                verticalAlignment: Text.AlignVCenter
                                                wrapMode: Text.WordWrap
                                            }
                                            onClicked: window.controller.setStructureMaterialSelected(
                                                modelData.material_id, modelData.optical_set_id, !checked)
                                        }
                                    }
                                    Label {
                                        visible: window.controller.structureConfigurationError.length > 0
                                        Layout.fillWidth: true
                                        text: window.controller.structureConfigurationError
                                        color: window.themeTokens.colorWarning
                                        wrapMode: Text.WordWrap
                                    }
                                    GridLayout {
                                        Layout.fillWidth: true
                                        columns: 2
                                        columnSpacing: window.themeTokens.spaceSm
                                        rowSpacing: window.themeTokens.spaceXs
                                        Label { text: "总厚度上限 (nm)"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeField {
                                            objectName: "filmStructureDmaxField"
                                            Layout.fillWidth: true
                                            numericDisplayValue: window.controller.structureOptions.max_total_thickness_nm
                                            validator: DoubleValidator { bottom: 0.000001; decimals: 6 }
                                            function commitDraft() {
                                                if (!numericValueEdited) return
                                                window.controller.setStructureOption("max_total_thickness_nm", numericEditText)
                                                numericValueEdited = false
                                            }
                                            onEditingFinished: commitDraft()
                                        }
                                        Label { text: "单层厚度下限 (nm)"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeField {
                                            objectName: "filmStructureDminField"
                                            Layout.fillWidth: true
                                            numericDisplayValue: window.controller.structureOptions.min_thickness_nm
                                            validator: DoubleValidator { bottom: 0.000001; decimals: 6 }
                                            function commitDraft() {
                                                if (!numericValueEdited) return
                                                window.controller.setStructureOption("min_thickness_nm", numericEditText)
                                                numericValueEdited = false
                                            }
                                            onEditingFinished: commitDraft()
                                        }
                                        Label { text: "单层厚度上限 (nm)"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeField {
                                            objectName: "filmStructureLayerDmaxField"
                                            Layout.fillWidth: true
                                            numericDisplayValue: window.controller.structureOptions.max_thickness_nm
                                            validator: DoubleValidator { bottom: 0.000001; decimals: 6 }
                                            function commitDraft() {
                                                if (!numericValueEdited) return
                                                window.controller.setStructureOption("max_thickness_nm", numericEditText)
                                                numericValueEdited = false
                                            }
                                            onEditingFinished: commitDraft()
                                        }
                                        Label { text: "总层数预算"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeField {
                                            objectName: "filmStructureLmaxField"
                                            Layout.fillWidth: true
                                            numericDisplayValue: window.controller.structureOptions.max_total_layers
                                            validator: IntValidator { bottom: 1; top: 4096 }
                                            function commitDraft() {
                                                if (!numericValueEdited) return
                                                window.controller.setStructureOption("max_total_layers", numericEditText)
                                                numericValueEdited = false
                                            }
                                            onEditingFinished: commitDraft()
                                        }
                                        Label { text: "新增针层数"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeField {
                                            objectName: "filmStructureAddedLayersField"
                                            Layout.fillWidth: true
                                            numericDisplayValue: window.controller.structureOptions.max_added_layers
                                            validator: IntValidator { bottom: 1; top: 1 }
                                            function commitDraft() {
                                                if (!numericValueEdited) return
                                                window.controller.setStructureOption("max_added_layers", numericEditText)
                                                numericValueEdited = false
                                            }
                                            onEditingFinished: commitDraft()
                                        }
                                        Label { text: "层内位置采样数"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeField {
                                            objectName: "filmStructurePositionSamplesField"
                                            Layout.fillWidth: true
                                            numericDisplayValue: window.controller.structureOptions.position_samples
                                            validator: RegularExpressionValidator { regularExpression: /^[1-9][0-9]*$/ }
                                            function commitDraft() {
                                                if (!numericValueEdited) return
                                                window.controller.setStructureOption("position_samples", numericEditText)
                                                numericValueEdited = false
                                            }
                                            onEditingFinished: commitDraft()
                                        }
                                        Label { text: "结构候选预算"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeField {
                                            objectName: "filmStructureCandidateBudgetField"
                                            Layout.fillWidth: true
                                            numericDisplayValue: window.controller.structureOptions.candidate_budget
                                            validator: RegularExpressionValidator { regularExpression: /^[1-9][0-9]*$/ }
                                            function commitDraft() {
                                                if (!numericValueEdited) return
                                                window.controller.setStructureOption("candidate_budget", numericEditText)
                                                numericValueEdited = false
                                            }
                                            onEditingFinished: commitDraft()
                                        }
                                        Label { text: "目标评估预算"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeField {
                                            objectName: "filmStructureEvaluationBudgetField"
                                            Layout.fillWidth: true
                                            numericDisplayValue: window.controller.maxEvaluations
                                            validator: RegularExpressionValidator { regularExpression: /^([2-9]|[1-9][0-9]+)$/ }
                                            function commitDraft() {
                                                if (!numericValueEdited) return
                                                window.controller.setSolverOption("max_evaluations", numericEditText)
                                                numericValueEdited = false
                                            }
                                            onEditingFinished: commitDraft()
                                        }
                                        Label { text: "针层厚度采样数"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeField {
                                            objectName: "filmStructureNeedleThicknessSamplesField"
                                            Layout.fillWidth: true
                                            numericDisplayValue: window.controller.structureOptions.needle_thickness_samples
                                            validator: RegularExpressionValidator { regularExpression: /^[1-9][0-9]*$/ }
                                            function commitDraft() {
                                                if (!numericValueEdited) return
                                                window.controller.setStructureOption("needle_thickness_samples", numericEditText)
                                                numericValueEdited = false
                                            }
                                            onEditingFinished: commitDraft()
                                        }
                                        Label { text: "层增长循环数（本轮固定 1）"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeField {
                                            objectName: "filmStructureGrowthCyclesField"
                                            Layout.fillWidth: true
                                            enabled: false
                                            numericDisplayValue: window.controller.structureOptions.growth_cycles
                                            validator: IntValidator { bottom: 1; top: 1 }
                                        }
                                        Label { text: "随机种子"; color: window.themeTokens.colorText }
                                        ThemeControls.ThemeField {
                                            objectName: "filmStructureSeedField"
                                            Layout.fillWidth: true
                                            numericDisplayValue: window.controller.structureOptions.seed
                                            validator: DoubleValidator { bottom: 0; top: 4294967295; decimals: 0 }
                                            function commitDraft() {
                                                if (!numericValueEdited) return
                                                window.controller.setStructureOption("seed", numericEditText)
                                                numericValueEdited = false
                                            }
                                            onEditingFinished: commitDraft()
                                        }
                                    }
                                    Label {
                                        objectName: "filmOptimizationStructureParameterGuide"
                                        Layout.fillWidth: true
                                        text: "先按现有膜厚设置足够的总厚度和总层数预算，例如 60 层方案试插一层可设总层数 61；层内针插会拆分宿主层，需另预留一行。位置采样越密、针层厚度候选越多，计算量越大。候选预算限制结构尝试数，目标评估预算还包含评分、导数与验证。增长步幅如 0.5, 1, 2 nm 用于小幅增厚；相位比例按当前参考波长与折射率换算，适合相位尺度探索，不能作为制造精度承诺。"
                                        color: window.themeTokens.colorTextMuted
                                        wrapMode: Text.WordWrap
                                        font.pointSize: window.themeTokens.fontSizeSmall
                                    }
                                    Label { text: "层增长厚度步幅 (nm，逗号分隔)"; color: window.themeTokens.colorText }
                                    ThemeControls.ThemeField {
                                        objectName: "filmStructureGrowthStepsField"
                                        Layout.fillWidth: true
                                        text: window.controller.structureOptions.growth_steps_text
                                        onEditingFinished: window.controller.setStructureOption("growth_steps_nm", text)
                                    }
                                    Label { text: "光学相位增长比例 (0, 2]，逗号分隔"; color: window.themeTokens.colorText }
                                    ThemeControls.ThemeField {
                                        objectName: "filmStructureGrowthPhaseFractionsField"
                                        Layout.fillWidth: true
                                        text: window.controller.structureOptions.growth_phase_fractions_text
                                        onEditingFinished: window.controller.setStructureOption("growth_phase_fractions", text)
                                    }
                                }
                            }
                        }
                    }
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: window.themeTokens.spaceSm

                Rectangle {
                    id: plotPanel
                    objectName: "filmOptimizationPlotPanel"
                    readonly property var activeLiveData: window.controller.liveDisplay.running && window.controller.liveDisplay.enabled
                                                          ? window.controller.liveDisplay.plotData : ({})
                    readonly property bool showingLiveData: activeLiveData && activeLiveData.live === true
                    readonly property bool waitingForLive: window.controller.liveDisplay.running
                                                          && window.controller.liveDisplay.enabled && !showingLiveData
                    readonly property string previewStatus: waitingForLive
                                                            ? "等待完整预览；暂显示正式曲线。"
                                                            : (!window.controller.liveDisplay.running && window.controller.liveDisplay.hasAttempt
                                                               ? "实时预览已结束；显示正式曲线，Top 10 保留。"
                                                               : window.controller.liveDisplay.status)
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumHeight: Math.max(140, Math.min(220, window.height * 0.25))
                    radius: window.themeTokens.radiusMd
                    color: window.themeTokens.colorSurface
                    border.color: window.synthesisPreviewHighlighted ? window.themeTokens.colorAccent : window.themeTokens.colorBorder
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: window.themeTokens.spaceSm
                        spacing: window.themeTokens.spaceXs
                        RowLayout {
                            Layout.fillWidth: true
                            Label {
                                objectName: "filmOptimizationPlotTitle"
                                Layout.fillWidth: true
                                text: "光谱对比"
                                color: window.themeTokens.colorText
                                font.pointSize: window.themeTokens.fontSizeHeading
                                font.weight: window.themeTokens.fontWeightEmphasis
                            }
                            ThemeControls.ThemeComboBox {
                                objectName: "filmOptimizationPlotObjectiveBox"
                                Layout.minimumWidth: Math.min(160, plotPanel.width * 0.4)
                                Layout.preferredWidth: Math.min(260, plotPanel.width * 0.5)
                                model: window.controller.plotObjectives
                                textRole: "label"
                                valueRole: "index"
                                currentIndex: model.length > 0 ? window.controller.selectedPlotObjectiveIndex : -1
                                onActivated: window.controller.selectPlotObjective(currentIndex)
                            }
                            ThemeControls.ThemeComboBox {
                                objectName: "filmOptimizationPlotAngleBox"
                                Layout.preferredWidth: 96
                                model: window.controller.plotAngles
                                textRole: "label"
                                valueRole: "value"
                                currentIndex: model.length > 0 ? window.controller.selectedPlotAngleIndex : -1
                                onActivated: window.controller.selectPlotAngle(currentIndex)
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            CheckBox {
                                id: livePreviewToggle
                                objectName: "filmCandidateLivePreviewToggle"
                                text: "实时光谱"
                                checked: window.controller.liveDisplay.enabled
                                onToggled: window.controller.liveDisplay.setEnabled(livePreviewToggle.checked)
                                ToolTip.visible: livePreviewToggle.hovered
                                ToolTip.text: window.controller.liveDisplay.status + "\n默认关闭；只展示既有完整候选，不增加光学计算。"
                            }
                            ThemeControls.ThemeComboBox {
                                objectName: "filmCandidateLiveMetricBox"
                                Layout.preferredWidth: plotPanel.width >= 500 ? 72 : 48
                                model: ["R", "T"]
                                currentIndex: window.controller.liveDisplay.metric === "T" ? 1 : 0
                                onActivated: window.controller.liveDisplay.setMetric(model[currentIndex])
                            }
                            CheckBox {
                                objectName: "filmOptimizationYAxisAuto"
                                Layout.preferredWidth: 84
                                text: "自动轴"
                                checked: window.controller.liveDisplay.yAxisAuto
                                onToggled: {
                                    window.controller.liveDisplay.setYAxisAuto(checked)
                                    window.controller.liveDisplay.updatePlotYAxis(comparisonCanvas.dataSnapshot)
                                }
                                ToolTip.visible: hovered
                                ToolTip.text: "随当前光谱调整纵轴上限；关闭后输入百分比范围。"
                            }
                            Label {
                                objectName: "filmCandidateLivePreviewStatus"
                                Layout.fillWidth: true
                                text: plotPanel.width >= 500 ? plotPanel.previewStatus
                                      : (plotPanel.waitingForLive ? "等待预览 · 正式曲线"
                                         : (plotPanel.showingLiveData ? "实时当前 / 最佳" : "正式曲线 · Top 10 保留"))
                                color: window.themeTokens.colorTextMuted
                                wrapMode: Text.NoWrap
                                elide: Text.ElideRight
                                font.pointSize: window.themeTokens.fontSizeSmall
                            }
                        }
                        Label {
                            objectName: "filmOptimizationPlotBaselineTitle"
                            Layout.fillWidth: true
                            text: plotPanel.showingLiveData
                                  ? "R/T (%) · 当前试探与优化器最佳；仅抽样原目标网格"
                                  : "R/T (%) · " + (comparisonCanvas.dataSnapshot.baseline_label || "原设计") + "与候选；目标线按未加权界限显示"
                            color: window.themeTokens.colorTextMuted
                            font.pointSize: window.themeTokens.fontSizeSmall
                        }
                        Label {
                            objectName: "filmOptimizationPlotAnglesSummary"
                            Layout.fillWidth: true
                            visible: window.controller.plotAnglesSummary.length > 0
                            text: window.controller.plotAnglesSummary
                            color: window.themeTokens.colorTextMuted
                            wrapMode: Text.WordWrap
                            font.pointSize: window.themeTokens.fontSizeSmall
                        }
                        Label {
                            objectName: "filmSynthesisPlotPreviewStatus"
                            Layout.fillWidth: true
                            visible: window.synthesisPreviewHighlighted && window.controller.hasCandidate
                            text: "已显示任务冻结候选的目标采样点；仅针对所选目标与角度，不代表连续谱证明。"
                            color: window.themeTokens.colorAccent
                            wrapMode: Text.WordWrap
                        }
                        Canvas {
                            id: comparisonCanvas
                            objectName: "filmOptimizationComparisonCanvas"
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.minimumHeight: 90
                            property var dataSnapshot: plotPanel.showingLiveData
                                                       ? plotPanel.activeLiveData
                                                       : window.controller.plotData
                            property string seriesLegend: plotPanel.showingLiveData
                                                          ? "当前试探 / 优化器最佳" : "原设计 / 候选"
                            onDataSnapshotChanged: {
                                window.controller.liveDisplay.updatePlotYAxis(dataSnapshot)
                                requestPaint()
                            }
                            Connections {
                                target: window.controller.liveDisplay
                                function onAxisChanged() { comparisonCanvas.requestPaint() }
                            }
                            onPaint: {
                                const ctx = getContext("2d")
                                ctx.clearRect(0, 0, width, height)
                                const left = 52, right = 14, top = 18, bottom = 36
                                const plotWidth = Math.max(1, width - left - right)
                                const plotHeight = Math.max(1, height - top - bottom)
                                const rows = dataSnapshot
                                ctx.font = (window.themeTokens.fontSizeSmall || 10) + "pt sans-serif"
                                ctx.lineWidth = 1
                                ctx.strokeStyle = window.themeTokens.colorBorder
                                ctx.fillStyle = window.themeTokens.colorTextMuted
                                const yMinimum = window.controller.liveDisplay.yAxisMinimum
                                const yMaximum = window.controller.liveDisplay.yAxisMaximum
                                for (let tick = 0; tick <= 4; ++tick) {
                                    const y = top + plotHeight * tick / 4
                                    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + plotWidth, y); ctx.stroke()
                                    const value = yMaximum - (yMaximum - yMinimum) * tick / 4
                                    ctx.fillText(Number((100 * value).toPrecision(4)).toString() + "%", 2, y + 4)
                                }
                                const live = rows && rows.live === true
                                const primary = live ? (rows.current || rows.best) : null
                                const selectedWaves = live && primary ? primary.wavelength_nm : (rows ? rows.wavelength_nm : null)
                                if (!selectedWaves || selectedWaves.length === 0) {
                                    ctx.fillStyle = window.themeTokens.colorTextMuted
                                    ctx.fillText(plotPanel.waitingForLive ? "等待所选完整候选光谱" : "运行优化后显示采样光谱", left + 12, top + 24)
                                    return
                                }
                                const waves = selectedWaves
                                const discreteLine = waves.length === 1
                                const minWave = discreteLine ? waves[0] - 0.5 : waves[0]
                                const maxWave = discreteLine ? waves[0] + 0.5 : waves[waves.length - 1]
                                const xAt = value => left + (value - minWave) / Math.max(1e-12, maxWave - minWave) * plotWidth
                                const yAt = value => top + plotHeight * (1 - (value - yMinimum) / (yMaximum - yMinimum))
                                const drawTarget = value => {
                                    if (value === null || value === undefined) return
                                    if (value < yMinimum || value > yMaximum) return
                                    ctx.save(); ctx.setLineDash([5, 4]); ctx.lineWidth = 1.5
                                    ctx.strokeStyle = window.themeTokens.colorSuccess
                                    ctx.beginPath(); ctx.moveTo(left, yAt(value)); ctx.lineTo(left + plotWidth, yAt(value)); ctx.stroke()
                                    ctx.restore()
                                }
                                ctx.save(); ctx.beginPath(); ctx.rect(left, top, plotWidth, plotHeight); ctx.clip()
                                drawTarget(rows.target_value)
                                drawTarget(rows.lower_bound)
                                drawTarget(rows.upper_bound)
                                const drawSeries = (values, color, lineWidth, seriesWaves) => {
                                    const xWaves = seriesWaves || waves
                                    if (!values || values.length !== xWaves.length) return
                                    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = lineWidth
                                    for (let i = 0; i < values.length; ++i) {
                                        const x = xAt(xWaves[i]), y = yAt(values[i])
                                        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
                                    }
                                    ctx.stroke()
                                    if (discreteLine) {
                                        ctx.beginPath(); ctx.fillStyle = color
                                        ctx.arc(xAt(xWaves[0]), yAt(values[0]), 4, 0, 2 * Math.PI); ctx.fill()
                                    }
                                }
                                if (live) {
                                    if (rows.current) drawSeries(rows.current.actual, window.themeTokens.colorWarning, 1.5, rows.current.wavelength_nm)
                                    if (rows.best) drawSeries(rows.best.actual, window.themeTokens.colorAccent, 2.5, rows.best.wavelength_nm)
                                } else {
                                    drawSeries(rows.baseline, window.themeTokens.colorTextMuted, 1.5)
                                    drawSeries(rows.candidate, window.themeTokens.colorAccent, 2.5)
                                }
                                ctx.restore()
                                ctx.fillStyle = window.themeTokens.colorTextMuted
                                ctx.fillText(discreteLine ? waves[0] + " nm（离散目标）" : minWave.toFixed(0) + " nm", left, height - 8)
                                if (!discreteLine) ctx.fillText(maxWave.toFixed(0) + " nm", left + plotWidth - 52, height - 8)
                                ctx.fillStyle = live ? window.themeTokens.colorWarning : window.themeTokens.colorTextMuted
                                ctx.fillRect(left + 8, 4, 14, 3)
                                ctx.fillText(live ? "当前试探" : (rows.baseline_label || "原设计"), left + 26, 10)
                                ctx.fillStyle = window.themeTokens.colorAccent
                                ctx.fillRect(left + 90, 4, 14, 3)
                                ctx.fillText(live ? "优化器最佳" : "候选", left + 108, 10)
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            visible: !window.controller.liveDisplay.yAxisAuto
                            Label { text: "纵轴 (%)"; color: window.themeTokens.colorTextMuted }
                            ThemeControls.ThemeField {
                                id: yAxisMinimumField
                                objectName: "filmOptimizationYAxisMinimumField"
                                Layout.preferredWidth: 72
                                enabled: !window.controller.liveDisplay.yAxisAuto
                                accessibleLabel: "纵轴下限，百分比"
                                text: window.controller.liveDisplay.yAxisManualMinimumPercent
                                onEditingFinished: window.controller.liveDisplay.setYAxisBoundsPercent(text, yAxisMaximumField.text)
                            }
                            Label { text: "–"; color: window.themeTokens.colorTextMuted }
                            ThemeControls.ThemeField {
                                id: yAxisMaximumField
                                objectName: "filmOptimizationYAxisMaximumField"
                                Layout.preferredWidth: 72
                                enabled: !window.controller.liveDisplay.yAxisAuto
                                accessibleLabel: "纵轴上限，百分比"
                                text: window.controller.liveDisplay.yAxisManualMaximumPercent
                                onEditingFinished: window.controller.liveDisplay.setYAxisBoundsPercent(yAxisMinimumField.text, text)
                            }
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: window.controller.liveDisplay.axisError.length > 0
                            text: window.controller.liveDisplay.axisError
                            color: window.themeTokens.colorWarning
                            wrapMode: Text.WordWrap
                        }
                    }
                }

                Rectangle {
                    objectName: "filmOptimizationConditionErrorsPanel"
                    Layout.fillWidth: true
                    Layout.minimumHeight: 96
                    Layout.preferredHeight: Math.max(96, Math.min(176, window.height * 0.18))
                    radius: window.themeTokens.radiusMd
                    color: window.themeTokens.colorSurface
                    border.color: window.themeTokens.colorBorder
                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: window.themeTokens.spaceSm
                        spacing: window.themeTokens.spaceXs
                        Label {
                            Layout.fillWidth: true
                            text: "最大违约采样点"
                            color: window.themeTokens.colorText
                            font.weight: window.themeTokens.fontWeightEmphasis
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: window.controller.conditionErrors.length === 0 && !window.controller.liveDisplay.hasAttempt
                            objectName: "filmOptimizationConditionEmptySummary"
                            text: window.controller.conditionErrorsEmptySummary
                            color: window.themeTokens.colorTextMuted
                        }
                        ScrollView {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            ScrollBar.vertical.policy: ScrollBar.AsNeeded
                            Column {
                                width: parent.width
                                Label {
                                    objectName: "filmOptimizationBestDetails"
                                    width: parent.width
                                    visible: window.controller.liveDisplay.hasAttempt
                                    text: window.controller.liveDisplay.bestDetailSummary
                                    color: window.themeTokens.colorText
                                    font.pointSize: window.themeTokens.fontSizeSmall
                                    wrapMode: Text.WordWrap
                                }
                                Repeater {
                                    model: window.controller.conditionErrors
                                    delegate: Label {
                                        required property var modelData
                                        width: parent.width
                                        text: "目标 " + modelData.objective + " · " + modelData.wavelength + " · " + modelData.angle + " · " + modelData.polarization + " · " + modelData.metric + "=" + modelData.actual + "（" + modelData.target + "，违约 " + modelData.violation + "）"
                                        color: modelData.normalized_violation > 0 ? window.themeTokens.colorWarning : window.themeTokens.colorTextMuted
                                        elide: Text.ElideRight
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

    }

    Item {
        id: optimizationSidebar
        objectName: "filmOptimizationSidebar"
        width: window.detailsExpanded ? Math.min(404, window.width - window.themeTokens.spaceSm * 2) : 64
        z: 20
        anchors.top: parent.top
        anchors.topMargin: window.contentTopMargin + window.themeTokens.spaceMd
                           + optimizationToolbar.height + window.themeTokens.spaceSm
        anchors.right: parent.right
        anchors.rightMargin: window.themeTokens.spaceSm
        anchors.bottom: parent.bottom
        anchors.bottomMargin: window.themeTokens.spaceMd

        Rectangle {
            id: detailsRail
            objectName: "filmOptimizationSidebarRail"
            width: 64
            anchors.left: parent.left
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            color: window.themeTokens.colorSurface
            border.color: window.themeTokens.colorBorder
            radius: window.themeTokens.radiusSm
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: window.themeTokens.spaceXs
                spacing: window.themeTokens.spaceSm
                Label {
                    Layout.fillWidth: true
                    text: "优化\n详情"
                    color: window.themeTokens.colorText
                    horizontalAlignment: Text.AlignHCenter
                    font.pointSize: window.themeTokens.fontSizeBody
                }
                ThemeControls.ThemeDoubleChevron {
                    objectName: "filmOptimizationSidebarToggle"
                    Layout.alignment: Qt.AlignHCenter
                    Layout.preferredWidth: 24
                    Layout.preferredHeight: window.themeTokens.controlHeightLg
                    text: window.detailsExpanded ? "折叠优化详情" : "展开优化详情"
                    direction: window.detailsExpanded ? "right" : "left"
                    Accessible.name: text + "（F4）"
                    ToolTip.visible: hovered
                    ToolTip.text: Accessible.name
                    onClicked: window.toggleDetails()
                }
                Item { Layout.fillHeight: true }
            }
        }

        ThemeControls.ThemeSidebar {
            id: optimizationDetailsSidebar
            objectName: "filmOptimizationDetailsSidebar"
            title: "优化详情"
            sidebarWidth: optimizationSidebar.width - detailsRail.width
            visible: window.detailsExpanded
            anchors.left: detailsRail.right
            anchors.top: parent.top
            anchors.right: parent.right
            anchors.bottom: parent.bottom

            Label {
                objectName: "filmOptimizationSidebarStatus"
                Layout.fillWidth: true
                text: window.controller.statusText
                color: window.controller.error.length > 0 ? window.themeTokens.colorError
                      : window.controller.loading || window.controller.busy ? window.themeTokens.colorInfo
                      : window.themeTokens.colorText
                wrapMode: Text.WordWrap
                Accessible.name: text
            }
            Label {
                objectName: "filmOptimizationSidebarError"
                visible: window.controller.error.length > 0
                         && window.controller.statusText.indexOf(window.controller.error) < 0
                Layout.fillWidth: true
                text: window.controller.error
                color: window.themeTokens.colorError
                wrapMode: Text.WordWrap
                Accessible.name: text
            }
            Label {
                objectName: "filmOptimizationConfigurationRestoreNotice"
                visible: window.controller.configurationRestoreNotice.length > 0
                Layout.fillWidth: true
                text: window.controller.configurationRestoreNotice
                color: window.themeTokens.colorWarning
                wrapMode: Text.WrapAnywhere
                Accessible.name: text
            }
            Label {
                objectName: "filmOptimizationSidebarProgress"
                visible: window.controller.busy
                Layout.fillWidth: true
                text: window.controller.progressStatistics
                      + (window.controller.progressText !== window.controller.statusText
                         ? "\n" + window.controller.progressText : "")
                color: window.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            Label {
                objectName: "filmOptimizationSidebarCandidate"
                visible: window.controller.hasCandidate
                Layout.fillWidth: true
                text: window.controller.resultTerminalState + " · "
                      + window.controller.resultSummary + " · " + window.controller.resultMessage
                color: window.themeTokens.colorText
                wrapMode: Text.WordWrap
            }
            Label {
                objectName: "filmOptimizationValidationSummary"
                visible: window.controller.hasCandidate
                Layout.fillWidth: true
                text: window.controller.resultValidationSummary
                color: window.themeTokens.colorWarning
                wrapMode: Text.WordWrap
            }
            Label {
                objectName: "filmCandidateLiveBestSummary"
                Layout.fillWidth: true
                text: "优化器最佳（原接受门）：\n" + window.controller.liveDisplay.bestSummary
                color: window.themeTokens.colorAccent
                wrapMode: Text.WrapAnywhere
            }
            Label {
                objectName: "filmCandidateLiveCurrentSummary"
                Layout.fillWidth: true
                text: "当前完整试探：\n" + window.controller.liveDisplay.currentSummary
                color: window.themeTokens.colorTextMuted
                wrapMode: Text.WrapAnywhere
            }
            Label {
                objectName: "filmCandidateDisplayRankNotice"
                Layout.fillWidth: true
                text: window.controller.liveDisplay.rankingDiffers
                      ? "显示榜首 ≠ 优化器最佳。S 仅排序偏好，不改变 F、名义约束、guard 或应用条件。"
                      : "Top 10 为独立显示榜，不是优化器接受门；榜单无应用入口。"
                color: window.themeTokens.colorWarning
                wrapMode: Text.WordWrap
            }
            Label {
                Layout.fillWidth: true
                text: "Top 10 完整候选 · S / E / 名义 / 归一化违规 / N / D / ID / 来源 / 范围"
                color: window.themeTokens.colorText
                wrapMode: Text.WordWrap
                font.weight: window.themeTokens.fontWeightEmphasis
            }
            Repeater {
                objectName: "filmCandidateDisplayTop10"
                model: window.controller.liveDisplay.top10
                delegate: Label {
                    required property var modelData
                    required property int index
                    objectName: "filmCandidateDisplayRank_" + index
                    Layout.fillWidth: true
                    text: modelData.display
                    color: modelData.candidate_eligible ? window.themeTokens.colorText : window.themeTokens.colorWarning
                    wrapMode: Text.WrapAnywhere
                    Accessible.name: text
                }
            }
            CheckBox {
                id: scorePreferencesToggle
                objectName: "filmCandidateScorePreferencesToggle"
                text: "高级：显示评分偏好"
            }
            ColumnLayout {
                visible: scorePreferencesToggle.checked
                Layout.fillWidth: true
                Label {
                    Layout.fillWidth: true
                    text: "E 为完整原目标加权 RMS，N 含合法零厚膜层，D 为前/后全膜层物理总厚度。修改仅下一任务生效，当前榜单保留本任务冻结评分；Top 10 不是全部历史。权重和归一化锚点是显示偏好，不是工艺参数或上限。"
                    color: window.themeTokens.colorTextMuted
                    wrapMode: Text.WordWrap
                }
                Repeater {
                    model: [{key: "weight_error", label: "E 权重"},
                            {key: "weight_layers", label: "层数权重"},
                            {key: "weight_thickness", label: "厚度权重"},
                            {key: "layer_reference", label: "层数锚点（层）"},
                            {key: "thickness_reference_nm", label: "厚度锚点（nm）"}]
                    delegate: RowLayout {
                        id: scorePreferenceRow
                        required property var modelData
                        Layout.fillWidth: true
                        Label {
                            Layout.fillWidth: true
                            text: scorePreferenceRow.modelData.label
                            color: window.themeTokens.colorText
                        }
                        ThemeControls.ThemeField {
                            objectName: "filmCandidateScore_" + scorePreferenceRow.modelData.key
                            Layout.preferredWidth: 96
                            numericDisplayValue: window.controller.liveDisplay.scoreConfig[scorePreferenceRow.modelData.key]
                            onEditingFinished: window.controller.liveDisplay.setScoreField(scorePreferenceRow.modelData.key, text)
                        }
                    }
                }
                Label {
                    objectName: "filmCandidateScoreNextRunNotice"
                    Layout.fillWidth: true
                    visible: window.controller.liveDisplay.scoreChangesPending
                    text: "评分偏好已改，仅下一任务生效；当前榜单沿用本任务冻结配置。"
                    color: window.themeTokens.colorWarning
                    wrapMode: Text.WordWrap
                }
            }
            Label {
                objectName: "filmCandidateDisplayError"
                Layout.fillWidth: true
                visible: window.controller.liveDisplay.error.length > 0
                text: window.controller.liveDisplay.error
                color: window.themeTokens.colorWarning
                wrapMode: Text.WordWrap
            }
            Label {
                objectName: "filmSynthesisExecutionSummary"
                visible: window.controller.synthesisExecutionSummary.length > 0
                Layout.fillWidth: true
                text: window.controller.synthesisExecutionSummary
                wrapMode: Text.WrapAnywhere
                color: window.themeTokens.colorTextMuted
            }
            Label {
                objectName: "filmSynthesisAuditSummary"
                visible: window.controller.synthesisAuditSummary.length > 0
                Layout.fillWidth: true
                text: window.controller.synthesisAuditSummary
                wrapMode: Text.WrapAnywhere
                color: window.themeTokens.colorText
            }
            Repeater {
                objectName: "filmSynthesisObjectiveAudit"
                model: window.controller.synthesisObjectiveAudit
                delegate: Label {
                    required property var modelData
                    Layout.fillWidth: true
                    text: modelData.display
                    wrapMode: Text.WrapAnywhere
                    color: window.themeTokens.colorText
                    Accessible.name: text
                }
            }
            Label {
                objectName: "filmSynthesisAbsorptionSummary"
                visible: window.controller.synthesisAbsorptionSummary.length > 0
                Layout.fillWidth: true
                text: window.controller.synthesisAbsorptionSummary
                wrapMode: Text.WrapAnywhere
                color: window.themeTokens.colorTextMuted
            }
            Repeater {
                objectName: "filmSynthesisLayers"
                model: window.controller.synthesisLayers
                delegate: Label {
                    required property var modelData
                    Layout.fillWidth: true
                    text: modelData.display
                    wrapMode: Text.WrapAnywhere
                    color: window.themeTokens.colorText
                    Accessible.name: text
                }
            }
            Label {
                objectName: "filmOptimizationCriteriaSummary"
                visible: window.controller.objectiveCriteriaSummary.length > 0
                Layout.fillWidth: true
                text: window.controller.objectiveCriteriaSummary
                wrapMode: Text.WordWrap
                color: window.themeTokens.colorText
            }
            Label {
                objectName: "filmOptimizationRunMetadataSummary"
                visible: window.controller.hasCandidate && window.controller.searchMode === "fixed_topology"
                Layout.fillWidth: true
                text: window.controller.resultRunMetadataSummary
                color: window.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            Label {
                objectName: "filmOptimizationStructureResultSummary"
                visible: window.controller.structureResultSummary.length > 0
                Layout.fillWidth: true
                text: window.controller.structureResultSummary
                color: window.themeTokens.colorText
                wrapMode: Text.WordWrap
            }
            Label {
                visible: window.controller.structureConfigurationError.length > 0
                Layout.fillWidth: true
                text: window.controller.structureConfigurationError
                color: window.themeTokens.colorWarning
                wrapMode: Text.WordWrap
            }
            Label {
                visible: window.controller.structureMaterialError.length > 0
                Layout.fillWidth: true
                text: window.controller.structureMaterialError
                color: window.themeTokens.colorError
                wrapMode: Text.WordWrap
            }
            Label {
                Layout.fillWidth: true
                visible: window.controller.conditionErrors.length === 0
                objectName: "filmOptimizationConditionFooterEmptySummary"
                text: window.controller.conditionErrorsEmptySummary
                color: window.themeTokens.colorTextMuted
                wrapMode: Text.WordWrap
            }
            Repeater {
                model: window.controller.conditionErrors
                delegate: Label {
                    required property var modelData
                    Layout.fillWidth: true
                    text: "目标 " + modelData.objective + " · " + modelData.wavelength
                          + " · " + modelData.angle + " · " + modelData.metric
                          + "=" + modelData.actual + " · 违约 " + modelData.violation
                    color: modelData.normalized_violation > 0
                           ? window.themeTokens.colorWarning : window.themeTokens.colorTextMuted
                    wrapMode: Text.WordWrap
                }
            }
        }
    }
}
