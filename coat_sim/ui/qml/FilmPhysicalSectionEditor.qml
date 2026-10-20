pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "theme_controls" as ThemeControls

ColumnLayout {
    id: editor
    required property var themeSnapshot
    required property var controller
    required property var section
    required property var bindings
    required property string fieldPrefix
    signal sectionEdited(var value)
    spacing: editor.themeSnapshot.spaceSm
    function replaceField(name, value) {
        const copy = Object.assign({}, section)
        copy[name] = value
        sectionEdited(copy)
    }
    function nestedField(key, name, value, index) {
        const copy = Object.assign({}, section)
        if (index < 0) {
            copy[key] = Object.assign({}, section[key])
            copy[key][name] = value
        } else {
            copy[key] = section[key].slice()
            copy[key][index] = Object.assign({}, section[key][index])
            copy[key][index][name] = value
        }
        sectionEdited(copy)
    }
    function addCavity() {
        const copy = Object.assign({}, section)
        const cavity = controller.physicalSectionDefaults("layer")
        cavity.role = section.cavities.length ? section.cavities[0].role : "H"
        cavity.optical_thickness_waves = 0.5
        copy.cavities = section.cavities.concat([cavity])
        copy.coupling_mirrors = section.coupling_mirrors.concat([controller.physicalSectionDefaults("bragg")])
        sectionEdited(copy)
    }
    function removeCavity(index) {
        if (section.cavities.length <= 1) return
        const copy = Object.assign({}, section)
        copy.cavities = section.cavities.slice()
        copy.cavities.splice(index, 1)
        copy.coupling_mirrors = section.coupling_mirrors.slice()
        copy.coupling_mirrors.splice(Math.min(index, copy.coupling_mirrors.length - 1), 1)
        sectionEdited(copy)
    }
    FilmPhysicalFields {
        visible: editor.section.type !== "coupled_cavity"
        Layout.fillWidth: true
        themeSnapshot: editor.themeSnapshot
            controller: editor.controller
        value: editor.section
        bindings: editor.bindings
        fieldPrefix: editor.fieldPrefix
        onFieldEdited: (name, value) => editor.replaceField(name, value)
    }
    ColumnLayout {
        visible: editor.section.type === "coupled_cavity"
        Layout.fillWidth: true
        spacing: editor.themeSnapshot.spaceSm
        Label {
            Layout.fillWidth: true
            text: "腔层默认复用高折射率膜料；可改选低折射率膜料或按需添加独立腔层用料，不要求第三种材料。两个端镜与每个耦合镜的参数分别编辑。"
            color: editor.themeSnapshot.colorTextMuted
            wrapMode: Text.WordWrap
        }
        Label { text: "基片侧端镜（独立参数）"; color: editor.themeSnapshot.colorText }
        FilmPhysicalFields {
            Layout.fillWidth: true
            themeSnapshot: editor.themeSnapshot
            controller: editor.controller
            value: editor.section.substrate_mirror || ({type: "coupled_cavity"})
            bindings: editor.bindings
            fieldPrefix: editor.fieldPrefix + "_substrate_mirror"
            onFieldEdited: (name, value) => editor.nestedField("substrate_mirror", name, value, -1)
        }
        Repeater {
            model: (editor.section.cavities || []).length
            delegate: ColumnLayout {
                id: cavityRow
                required property int index
                property var modelData: editor.section.cavities[index]
                Layout.fillWidth: true
                Label { text: "独立腔 " + (cavityRow.index + 1); color: editor.themeSnapshot.colorText }
                FilmPhysicalFields {
                    Layout.fillWidth: true
                    themeSnapshot: editor.themeSnapshot
            controller: editor.controller
                    value: cavityRow.modelData
                    bindings: editor.bindings
                    fieldPrefix: editor.fieldPrefix + "_cavity_" + cavityRow.index
                    onFieldEdited: (name, value) => editor.nestedField("cavities", name, value, cavityRow.index)
                }
                ThemeControls.ThemeButton {
                    text: "移除此腔及相邻耦合镜"
                    enabled: editor.section.cavities.length > 1
                    onClicked: editor.removeCavity(cavityRow.index)
                }
            }
        }
        Repeater {
            model: (editor.section.coupling_mirrors || []).length
            delegate: ColumnLayout {
                id: couplingRow
                required property int index
                property var modelData: editor.section.coupling_mirrors[index]
                Layout.fillWidth: true
                Label { text: "独立耦合镜 " + (couplingRow.index + 1); color: editor.themeSnapshot.colorText }
                FilmPhysicalFields {
                    Layout.fillWidth: true
                    themeSnapshot: editor.themeSnapshot
            controller: editor.controller
                    value: couplingRow.modelData
                    bindings: editor.bindings
                    fieldPrefix: editor.fieldPrefix + "_coupling_" + couplingRow.index
                    onFieldEdited: (name, value) => editor.nestedField("coupling_mirrors", name, value, couplingRow.index)
                }
            }
        }
        ThemeControls.ThemeButton {
            objectName: editor.fieldPrefix + "_addCavity"
            text: "添加独立腔与耦合镜"
            onClicked: editor.addCavity()
        }
        Label { text: "入射侧端镜（独立参数）"; color: editor.themeSnapshot.colorText }
        FilmPhysicalFields {
            Layout.fillWidth: true
            themeSnapshot: editor.themeSnapshot
            controller: editor.controller
            value: editor.section.incident_mirror || ({type: "coupled_cavity"})
            bindings: editor.bindings
            fieldPrefix: editor.fieldPrefix + "_incident_mirror"
            onFieldEdited: (name, value) => editor.nestedField("incident_mirror", name, value, -1)
        }
    }
}
