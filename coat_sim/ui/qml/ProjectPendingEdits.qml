import QtQuick

Item {
    id: tracker
    objectName: "projectPendingEditsTracker"
    property var editorWindow: null
    width: 0
    height: 0
    visible: false

    Connections {
        target: tracker.editorWindow ? tracker.editorWindow.activeFocusItem : null
        ignoreUnknownSignals: true
        function onTextEdited() {
            if (tracker.editorWindow)
                tracker.editorWindow.projectPendingTextEdited = true
        }
        function onEditingFinished() {
            if (tracker.editorWindow)
                tracker.editorWindow.projectPendingTextEdited = false
        }
    }

    Connections {
        target: tracker.editorWindow
        ignoreUnknownSignals: true
        function onActiveFocusItemChanged() {
            if (tracker.editorWindow)
                tracker.editorWindow.projectPendingTextEdited = false
        }
    }
}
