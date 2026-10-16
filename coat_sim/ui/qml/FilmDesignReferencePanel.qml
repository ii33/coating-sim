pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ColumnLayout {
    id: panel
    objectName: "filmDesignReferencePanel"
    required property var controller
    property string selectedRecipeId: ""
    property var selectedRecipe: null
    property var recipeOptions: [{stable_id: "", title: "选择固定参考配方"}]
                         .concat(controller.builtinReferenceDesigns || [])
    property var roleBindings: ({})
    property var incidentBinding: ({material_id: "", optical_set_id: ""})
    property var substrateBinding: ({material_id: "", optical_set_id: ""})
    property bool portsTouched: false
    property bool assumeUnknownKLossless: false
    property bool mappingConfirmed: false
    property var previewData: null
    property string errorText: ""

    Layout.fillWidth: true
    Layout.minimumWidth: 0
    spacing: Theme.snapshot.spaceSm

    function indexForId(options, identity) {
        if (!options) return -1
        const wanted = String(identity || "")
        if (!wanted) return 0
        for (let i = 0; i < options.length; ++i) {
            const row = options[i]
            if (String(row.id || row.stable_id || "") === wanted) return i
        }
        return -1
    }
    function materialModel(purpose) {
        const rows = purpose === "film" ? controller.filmOptions
                    : purpose === "substrate" ? controller.substrateOptions
                    : controller.mediumOptions
        return [{id: "", name: "选择真实材料"}].concat(rows || [])
    }
    function datasetModel(purpose, materialId) {
        let rows = purpose === "film" ? controller.opticalSetsForFilm(String(materialId || ""))
                  : purpose === "substrate" ? controller.opticalSetsForSubstrate(String(materialId || ""))
                  : controller.opticalSetsFor(String(materialId || ""))
        rows = rows || []
        if (rows.length > 0 && String(rows[0].id || "") === "") return rows
        return [{id: "", name: "选择光学数据集"}].concat(rows)
    }
    function roleBinding(role) {
        return roleBindings && roleBindings[role]
               ? roleBindings[role] : {material_id: "", optical_set_id: ""}
    }
    function selectedId(options, index) {
        if (!options || index < 0 || index >= options.length) return ""
        return String(options[index].id || options[index].stable_id || "")
    }
    function displayOptionalValue(value) {
        return value === null || value === undefined ? "\u672a\u77e5" : String(value)
    }
    function prefillPortsFromCurrentDesign() {
        if (!controller.initializationReady || portsTouched) return
        const design = controller.currentDesign() || ({})
        const incident = design.incident_medium || ({})
        const substrate = design.substrate || ({})
        incidentBinding = incident.kind === "material"
                ? {material_id: String(incident.material_id || ""),
                   optical_set_id: String(incident.optical_set_id || "")}
                : {material_id: "", optical_set_id: ""}
        substrateBinding = {material_id: String(substrate.material_id || ""),
                            optical_set_id: String(substrate.optical_set_id || "")}
    }
    function clearPreview() {
        previewData = null
        errorText = ""
        controller.clearGeneratorPreview()
    }
    function changedMapping() {
        mappingConfirmed = false
        clearPreview()
    }
    function selectRecipe(identity) {
        const wanted = String(identity || "")
        const rows = controller.builtinReferenceDesigns || []
        let found = null
        for (let i = 0; i < rows.length; ++i) {
            if (String(rows[i].stable_id || "") === wanted) {
                found = rows[i]
                break
            }
        }
        selectedRecipeId = found ? wanted : ""
        selectedRecipe = found
        const emptyRoles = ({})
        if (found) {
            for (let i = 0; i < found.roles.length; ++i) {
                const role = String(found.roles[i].role || "")
                emptyRoles[role] = {material_id: "", optical_set_id: ""}
            }
        }
        roleBindings = emptyRoles
        assumeUnknownKLossless = false
        mappingConfirmed = false
        clearPreview()
    }
    function setRoleMaterial(role, materialId) {
        const next = Object.assign({}, roleBindings || {})
        next[role] = {material_id: String(materialId || ""), optical_set_id: ""}
        roleBindings = next
        changedMapping()
    }
    function setRoleDataset(role, datasetId) {
        const next = Object.assign({}, roleBindings || {})
        const current = roleBinding(role)
        next[role] = {material_id: current.material_id,
                      optical_set_id: String(datasetId || "")}
        roleBindings = next
        changedMapping()
    }
    function setPortMaterial(endpoint, materialId) {
        portsTouched = true
        const next = {material_id: String(materialId || ""), optical_set_id: ""}
        if (endpoint === "incident") incidentBinding = next
        else substrateBinding = next
        changedMapping()
    }
    function setPortDataset(endpoint, datasetId) {
        portsTouched = true
        const current = endpoint === "incident" ? incidentBinding : substrateBinding
        const next = {material_id: current.material_id,
                      optical_set_id: String(datasetId || "")}
        if (endpoint === "incident") incidentBinding = next
        else substrateBinding = next
        changedMapping()
    }
    function mappingComplete() {
        if (!selectedRecipe) return false
        for (let i = 0; i < selectedRecipe.roles.length; ++i) {
            const binding = roleBinding(String(selectedRecipe.roles[i].role || ""))
            if (!binding.material_id || !binding.optical_set_id) return false
        }
        return !!(incidentBinding.material_id && incidentBinding.optical_set_id
                  && substrateBinding.material_id && substrateBinding.optical_set_id)
    }
    function requestPreview() {
        clearPreview()
        if (!selectedRecipe || !mappingConfirmed || !mappingComplete()) {
            errorText = "请选择每个角色及两个端口的真实材料和数据集，并确认映射。"
            return
        }
        const result = controller.previewReferenceDesign({
            recipe_id: selectedRecipeId,
            role_bindings: roleBindings,
            incident_binding: incidentBinding,
            substrate_binding: substrateBinding,
            mapping_confirmed: true,
            assume_lossless: assumeUnknownKLossless
        })
        if (result && result.ok) {
            previewData = result
            errorText = ""
        } else {
            previewData = null
            errorText = result && result.error ? String(result.error) : "参考配方预览失败。"
        }
    }
    function applyPreview() {
        if (!previewData || !previewData.preview_token) return
        if (controller.applyGeneratedDesign(String(previewData.preview_token))) {
            previewData = null
            errorText = ""
        } else {
            previewData = null
            errorText = "设计状态已变化或预览已失效，请重新映射并预览。"
        }
    }

    Connections {
        target: panel.controller
        function onInitializationChanged() { panel.prefillPortsFromCurrentDesign() }
        function onGeneratorPreviewInvalidated() {
            if (panel.previewData) panel.previewData = null
        }
    }
    Component.onCompleted: prefillPortsFromCurrentDesign()

    Label {
        Layout.fillWidth: true
        text: "公开固定参考配方"
        color: Theme.snapshot.colorText
        font.pointSize: Theme.snapshot.fontSizeHeading
        font.weight: Theme.snapshot.fontWeightEmphasis
        wrapMode: Text.WordWrap
    }
    Label {
        Layout.fillWidth: true
        text: "公开示例名义层表只用于参考。映射必须选择本地真实材料/数据集；实际厚度沿用来源表，不会按映射后的 n 重新计算。"
        color: Theme.snapshot.colorTextMuted
        wrapMode: Text.WordWrap
    }
    ThemeControls.ThemeComboBox {
        objectName: "filmReferenceRecipePicker"
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        model: panel.recipeOptions
        textRole: "title"
        valueRole: "stable_id"
        currentIndex: panel.indexForId(model, panel.selectedRecipeId)
        accessibleLabel: "固定参考配方"
        onActivated: panel.selectRecipe(String(currentValue || ""))
    }
    ColumnLayout {
        visible: !!panel.selectedRecipe
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        spacing: Theme.snapshot.spaceXs
        Label {
            objectName: "filmReferenceSourceSection"
            Layout.fillWidth: true
            text: panel.selectedRecipe ? String(panel.selectedRecipe.reference_section || "") : ""
            color: Theme.snapshot.colorTextMuted
            wrapMode: Text.WordWrap
        }
        Label {
            objectName: "filmReferenceSourceMetadata"
            Layout.fillWidth: true
            text: panel.selectedRecipe
                  ? String(panel.selectedRecipe.source_title || "") + " · v" + String(panel.selectedRecipe.version || "")
                    + " · 发布日期未标注 · 核验 " + String(panel.selectedRecipe.checked_date || "")
                    + "\n" + String(panel.selectedRecipe.source_url || "")
                  : ""
            color: Theme.snapshot.colorInfo
            wrapMode: Text.WrapAnywhere
        }
        Label {
            Layout.fillWidth: true
            text: panel.selectedRecipe ? String(panel.selectedRecipe.original_material_band || "") : ""
            color: Theme.snapshot.colorTextMuted
            wrapMode: Text.WordWrap
        }
        Label {
            Layout.fillWidth: true
            text: panel.selectedRecipe ? String(panel.selectedRecipe.principle || "") : ""
            color: Theme.snapshot.colorText
            wrapMode: Text.WordWrap
        }
        Label {
            Layout.fillWidth: true
            text: panel.selectedRecipe ? String(panel.selectedRecipe.verification_status || "") : ""
            color: Theme.snapshot.colorTextMuted
            wrapMode: Text.WordWrap
        }
        Label {
            Layout.fillWidth: true
            text: "原始名义层表（入射端 → 基片）"
            color: Theme.snapshot.colorText
            font.weight: Theme.snapshot.fontWeightEmphasis
        }
        Repeater {
            objectName: "filmReferenceSourceLayerRepeater"
            model: panel.selectedRecipe ? panel.selectedRecipe.layers : []
            delegate: Label {
                required property var modelData
                objectName: "filmReferenceSourceLayer_" + String(modelData.sequence)
                Layout.fillWidth: true
                text: String(modelData.sequence) + ". " + String(modelData.role) + " — "
                      + String(modelData.nominal_material) + ", n=" + Number(modelData.nominal_n).toPrecision(6)
                      + ", k=" + Number(modelData.nominal_k).toPrecision(3)
                      + ", d=" + Number(modelData.thickness_nm).toPrecision(8) + " nm"
                color: Theme.snapshot.colorText
                wrapMode: Text.WordWrap
            }
        }
        Label {
            Layout.fillWidth: true
            text: "名义端口：" + (panel.selectedRecipe ? panel.selectedRecipe.nominal_endpoints.incident.label : "")
                  + " n=" + (panel.selectedRecipe ? panel.selectedRecipe.nominal_endpoints.incident.n : "")
                  + " → " + (panel.selectedRecipe ? panel.selectedRecipe.nominal_endpoints.substrate.label : "")
                  + " n=" + (panel.selectedRecipe ? panel.selectedRecipe.nominal_endpoints.substrate.n : "")
            color: Theme.snapshot.colorTextMuted
            wrapMode: Text.WordWrap
        }
    }

    Label {
        Layout.fillWidth: true
        text: "角色映射（每次选择配方都从空白开始）"
        color: Theme.snapshot.colorText
        font.weight: Theme.snapshot.fontWeightEmphasis
        visible: !!panel.selectedRecipe
    }
    Repeater {
        objectName: "filmReferenceRoleRepeater"
        model: panel.selectedRecipe ? panel.selectedRecipe.roles : []
        delegate: ColumnLayout {
            required property var modelData
            objectName: "filmReferenceRole_" + String(modelData.role || "")
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            spacing: Theme.snapshot.spaceXs
            Label {
                Layout.fillWidth: true
                text: String(modelData.role) + " · 名义 " + String(modelData.nominal_material)
                      + " n=" + Number(modelData.nominal_n).toPrecision(6)
                color: Theme.snapshot.colorText
                wrapMode: Text.WordWrap
            }
            ThemeControls.ThemeComboBox {
                objectName: "filmReferenceMaterial_" + String(modelData.role || "")
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                model: panel.materialModel("film")
                textRole: "name"
                valueRole: "id"
                currentIndex: panel.indexForId(model, panel.roleBinding(String(modelData.role || "")).material_id)
                accessibleLabel: String(modelData.role) + " 实际材料"
                onActivated: panel.setRoleMaterial(String(modelData.role || ""), String(currentValue || ""))
            }
            ThemeControls.ThemeComboBox {
                objectName: "filmReferenceDataset_" + String(modelData.role || "")
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                model: panel.datasetModel("film", panel.roleBinding(String(modelData.role || "")).material_id)
                textRole: "name"
                valueRole: "id"
                currentIndex: panel.indexForId(model, panel.roleBinding(String(modelData.role || "")).optical_set_id)
                accessibleLabel: String(modelData.role) + " 光学数据集"
                enabled: !!panel.roleBinding(String(modelData.role || "")).material_id
                onActivated: panel.setRoleDataset(String(modelData.role || ""), String(currentValue || ""))
            }
        }
    }

    Label {
        Layout.fillWidth: true
        text: "端口映射（当前设计若已有真实材料绑定会预填；请核对并确认。常数 n 不会转换为材料。）"
        color: Theme.snapshot.colorTextMuted
        wrapMode: Text.WordWrap
        visible: !!panel.selectedRecipe
    }
    Label { Layout.fillWidth: true; visible: !!panel.selectedRecipe; text: "入射介质"; color: Theme.snapshot.colorText }
    ThemeControls.ThemeComboBox {
        objectName: "filmReferenceIncidentMaterial"
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        visible: !!panel.selectedRecipe
        model: panel.materialModel("medium")
        textRole: "name"
        valueRole: "id"
        currentIndex: panel.indexForId(model, panel.incidentBinding.material_id)
        accessibleLabel: "参考配方入射介质材料"
        onActivated: panel.setPortMaterial("incident", String(currentValue || ""))
    }
    ThemeControls.ThemeComboBox {
        objectName: "filmReferenceIncidentDataset"
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        visible: !!panel.selectedRecipe
        model: panel.datasetModel("medium", panel.incidentBinding.material_id)
        textRole: "name"
        valueRole: "id"
        currentIndex: panel.indexForId(model, panel.incidentBinding.optical_set_id)
        accessibleLabel: "参考配方入射介质数据集"
        enabled: !!panel.incidentBinding.material_id
        onActivated: panel.setPortDataset("incident", String(currentValue || ""))
    }
    Label { Layout.fillWidth: true; visible: !!panel.selectedRecipe; text: "基片"; color: Theme.snapshot.colorText }
    ThemeControls.ThemeComboBox {
        objectName: "filmReferenceSubstrateMaterial"
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        visible: !!panel.selectedRecipe
        model: panel.materialModel("substrate")
        textRole: "name"
        valueRole: "id"
        currentIndex: panel.indexForId(model, panel.substrateBinding.material_id)
        accessibleLabel: "参考配方基片材料"
        onActivated: panel.setPortMaterial("substrate", String(currentValue || ""))
    }
    ThemeControls.ThemeComboBox {
        objectName: "filmReferenceSubstrateDataset"
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        visible: !!panel.selectedRecipe
        model: panel.datasetModel("substrate", panel.substrateBinding.material_id)
        textRole: "name"
        valueRole: "id"
        currentIndex: panel.indexForId(model, panel.substrateBinding.optical_set_id)
        accessibleLabel: "参考配方基片数据集"
        enabled: !!panel.substrateBinding.material_id
        onActivated: panel.setPortDataset("substrate", String(currentValue || ""))
    }

    CheckBox {
        id: assumeUnknownKBox
        objectName: "filmReferenceAssumeUnknownK"
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        visible: !!panel.selectedRecipe
        text: "目标波长处 k 未知（缺少 k 或 k 波段未覆盖）时，显式假设无损"
        checked: panel.assumeUnknownKLossless
        font.family: Theme.snapshot.fontFamily
        font.pointSize: Theme.snapshot.fontSizeBody
        palette.windowText: Theme.snapshot.colorText
        palette.text: Theme.snapshot.colorText
        palette.buttonText: Theme.snapshot.colorText
        palette.base: Theme.snapshot.colorSurface
        palette.highlight: Theme.snapshot.colorSelection
        contentItem: Text {
            text: assumeUnknownKBox.text
            font: assumeUnknownKBox.font
            color: Theme.snapshot.colorText
            wrapMode: Text.WordWrap
            elide: Text.ElideNone
            verticalAlignment: Text.AlignVCenter
            anchors.fill: parent
            anchors.leftMargin: assumeUnknownKBox.indicator.width + Theme.snapshot.spaceSm
        }
        onToggled: {
            if (panel.assumeUnknownKLossless !== checked) {
                panel.assumeUnknownKLossless = checked
                panel.changedMapping()
            }
        }
    }
    Label {
        Layout.fillWidth: true
        visible: !!panel.selectedRecipe
        text: "膜层与基片保留所选真实 k；已知 |k| > 1e-9 不会清零。入射端口需 k=0。来源扫描/目标区间不等于材料有效波段，请查看数据集范围。"
        color: Theme.snapshot.colorTextMuted
        wrapMode: Text.WordWrap
    }
    CheckBox {
        id: mappingConfirmedBox
        objectName: "filmReferenceMappingConfirmed"
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        visible: !!panel.selectedRecipe
        enabled: panel.mappingComplete()
        text: "我已核对上方每项实际材料、数据集与端口映射"
        checked: panel.mappingConfirmed
        font.family: Theme.snapshot.fontFamily
        font.pointSize: Theme.snapshot.fontSizeBody
        palette.windowText: Theme.snapshot.colorText
        palette.text: Theme.snapshot.colorText
        palette.buttonText: Theme.snapshot.colorText
        palette.base: Theme.snapshot.colorSurface
        palette.highlight: Theme.snapshot.colorSelection
        contentItem: Text {
            text: mappingConfirmedBox.text
            font: mappingConfirmedBox.font
            color: Theme.snapshot.colorText
            wrapMode: Text.WordWrap
            elide: Text.ElideNone
            verticalAlignment: Text.AlignVCenter
            anchors.fill: parent
            anchors.leftMargin: mappingConfirmedBox.indicator.width + Theme.snapshot.spaceSm
        }
        onToggled: {
            if (panel.mappingConfirmed !== checked) {
                panel.mappingConfirmed = checked
                panel.clearPreview()
            }
        }
    }
    ThemeControls.ThemeButton {
        objectName: "filmReferencePreview"
        Layout.fillWidth: true
        visible: !!panel.selectedRecipe
        text: "预览映射候选"
        primary: true
        enabled: controller.initializationReady && panel.mappingComplete() && panel.mappingConfirmed
        onClicked: panel.requestPreview()
    }
    Label {
        objectName: "filmReferenceError"
        Layout.fillWidth: true
        visible: panel.errorText.length > 0
        text: panel.errorText
        color: Theme.snapshot.colorError
        wrapMode: Text.WordWrap
    }

    ColumnLayout {
        objectName: "filmReferencePreviewResult"
        visible: !!panel.previewData
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        spacing: Theme.snapshot.spaceXs
        Label {
            objectName: "filmReferencePreviewHeading"
            Layout.fillWidth: true
            text: "映射预览 · 原层序固定厚度"
            color: Theme.snapshot.colorSuccess
            font.weight: Theme.snapshot.fontWeightEmphasis
        }
        Label {
            objectName: "filmReferenceMappedEndpoints"
            Layout.fillWidth: true
            text: panel.previewData
                  ? "入射：" + panel.previewData.mapped_endpoints.incident.material_name
                    + " / " + panel.previewData.mapped_endpoints.incident.optical_set_name
                    + "，n=" + Number(panel.previewData.mapped_endpoints.incident.n_at_wavelength).toPrecision(8)
                    + "，k=" + panel.displayOptionalValue(panel.previewData.mapped_endpoints.incident.k_at_wavelength)
                    + "，Δn=" + Number(panel.previewData.mapped_endpoints.incident.delta_n).toPrecision(6)
                    + "，Δk=" + panel.displayOptionalValue(panel.previewData.mapped_endpoints.incident.delta_k)
                    + "；基片：" + panel.previewData.mapped_endpoints.substrate.material_name
                    + " / " + panel.previewData.mapped_endpoints.substrate.optical_set_name
                    + "，n=" + Number(panel.previewData.mapped_endpoints.substrate.n_at_wavelength).toPrecision(8)
                    + "，k=" + panel.displayOptionalValue(panel.previewData.mapped_endpoints.substrate.k_at_wavelength)
                    + "，Δn=" + Number(panel.previewData.mapped_endpoints.substrate.delta_n).toPrecision(6)
                    + "，Δk=" + panel.displayOptionalValue(panel.previewData.mapped_endpoints.substrate.delta_k)
                  : ""
            color: Theme.snapshot.colorText
            wrapMode: Text.WordWrap
        }
        Repeater {
            model: panel.previewData ? panel.previewData.mapped_layers_incident_order : []
            delegate: Label {
                required property var modelData
                objectName: "filmReferenceMappedLayer_" + String(modelData.sequence)
                Layout.fillWidth: true
                text: String(modelData.sequence) + ". " + String(modelData.role) + " — "
                      + String(modelData.material_name) + " / " + String(modelData.optical_set_name)
                      + ", n=" + Number(modelData.n_at_wavelength).toPrecision(8)
                      + ", k=" + panel.displayOptionalValue(modelData.k_at_wavelength)
                      + ", Δn=" + Number(modelData.delta_n).toPrecision(6)
                      + ", Δk=" + panel.displayOptionalValue(modelData.delta_k)
                      + ", fixed d=" + Number(modelData.fixed_thickness_nm).toPrecision(8) + " nm"
                color: Theme.snapshot.colorText
                wrapMode: Text.WordWrap
            }
        }
        Label {
            objectName: "filmReferenceTotalThickness"
            Layout.fillWidth: true
            text: panel.previewData
                  ? "共 " + String(panel.previewData.layer_count) + " 层；物理总厚度 "
                    + Number(panel.previewData.total_physical_thickness_nm).toPrecision(10) + " nm"
                  : ""
            color: Theme.snapshot.colorText
            wrapMode: Text.WordWrap
        }
        Repeater {
            model: panel.previewData ? panel.previewData.assumptions : []
            delegate: Label {
                required property string modelData
                Layout.fillWidth: true
                text: "假设：" + modelData
                color: Theme.snapshot.colorWarning
                wrapMode: Text.WordWrap
            }
        }
        Label {
            Layout.fillWidth: true
            text: panel.previewData ? String(panel.previewData.limitations || "") : ""
            color: Theme.snapshot.colorTextMuted
            wrapMode: Text.WordWrap
        }
        RowLayout {
            Layout.fillWidth: true
            ThemeControls.ThemeButton {
                objectName: "filmReferenceCancelPreview"
                Layout.fillWidth: true
                text: "取消预览"
                onClicked: panel.clearPreview()
            }
            ThemeControls.ThemeButton {
                objectName: "filmReferenceApplyPreview"
                Layout.fillWidth: true
                text: "替换当前设计…"
                primary: true
                enabled: !!(panel.previewData && panel.previewData.preview_token)
                onClicked: replaceConfirmation.open()
            }
        }
    }

    ThemeControls.ThemeDialogBase {
        id: replaceConfirmation
        objectName: "filmReferenceReplaceConfirmation"
        title: "替换当前设计？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.Cancel
        contentItem: Label {
            text: "确认将当前膜系替换为这个已映射的参考配方吗？预览没有修改当前设计。应用后可使用本次生成撤销。"
            color: Theme.snapshot.colorText
            wrapMode: Text.WordWrap
            padding: Theme.snapshot.spaceMd
        }
        onAccepted: panel.applyPreview()
        onRejected: panel.clearPreview()
    }
}
