import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ComboBox {
    id: picker
    objectName: "projectResourceDependencyPicker"
    property var projectResources: null
    property string kind: ""
    property string pointer: ""
    property string currentReference: ""
    visible: projectResources !== null
    Layout.preferredWidth: 150
    Layout.minimumWidth: 90
    textRole: "name"
    valueRole: "id"
    currentIndex: -1
    displayText: currentIndex < 0
                 ? (currentReference.length ? "当前引用未导入" : "选择项目资源")
                 : currentText
    function refresh() {
        if (projectResources === null) return
        model = projectResources.resourcesForKind(kind)
        const identity = currentReference.length
            ? projectResources.dependencyDocumentId(kind, currentReference) : ""
        currentIndex = -1
        if (identity.length) {
            for (let index = 0; index < model.length; ++index) {
                if (String(model[index].id) === identity) {
                    currentIndex = index
                    break
                }
            }
        }
    }
    onActivated: index => {
        if (projectResources !== null && index >= 0)
            projectResources.chooseDependency(pointer, model[index].id)
    }
    onPressedChanged: { if (pressed) refresh() }
    onCurrentReferenceChanged: refresh()
    Component.onCompleted: refresh()
    Connections {
        target: picker.projectResources
        function onChanged() { picker.refresh() }
    }
}
