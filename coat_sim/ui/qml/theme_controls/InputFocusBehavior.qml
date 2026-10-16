import QtQuick

// Single-editor selection and accepted/blur policy shared by themed controls.
// Timers belong to this item, so destruction also cancels deferred work.
Item {
    id: behavior
    property var target: parent
    readonly property var focusOwner: target && target.sharedInputFocusOwner
                                      ? target.sharedInputFocusOwner : target
    signal focusSelectionApplied()
    property bool selectOnFocus: true
    property bool blurOnAccepted: true
    readonly property bool editableInput: target !== null
        && (target instanceof TextInput || target instanceof TextEdit)
        && !target.readOnly && target.enabled
    readonly property bool composing: editableInput && target.inputMethodComposing
    property bool pendingMouseSelection: false
    property var selectionTarget: null
    property var acceptedTarget: null

    anchors.fill: parent

    function requestSelection() {
        if (!editableInput || !selectOnFocus || !target.activeFocus || composing)
            return
        selectionTarget = target
        selectionTimer.restart()
    }

    function updateFocusSession() {
        selectionTimer.stop()
        selectionTarget = null
        pendingMouseSelection = false
        if (!editableInput || !selectOnFocus || !target.activeFocus)
            return
        // Mouse release performs native cursor placement. Select afterwards;
        // canceling a tap (selection/scroll drag) keeps the native selection.
        if (pointer.pressed)
            pendingMouseSelection = true
        else
            requestSelection()
    }

    onTargetChanged: updateFocusSession()
    Component.onCompleted: updateFocusSession()

    Connections {
        target: behavior.target
        ignoreUnknownSignals: true
        function onActiveFocusChanged() { behavior.updateFocusSession() }
        function onAccepted() {
            if (behavior.editableInput && behavior.blurOnAccepted
                    && !behavior.composing) {
                behavior.acceptedTarget = behavior.target
                acceptTimer.restart()
            }
        }
    }

    Timer {
        id: selectionTimer
        interval: 0
        onTriggered: {
            if (behavior.selectionTarget === behavior.target
                    && behavior.editableInput && behavior.target.activeFocus
                    && !behavior.composing) {
                behavior.target.selectAll()
                behavior.focusSelectionApplied()
            }
            behavior.selectionTarget = null
        }
    }
    Timer {
        id: acceptTimer
        interval: 0
        onTriggered: {
            const editor = behavior.acceptedTarget
            behavior.acceptedTarget = null
            if (editor && editor === behavior.target && editor.activeFocus
                    && !editor.inputMethodComposing)
                behavior.focusOwner.focus = false
        }
    }
    TapHandler {
        id: pointer
        objectName: behavior.objectName + "_pointer"
        parent: behavior.parent
        acceptedButtons: Qt.LeftButton
        gesturePolicy: TapHandler.DragThreshold
        onTapped: function(eventPoint) {
            if (behavior.pendingMouseSelection && behavior.editableInput && behavior.target.activeFocus) {
                behavior.pendingMouseSelection = false
                behavior.requestSelection()
            }
        }
        onCanceled: {
            behavior.pendingMouseSelection = false
        }
    }
}
