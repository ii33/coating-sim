pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

// Local text drafts are deliberately separate from the last valid range.
GridLayout {
    id: editor
    required property var controller
    required property var sourceObjective
    required property var themeTokens
    property string objectiveId: ""
    property bool dirty: false
    columns: 2
    columnSpacing: themeTokens.spaceXs
    rowSpacing: themeTokens.spaceXs

    function syncSource() {
        if (!sourceObjective) return
        const identity = String(sourceObjective.id || "")
        if (identity === objectiveId && (dirty || minimum.activeFocus || maximum.activeFocus || precision.activeFocus)) return
        objectiveId = identity
        minimum.text = String(sourceObjective.angle_min_deg ?? 0)
        maximum.text = String(sourceObjective.angle_max_deg ?? 0)
        precision.text = String(sourceObjective.angle_step_deg ?? 1)
        dirty = false
    }
    function draftEdited() {
        dirty = true
        controller.noteObjectiveAngleDraft(minimum.text, maximum.text, precision.text)
    }
    function commitDraft(explicitConfirmation) {
        if (!dirty) return true
        if (sourceObjective.angle_sampling_mode === "points" && explicitConfirmation !== true) return false
        const accepted = controller.confirmObjectiveAngleRange(minimum.text, maximum.text, precision.text)
        if (accepted) dirty = false
        return accepted
    }
    onSourceObjectiveChanged: syncSource()
    Component.onCompleted: syncSource()

    Label { text: "角度范围 (°)"; color: editor.themeTokens.colorText }
    RowLayout {
        Layout.fillWidth: true
        spacing: editor.themeTokens.spaceXs
        ThemeControls.ThemeField {
            id: minimum
            objectName: "filmOptimizationAngleMinField"
            preserveNumericDisplayText: true
            Layout.fillWidth: true
            accessibleLabel: "入射角下限（度）"
            inputMethodHints: Qt.ImhFormattedNumbersOnly
            function commitDraft() { return editor.commitDraft() }
            onTextEdited: editor.draftEdited()
            onEditingFinished: commitDraft()
        }
        Label { text: "–"; color: editor.themeTokens.colorTextMuted }
        ThemeControls.ThemeField {
            id: maximum
            objectName: "filmOptimizationAngleMaxField"
            preserveNumericDisplayText: true
            Layout.fillWidth: true
            accessibleLabel: "入射角上限（度）"
            inputMethodHints: Qt.ImhFormattedNumbersOnly
            function commitDraft() { return editor.commitDraft() }
            onTextEdited: editor.draftEdited()
            onEditingFinished: commitDraft()
        }
    }
    Label { text: "精度 (°)"; color: editor.themeTokens.colorText }
    ThemeControls.ThemeField {
        id: precision
        objectName: "filmOptimizationAngleStepField"
        preserveNumericDisplayText: true
        Layout.fillWidth: true
        accessibleLabel: "入射角精度（度）"
        inputMethodHints: Qt.ImhFormattedNumbersOnly
        // A scientific intermediate buffer must still reach controller validation.
        Keys.onReturnPressed: event => { commitDraft(); event.accepted = true }
        Keys.onEnterPressed: event => { commitDraft(); event.accepted = true }
        onActiveFocusChanged: { if (!activeFocus && editor.dirty) editor.commitDraft() }
        function commitDraft() { return editor.commitDraft() }
        onTextEdited: editor.draftEdited()
        onEditingFinished: commitDraft()
    }
    ThemeControls.ThemeButton {
        objectName: "filmOptimizationConfirmAngleRangeButton"
        Layout.columnSpan: 2
        Layout.fillWidth: true
        visible: editor.sourceObjective.angle_sampling_mode === "points"
        text: "确认使用当前角度范围与精度"
        onClicked: {
            editor.dirty = true
            editor.commitDraft(true)
        }
    }
}
