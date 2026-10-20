pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "theme_controls" as ThemeControls

Item {
    id: form
    objectName: "filmPhysicalGeneratorForm"
    required property var themeSnapshot
    required property var controller
    property string mechanism: "multistack"
    property var sections: []
    property var bindings: []
    property var mechanismDrafts: ({})
    property var previewData: ({})
    property string previewToken: ""
    property string errorText: ""
    property string addSectionType: "layer"
    property string validationWavelengthsText: ""
    property string newRoleText: ""
    property bool defaultsLoaded: false
    property var rememberedFocus: null
    property var sectionTypes: [
        {id: "layer", title: "独立匹配 / 阻断层"}, {id: "bragg", title: "Bragg 膜堆"},
        {id: "coupled_cavity", title: "多腔 / 耦合镜"}, {id: "chirped", title: "局部 Bragg 与比例啁啾"},
        {id: "digital_rugate", title: "真实 H/L 数字 rugate"}]
    function clone(value) { return JSON.parse(JSON.stringify(value)) }
    function indexFor(rows, id) {
        for (let i = 0; i < rows.length; ++i) if (String(rows[i].id) === String(id)) return i
        return -1
    }
    function invalidate() {
        previewData = ({})
        previewToken = ""
        errorText = ""
        controller.clearGeneratorPreview()
    }
    function selectMechanism(id) {
        if (mechanism === id && sections.length) return
        const drafts = Object.assign({}, mechanismDrafts)
        drafts[mechanism] = clone(sections)
        mechanismDrafts = drafts
        mechanism = id
        const catalog = controller.physicalStructureCatalog
        const selected = catalog[indexFor(catalog, id)]
        sections = drafts[id] ? clone(drafts[id]) : [controller.physicalSectionDefaults(selected.section_type)]
        invalidate()
    }
    function setBinding(role, materialId, opticalSetId) {
        const copy = bindings.slice()
        let found = -1
        for (let i = 0; i < copy.length; ++i) if (copy[i].role === role) found = i
        const value = {role: role, material_id: String(materialId), optical_set_id: String(opticalSetId)}
        if (found < 0) copy.push(value)
        else copy[found] = value
        bindings = copy
        invalidate()
    }
    function chooseMaterial(role, materialId) {
        const sets = controller.opticalSetsForFilm(materialId)
        setBinding(role, materialId, sets.length ? sets[0].id : "")
    }
    function addRole() {
        const role = newRoleText.trim()
        if (!role) return
        for (let i = 0; i < bindings.length; ++i) if (bindings[i].role === role) return
        const options = controller.filmOptions
        chooseMaterial(role, options.length ? options[0].id : "")
        newRoleText = ""
    }
    function setSection(index, value) {
        const copy = sections.slice()
        copy[index] = value
        sections = copy
        invalidate()
    }
    function setSectionField(index, name, value) {
        const copy = Object.assign({}, sections[index])
        copy[name] = value
        setSection(index, copy)
    }
    function moveSection(index, delta) {
        const destination = index + delta
        if (destination < 0 || destination >= sections.length) return
        const copy = sections.slice()
        const row = copy.splice(index, 1)[0]
        copy.splice(destination, 0, row)
        sections = copy
        invalidate()
    }
    function syncDefaults() {
        if (defaultsLoaded || !controller.initializationReady) return
        const options = controller.filmOptions
        const roles = ["H", "L"]
        for (let i = 0; i < roles.length; ++i)
            chooseMaterial(roles[i], options.length ? options[0].id : "")
        sections = [controller.physicalSectionDefaults("bragg")]
        defaultsLoaded = true
    }
    function requiredBindings() {
        const required = controller.physicalRequiredMaterialUses(sections)
        const selected = []
        for (let i = 0; i < required.length; ++i) {
            let binding = null
            for (let j = 0; j < bindings.length; ++j)
                if (bindings[j].role === required[i]) binding = bindings[j]
            if (!binding) throw new Error("当前结构需要“" + controller.physicalMaterialUseLabel(required[i])
                + "”；请先添加该用料，或在对应层选择已有膜料。各用途可选同一材料。")
            selected.push(clone(binding))
        }
        return selected
    }
    function requestPreview() {
        invalidate()
        let selected
        try { selected = requiredBindings() }
        catch (error) { errorText = String(error.message || error); return }
        const result = controller.previewPhysicalStructure({mechanism: mechanism,
            bindings: selected, sections: clone(sections), name: "physical structure",
            validation_wavelengths_nm: validationWavelengthsText.trim()
                ? validationWavelengthsText.trim().split(/[,;\s]+/).map(Number) : []})
        if (!result || !result.ok) {
            errorText = result && result.error ? String(result.error) : "无法生成物理结构预览。"
            return
        }
        previewData = result
        previewToken = String(result.preview_token || "")
    }
    function applyPreview() {
        if (!previewToken) return
        const token = previewToken
        if (!controller.applyGeneratedDesign(token)) {
            invalidate()
            errorText = "预览已过期或材料校验失败；原设计未替换，请重新预览。"
            return
        }
        previewData = ({})
        previewToken = ""
        errorText = "已替换当前未保存草稿；可在其他编辑前撤销。"
    }
    Connections {
        target: form.controller
        function onInitializationChanged() { form.syncDefaults() }
        function onGeneratorPreviewInvalidated() { form.previewData = ({}); form.previewToken = "" }
    }
    Component.onCompleted: syncDefaults()
    ScrollView {
        id: scroll
        objectName: "filmPhysicalScroll"
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ScrollBar.vertical.policy: ScrollBar.AlwaysOn
        ColumnLayout {
            width: scroll.availableWidth
            spacing: form.themeSnapshot.spaceSm
            Label {
                Layout.fillWidth: true
                text: "使用当前设计的入射介质、基片及参考波长。仅替换正面层序；已知 k 严格校验，不外推材料数据。生成不自动优化。"
                wrapMode: Text.WordWrap
                color: form.themeSnapshot.colorTextMuted
            }
            ThemeControls.ThemeComboBox {
                objectName: "filmPhysicalMechanism"
                Layout.fillWidth: true
                model: form.controller.physicalStructureCatalog
                textRole: "title"
                valueRole: "id"
                currentIndex: form.indexFor(model, form.mechanism)
                onActivated: form.selectMechanism(String(currentValue))
            }
            Label { text: "附加材料校验波长 (nm，以空格或逗号分隔，可留空)"; color: form.themeSnapshot.colorTextMuted }
            ThemeControls.ThemeField {
                objectName: "filmPhysicalValidationWavelengths"
                Layout.fillWidth: true
                text: form.validationWavelengthsText
                accessibleLabel: "附加材料校验波长 nm"
                onTextEdited: { form.validationWavelengthsText = text; form.invalidate() }
            }
            Label { text: "膜层材料与光学数据集"; color: form.themeSnapshot.colorText }
            Label {
                visible: form.controller.initializationReady && form.controller.filmOptions.length === 0
                Layout.fillWidth: true
                text: "没有可用膜层材料，请先在当前工程加入光学材料。"
                wrapMode: Text.WordWrap
                color: form.themeSnapshot.colorWarning
            }
            Label {
                objectName: "filmPhysicalMaterialHelp"
                Layout.fillWidth: true
                text: "常用交替膜堆只需高、低折射率两种膜料。腔层和匹配层可复用已有材料；用途代号不是不同材料的数量要求。核心按实际 n 检查高折射率层大于低折射率层。"
                wrapMode: Text.WordWrap
                color: form.themeSnapshot.colorTextMuted
            }
            Repeater {
                model: form.bindings.length
                delegate: ColumnLayout {
                    id: bindingRow
                    required property int index
                    property var binding: form.bindings[index]
                    Layout.fillWidth: true
                    Label { text: form.controller.physicalMaterialUseLabel(bindingRow.binding.role); color: form.themeSnapshot.colorTextMuted }
                    ThemeControls.ThemeMaterialPicker {
                        objectName: "filmPhysicalMaterial_" + bindingRow.binding.role
                        Layout.fillWidth: true
                        model: form.controller.filmOptions
                        textRole: "name"
                        valueRole: "id"
                        currentIndex: form.indexFor(model, bindingRow.binding.material_id)
                        accessibleLabel: form.controller.physicalMaterialUseLabel(bindingRow.binding.role) + "材料选择"
                        onActivated: (selected) => form.chooseMaterial(bindingRow.binding.role, String(model[selected].id))
                    }
                    ThemeControls.ThemeComboBox {
                        objectName: "filmPhysicalDataset_" + bindingRow.binding.role
                        Accessible.name: form.controller.physicalMaterialUseLabel(bindingRow.binding.role) + "光学数据集"
                        Layout.fillWidth: true
                        model: form.controller.opticalSetsForFilm(bindingRow.binding.material_id)
                        textRole: "name"
                        valueRole: "id"
                        currentIndex: form.indexFor(model, bindingRow.binding.optical_set_id)
                        onActivated: form.setBinding(bindingRow.binding.role, bindingRow.binding.material_id, String(currentValue))
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeField {
                    objectName: "filmPhysicalNewRole"
                    Layout.fillWidth: true
                    text: form.newRoleText
                    placeholderText: "按需添加用料代号，例如 M1"
                    accessibleLabel: "新增独立用料代号（可选）"
                    onTextEdited: form.newRoleText = text
                }
                ThemeControls.ThemeButton { objectName: "filmPhysicalAddMaterialUse"; text: "添加独立用料"; onClicked: form.addRole() }
            }
            Label {
                Layout.fillWidth: true
                text: "需要单独指定腔层或匹配层时，才添加额外用料。不同用途允许选择同一材料和同一数据集。波长/物理厚度单位为 nm，1 μm = 1000 nm。"
                wrapMode: Text.WordWrap
                color: form.themeSnapshot.colorTextMuted
            }
            Label {
                text: "结构段：基片 → 入射侧"
                color: form.themeSnapshot.colorText
                font.weight: form.themeSnapshot.fontWeightEmphasis
            }
            Repeater {
                model: form.sections.length
                delegate: Frame {
                    id: sectionRow
                    required property int index
                    Layout.fillWidth: true
                    padding: form.themeSnapshot.spaceSm
                    background: Rectangle { color: form.themeSnapshot.colorSurfaceRaised; radius: form.themeSnapshot.radiusSm }
                    ColumnLayout {
                        width: parent.width
                        spacing: form.themeSnapshot.spaceSm
                        Label { text: "段 " + (sectionRow.index + 1) + " · " + form.sections[sectionRow.index].type; color: form.themeSnapshot.colorText }
                        RowLayout {
                            ThemeControls.ThemeButton { text: "上移"; enabled: sectionRow.index > 0; onClicked: form.moveSection(sectionRow.index, -1) }
                            ThemeControls.ThemeButton { text: "下移"; enabled: sectionRow.index + 1 < form.sections.length; onClicked: form.moveSection(sectionRow.index, 1) }
                            ThemeControls.ThemeButton {
                                text: "移除"
                                onClicked: {
                                    const copy = form.sections.slice()
                                    copy.splice(sectionRow.index, 1)
                                    form.sections = copy
                                    form.invalidate()
                                }
                            }
                        }
                        FilmPhysicalSectionEditor {
                            Layout.fillWidth: true
                            themeSnapshot: form.themeSnapshot
                            controller: form.controller
                            section: form.sections[sectionRow.index]
                            bindings: form.bindings
                            fieldPrefix: "filmPhysicalSection_" + sectionRow.index
                            onSectionEdited: (value) => form.setSection(sectionRow.index, value)
                        }
                    }
                }
            }
            ThemeControls.ThemeComboBox {
                objectName: "filmPhysicalAddSectionType"
                Layout.fillWidth: true
                model: form.sectionTypes
                textRole: "title"
                valueRole: "id"
                currentIndex: form.indexFor(model, form.addSectionType)
                onActivated: form.addSectionType = String(currentValue)
            }
            ThemeControls.ThemeButton {
                objectName: "filmPhysicalAddSection"
                text: "添加结构段"
                onClicked: {
                    form.sections = form.sections.concat([form.controller.physicalSectionDefaults(form.addSectionType)])
                    form.invalidate()
                }
            }
            ThemeControls.ThemeButton {
                objectName: "filmPhysicalPreview"
                Layout.fillWidth: true
                text: "预览真实逐层处方"
                primary: true
                enabled: form.controller.initializationReady && form.controller.filmOptions.length > 0
                onClicked: form.requestPreview()
            }
            Label {
                objectName: "filmPhysicalError"
                Layout.fillWidth: true
                visible: form.errorText.length > 0
                text: form.errorText
                wrapMode: Text.WordWrap
                color: form.themeSnapshot.colorWarning
            }
            ColumnLayout {
                Layout.fillWidth: true
                visible: form.previewToken.length > 0
                Label {
                    Layout.fillWidth: true
                    text: "真实单层 " + form.previewData.layer_count + " 层；总厚 " + Number(form.previewData.total_physical_thickness_nm).toPrecision(8) + " nm"
                    color: form.themeSnapshot.colorText
                    wrapMode: Text.WordWrap
                }
                Label {
                    Layout.fillWidth: true
                    text: form.previewData.limitations || ""
                    color: form.themeSnapshot.colorTextMuted
                    wrapMode: Text.WordWrap
                }
                Repeater {
                    model: form.previewData.layers_substrate_order || []
                    delegate: Label {
                        required property var modelData
                        Layout.fillWidth: true
                        text: modelData.sequence + ". " + modelData.role + " · " + modelData.material_id + " / " + modelData.optical_set_id + " · " + Number(modelData.physical_thickness_nm).toPrecision(8) + " nm"
                        color: form.themeSnapshot.colorTextMuted
                        wrapMode: Text.WordWrap
                    }
                }
                ThemeControls.ThemeButton {
                    objectName: "filmPhysicalApply"
                    Layout.fillWidth: true
                    text: "确认替换当前草稿…"
                    primary: true
                    onClicked: confirmation.open()
                }
            }
        }
    }
    ThemeControls.ThemeDialogBase {
        id: confirmation
        objectName: "filmPhysicalApplyConfirmation"
        title: "替换当前膜系设计？"
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: form.applyPreview()
        onRejected: form.invalidate()
        contentItem: Label {
            text: "将替换当前正面层序并标记为未保存，保留当前文件路径。应用后可在其他编辑发生前撤销一次。"
            wrapMode: Text.WordWrap
            color: form.themeSnapshot.colorText
        }
    }
}
