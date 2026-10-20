pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ColumnLayout {
    id: fields
    required property var themeSnapshot
    required property var controller
    required property var value
    required property var bindings
    required property string fieldPrefix
    property string sectionKind: String(value.type || "layer")
    signal fieldEdited(string name, var value)
    spacing: fields.themeSnapshot.spaceXs
    Repeater {
        model: fields.controller.physicalSectionFields(fields.sectionKind)
        delegate: ColumnLayout {
            id: row
            required property var modelData
            Layout.fillWidth: true
            property var choices: {
                if (modelData.kind === "choice") return modelData.choices
                const list = modelData.optional ? [{id: "", label: "无"}] : []
                for (let i = 0; i < fields.bindings.length; ++i) {
                    const binding = fields.bindings[i]
                    let materialName = String(binding.material_id || "未选材料")
                    const options = fields.controller.filmOptions
                    for (let j = 0; j < options.length; ++j)
                        if (options[j].id === binding.material_id) materialName = options[j].name
                    list.push({id: binding.role,
                        label: fields.controller.physicalMaterialUseLabel(binding.role) + " · " + materialName})
                }
                return list
            }
            Label {
                Layout.fillWidth: true
                text: row.modelData.label
                color: fields.themeSnapshot.colorTextMuted
                wrapMode: Text.WordWrap
            }
            Label {
                Layout.fillWidth: true
                visible: String(row.modelData.help || "").length > 0
                text: row.modelData.help || ""
                color: fields.themeSnapshot.colorTextMuted
                wrapMode: Text.WordWrap
                font.pointSize: fields.themeSnapshot.fontSizeSmall
            }
            ThemeControls.ThemeField {
                visible: row.modelData.kind === "number" || row.modelData.kind === "integer"
                Layout.fillWidth: true
                objectName: fields.fieldPrefix + "_" + row.modelData.name
                accessibleLabel: row.modelData.label
                text: fields.value[row.modelData.name] === null || fields.value[row.modelData.name] === undefined
                      ? "" : String(fields.value[row.modelData.name])
                selectByMouse: true
                onTextEdited: fields.fieldEdited(row.modelData.name, text)
            }
            ThemeControls.ThemeComboBox {
                visible: row.modelData.kind === "role" || row.modelData.kind === "choice"
                Layout.fillWidth: true
                objectName: fields.fieldPrefix + "_" + row.modelData.name + "Choice"
                Accessible.name: row.modelData.label
                model: row.choices
                textRole: "label"
                valueRole: "id"
                currentIndex: {
                    const current = String(fields.value[row.modelData.name] || "")
                    for (let i = 0; i < row.choices.length; ++i)
                        if (row.choices[i].id === current) return i
                    return -1
                }
                onActivated: fields.fieldEdited(row.modelData.name, String(currentValue || ""))
            }
        }
    }
}
