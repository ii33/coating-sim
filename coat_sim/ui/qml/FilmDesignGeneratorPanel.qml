pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "theme_controls" as ThemeControls

Item {
    id: panel
    required property var controller
    property string templateId: "ar_single_qw"
    property var roleBindings: ({})
    property var substrateBinding: ({})
    property var incidentBinding: ({})
    property string wavelengthText: "550"
    property string pairsText: "8"
    property bool assumeUnknownKLossless: false
    property var previewData: ({})
    property string previewToken: ""
    property string errorText: ""
    property bool defaultsLoaded: false
    property string boundDesignId: ""

    function indexFor(options, id) {
        if (!options) return -1
        for (let i = 0; i < options.length; ++i)
            if (String(options[i].id || options[i].stable_id || "") === String(id || "")) return i
        return -1
    }
    function selectedOptionId(options, index) {
        if (!options || index < 0 || index >= options.length) return ""
        return String(options[index].id || "")
    }
    function opticalSetId(options, id) {
        if (!options || options.length === 0) return ""
        if (indexFor(options, id) >= 0) return String(id)
        return String(options[0].id || "")
    }
    function firstMaterialBinding(options, materialId, setId, purpose) {
        let material = null
        for (let i = 0; i < options.length; ++i) {
            if (String(options[i].id || "") === String(materialId || "")) {
                material = options[i]
                break
            }
        }
        if (!material && options.length) material = options[0]
        if (!material) return {material_id: "", optical_set_id: ""}
        const sets = purpose === "film" ? controller.opticalSetsForFilm(String(material.id || ""))
                    : purpose === "substrate" ? controller.opticalSetsForSubstrate(String(material.id || ""))
                    : controller.opticalSetsFor(String(material.id || ""))
        let chosen = String(setId || "")
        if (indexFor(sets, chosen) < 0) {
            chosen = String(material.preferred_optical_set_id || material.default_optical_set_id || "")
            if (indexFor(sets, chosen) < 0) chosen = sets.length ? String(sets[0].id || "") : ""
        }
        return {material_id: String(material.id || ""), optical_set_id: chosen}
    }
    function roleBinding(role) {
        if (roleBindings && roleBindings[role]) return roleBindings[role]
        if (role === "C" && roleBindings && roleBindings.H) return roleBindings.H
        return {material_id: "", optical_set_id: ""}
    }
    function roleSetModel(role) {
        return controller.opticalSetsForFilm(roleBinding(role).material_id)
    }
    function clearPreview() {
        previewData = ({})
        previewToken = ""
        errorText = ""
        controller.clearGeneratorPreview()
    }
    function changedInput() {
        clearPreview()
    }
    function setRoleMaterial(role, materialId) {
        const current = roleBinding(role)
        const next = firstMaterialBinding(controller.filmOptions, materialId,
                                           current.optical_set_id, "film")
        const copy = Object.assign({}, roleBindings || {})
        copy[role] = next
        roleBindings = copy
        changedInput()
    }
    function setRoleOpticalSet(role, setId) {
        const copy = Object.assign({}, roleBindings || {})
        const current = roleBinding(role)
        copy[role] = {material_id: current.material_id, optical_set_id: String(setId || "")}
        roleBindings = copy
        changedInput()
    }
    function setEndpointMaterial(endpoint, purpose, materialId) {
        const current = endpoint === "substrate" ? substrateBinding : incidentBinding
        const options = purpose === "substrate" ? controller.substrateOptions : controller.mediumOptions
        const binding = firstMaterialBinding(options, materialId, current.optical_set_id, purpose)
        if (endpoint === "substrate") substrateBinding = binding
        else incidentBinding = binding
        changedInput()
    }
    function setEndpointOpticalSet(endpoint, setId) {
        if (endpoint === "substrate")
            substrateBinding = {material_id: substrateBinding.material_id, optical_set_id: String(setId || "")}
        else
            incidentBinding = {material_id: incidentBinding.material_id, optical_set_id: String(setId || "")}
        changedInput()
    }
    function syncDefaults() {
        if (!controller.initializationReady) return
        const meta = controller.designMeta || ({})
        boundDesignId = String(meta.design_id || "")
        const incident = meta.incident_medium || ({})
        const substrate = meta.substrate || ({})
        incidentBinding = firstMaterialBinding(controller.mediumOptions,
            incident.kind === "material" ? incident.material_id : "",
            incident.kind === "material" ? incident.optical_set_id : "", "medium")
        substrateBinding = firstMaterialBinding(controller.substrateOptions,
            substrate.material_id || "", substrate.optical_set_id || "", "substrate")
        wavelengthText = String(meta.reference_wavelength_nm || 550)
        const selected = selectedTemplate()
        pairsText = String(selected.default_pairs || 8)
        const copy = Object.assign({}, roleBindings || {})
        const roles = controller.generatorRoles(templateId)
        for (let i = 0; i < roles.length; ++i) {
            const role = String(roles[i].role || "")
            if (role === "C" && !copy.C) continue
            const old = copy[role]
            copy[role] = firstMaterialBinding(controller.filmOptions,
                old ? old.material_id : "", old ? old.optical_set_id : "", "film")
        }
        roleBindings = copy
        defaultsLoaded = true
    }
    function selectedTemplate() {
        const rows = controller.builtinDesignTemplates
        for (let i = 0; i < rows.length; ++i)
            if (String(rows[i].stable_id || "") === templateId) return rows[i]
        return rows.length ? rows[0] : ({})
    }
    function referenceResultsText() {
        const values = panel.previewData.reference_results || ({})
        const parts = []
        if (values.center_reflectance !== undefined)
            parts.push("R₀=" + Number(values.center_reflectance).toPrecision(8))
        if (values.center_transmittance !== undefined)
            parts.push("T₀=" + Number(values.center_transmittance).toPrecision(8))
        if (values.ideal_match_ratio_n2_over_n1 !== undefined)
            parts.push("理想 n₂/n₁=" + Number(values.ideal_match_ratio_n2_over_n1).toPrecision(8))
        if (values.middle_half_wave_matrix_is_minus_identity)
            parts.push("中间半波层中心矩阵 = −I")
        if (values.ideal_unit_transmission !== undefined)
            parts.push(values.ideal_unit_transmission ? "满足单位透射条件 n₀=ns" : "当前边界不满足单位透射条件")
        return parts.join("；")
    }
    function changeTemplate(id) {
        templateId = String(id || "")
        const entry = selectedTemplate()
        pairsText = String(entry.default_pairs || 8)
        const copy = Object.assign({}, roleBindings || {})
        const roles = controller.generatorRoles(templateId)
        for (let i = 0; i < roles.length; ++i) {
            const role = String(roles[i].role || "")
            if (role === "C" && !copy.C) continue
            if (!copy[role]) copy[role] = firstMaterialBinding(controller.filmOptions, "", "", "film")
        }
        roleBindings = copy
        changedInput()
    }
    function requestPreview() {
        const selected = selectedTemplate()
        const roles = controller.generatorRoles(templateId)
        const bindings = ({})
        for (let i = 0; i < roles.length; ++i) {
            const role = String(roles[i].role || "")
            if (role === "C" && !roleBindings.C) continue
            bindings[role] = roleBinding(role)
        }
        const inputs = {
            template_id: templateId,
            wavelength_nm: Number(wavelengthText),
            pairs: selected.default_pairs === null || selected.default_pairs === undefined
                    ? 0 : Number(pairsText),
            assume_lossless: assumeUnknownKLossless,
            role_bindings: bindings,
            incident_binding: incidentBinding,
            substrate_binding: substrateBinding
        }
        const result = controller.generateDesignPreview(inputs)
        if (!result || !result.ok) {
            previewData = ({})
            previewToken = ""
            errorText = result && result.error ? String(result.error) : "无法生成预览。"
            return
        }
        previewData = result
        previewToken = String(result.preview_token || "")
        errorText = ""
    }
    function askApply() {
        if (previewToken) applyConfirmation.open()
    }
    function applyPreview() {
        if (!previewToken) return
        if (!controller.applyGeneratedDesign(previewToken)) {
            errorText = "预览已过期或候选校验失败；原设计未替换，请重新生成预览。"
            previewData = ({})
            previewToken = ""
            return
        }
        previewData = ({})
        previewToken = ""
        errorText = "已应用到当前设计，尚未保存。可在本次编辑前撤销。"
    }

    Connections {
        target: panel.controller
        function onGeneratorPreviewInvalidated() {
            panel.previewData = ({})
            panel.previewToken = ""
        }
        function onInitializationChanged() {
            if (!panel.defaultsLoaded && panel.controller.initializationReady)
                panel.syncDefaults()
        }
        function onDesignMetaChanged() {
            const currentId = String(panel.controller.designMeta.design_id || "")
            if (panel.defaultsLoaded && currentId && currentId !== panel.boundDesignId)
                panel.syncDefaults()
        }
    }
    Component.onCompleted: syncDefaults()

    ColumnLayout {
        anchors.fill: parent
        spacing: Theme.snapshot.spaceSm

        RowLayout {
            Layout.fillWidth: true
            Label {
                Layout.fillWidth: true
                text: "参数化设计"
                color: Theme.snapshot.colorText
                font.pointSize: Theme.snapshot.fontSizeHeading
                font.weight: Theme.snapshot.fontWeightEmphasis
            }
            ThemeControls.ThemeButton {
                text: "撤销应用"
                enabled: panel.controller.canUndoGeneratedDesign
                Accessible.name: "撤销最近一次参数化设计替换"
                onClicked: {
                    if (panel.controller.undoGeneratedDesign())
                        panel.errorText = "已恢复应用前的设计、路径和未保存状态。"
                }
            }
        }
        Label {
            Layout.fillWidth: true
            text: "按真实材料数据集在 λ0 求 n/k；生成物理 nm 厚度后，扫谱保持厚度不变。"
            wrapMode: Text.WordWrap
            color: Theme.snapshot.colorTextMuted
        }
        Label {
            objectName: "filmDesignGeneratorThicknessDescription"
            Layout.fillWidth: true
            text: String(panel.controller.thicknessSummaryDescription || "")
            wrapMode: Text.WordWrap
            color: Theme.snapshot.colorTextMuted
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeSmall
        }

        ScrollView {
            id: generatorScroll
            objectName: "filmDesignGeneratorScroll"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth
            ScrollBar.vertical: ScrollBar {
                objectName: "filmDesignGeneratorVerticalScrollBar"
                policy: ScrollBar.AlwaysOn
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

            ColumnLayout {
                Layout.minimumWidth: 0
                width: generatorScroll.availableWidth
                spacing: Theme.snapshot.spaceSm

                Label { text: "经典结构"; color: Theme.snapshot.colorText; font.weight: Theme.snapshot.fontWeightEmphasis }
                ThemeControls.ThemeComboBox {
                    id: templatePicker
                    objectName: "filmDesignGeneratorTemplate"
                    Layout.fillWidth: true
                    model: panel.controller.builtinDesignTemplates
                    textRole: "title"
                    valueRole: "stable_id"
                    currentIndex: panel.indexFor(model, panel.templateId)
                    onActivated: panel.changeTemplate(String(currentValue || ""))
                }
                Label {
                    Layout.fillWidth: true
                    text: panel.selectedTemplate().description || ""
                    wrapMode: Text.WordWrap
                    color: Theme.snapshot.colorTextMuted
                }
                Label {
                    objectName: "filmDesignGeneratorApplicability"
                    Layout.fillWidth: true
                    text: {
                        const applicability = String(panel.selectedTemplate().applicability || "")
                        return applicability.length > 0
                               ? "适用场景（选型参考，不保证达标）：" + applicability
                               : "适用场景（选型参考，不保证达标）：请结合实际材料数据集与仿真结果判断。"
                    }
                    wrapMode: Text.WordWrap
                    color: Theme.snapshot.colorInfo
                }

                Label { text: "设计参数"; color: Theme.snapshot.colorText; font.weight: Theme.snapshot.fontWeightEmphasis }
                Label { text: "目标真空波长 λ0（nm）"; color: Theme.snapshot.colorTextMuted }
                ThemeControls.ThemeField {
                    objectName: "filmDesignGeneratorWavelength"
                    Layout.fillWidth: true
                    text: panel.wavelengthText
                    selectByMouse: true
                    validator: DoubleValidator { bottom: 0.000001; notation: DoubleValidator.ScientificNotation }
                    onTextEdited: { panel.wavelengthText = text; panel.changedInput() }
                }
                Label {
                    visible: panel.selectedTemplate().default_pairs !== null
                    text: "整数 pairs N（1–64）"
                    color: Theme.snapshot.colorTextMuted
                }
                ThemeControls.ThemeField {
                    objectName: "filmDesignGeneratorPairs"
                    visible: panel.selectedTemplate().default_pairs !== null
                    Layout.fillWidth: true
                    text: panel.pairsText
                    selectByMouse: true
                    validator: IntValidator { bottom: 1; top: 64 }
                    onTextEdited: { panel.pairsText = text; panel.changedInput() }
                }

                Label { text: "膜层材料与数据集"; color: Theme.snapshot.colorText; font.weight: Theme.snapshot.fontWeightEmphasis }
                Repeater {
                    model: panel.controller.generatorRoles(panel.templateId)
                    delegate: ColumnLayout {
                        required property var modelData
                        readonly property string role: String(modelData.role || "")
                        Layout.fillWidth: true
                        spacing: Theme.snapshot.spaceXs
                        Label {
                            Layout.fillWidth: true
                            text: String(modelData.label || role)
                            wrapMode: Text.WordWrap
                            color: Theme.snapshot.colorText
                            font.weight: Theme.snapshot.fontWeightEmphasis
                        }
                        ThemeControls.ThemeComboBox {
                            objectName: "filmGeneratorMaterial_" + role
                            Layout.fillWidth: true
                            model: panel.controller.filmOptions
                            textRole: "name"
                            valueRole: "id"
                            currentIndex: panel.indexFor(model, panel.roleBinding(role).material_id)
                            onActivated: panel.setRoleMaterial(role, panel.selectedOptionId(model, currentIndex))
                        }
                        ThemeControls.ThemeComboBox {
                            objectName: "filmGeneratorDataset_" + role
                            Layout.fillWidth: true
                            model: panel.roleSetModel(role)
                            textRole: "name"
                            valueRole: "id"
                            currentIndex: panel.indexFor(model, panel.roleBinding(role).optical_set_id)
                            onActivated: panel.setRoleOpticalSet(role, panel.selectedOptionId(model, currentIndex))
                        }
                    }
                }

                Label { text: "入射介质与基片"; color: Theme.snapshot.colorText; font.weight: Theme.snapshot.fontWeightEmphasis }
                Label {
                    objectName: "filmGeneratorEndpointBindingNote"
                    Layout.fillWidth: true
                    text: "若当前设计有真实材料/数据集绑定会沿用；若端点是常数 n，不会据此猜材料，下面显示材料库中的实际初始选择，请核对。"
                    wrapMode: Text.WordWrap
                    color: Theme.snapshot.colorTextMuted
                }
                Label { text: "入射介质材料"; color: Theme.snapshot.colorTextMuted }
                ThemeControls.ThemeComboBox {
                    objectName: "filmGeneratorIncidentMaterial"
                    Layout.fillWidth: true
                    model: panel.controller.mediumOptions
                    textRole: "name"
                    valueRole: "id"
                    currentIndex: panel.indexFor(model, panel.incidentBinding.material_id)
                    onActivated: panel.setEndpointMaterial("incident", "medium", panel.selectedOptionId(model, currentIndex))
                }
                ThemeControls.ThemeComboBox {
                    objectName: "filmGeneratorIncidentDataset"
                    Layout.fillWidth: true
                    model: panel.controller.opticalSetsFor(panel.incidentBinding.material_id || "")
                    textRole: "name"
                    valueRole: "id"
                    currentIndex: panel.indexFor(model, panel.incidentBinding.optical_set_id)
                    onActivated: panel.setEndpointOpticalSet("incident", panel.selectedOptionId(model, currentIndex))
                }
                Label { text: "基片材料"; color: Theme.snapshot.colorTextMuted }
                ThemeControls.ThemeComboBox {
                    objectName: "filmGeneratorSubstrateMaterial"
                    Layout.fillWidth: true
                    model: panel.controller.substrateOptions
                    textRole: "name"
                    valueRole: "id"
                    currentIndex: panel.indexFor(model, panel.substrateBinding.material_id)
                    onActivated: panel.setEndpointMaterial("substrate", "substrate", panel.selectedOptionId(model, currentIndex))
                }
                ThemeControls.ThemeComboBox {
                    objectName: "filmGeneratorSubstrateDataset"
                    Layout.fillWidth: true
                    model: panel.controller.opticalSetsForSubstrate(panel.substrateBinding.material_id || "")
                    textRole: "name"
                    valueRole: "id"
                    currentIndex: panel.indexFor(model, panel.substrateBinding.optical_set_id)
                    onActivated: panel.setEndpointOpticalSet("substrate", panel.selectedOptionId(model, currentIndex))
                }
                CheckBox {
                    objectName: "filmGeneratorAssumeUnknownK"
                    Layout.fillWidth: true
                    Layout.minimumWidth: 0
                    font.family: Theme.snapshot.fontFamily
                    font.pointSize: Theme.snapshot.fontSizeBody
                    palette.windowText: Theme.snapshot.colorText
                    palette.text: Theme.snapshot.colorText
                    palette.buttonText: Theme.snapshot.colorText
                    palette.base: Theme.snapshot.colorSurface
                    palette.highlight: Theme.snapshot.colorSelection
                    text: "仅对未知 k 显式假设无损"
                    checked: panel.assumeUnknownKLossless
                    onToggled: { panel.assumeUnknownKLossless = checked; panel.changedInput() }
                }
                Label {
                    Layout.fillWidth: true
                    text: "已知 |k| > 1e-9 始终拒绝；此选项不能绕过吸收。"
                    wrapMode: Text.WordWrap
                    color: Theme.snapshot.colorTextMuted
                }
                ThemeControls.ThemeButton {
                    id: previewAction
                    objectName: "filmDesignGeneratorPreview"
                    Layout.fillWidth: true
                    text: "生成只读预览"
                    primary: true
                    enabled: panel.controller.initializationReady
                    onClicked: panel.requestPreview()
                }
                Label {
                    objectName: "filmDesignGeneratorError"
                    Layout.fillWidth: true
                    visible: !!panel.errorText
                    text: panel.errorText
                    wrapMode: Text.WordWrap
                    color: panel.previewToken ? Theme.snapshot.colorTextMuted : Theme.snapshot.colorError
                }

                Rectangle {
                    visible: !!panel.previewToken
                    Layout.fillWidth: true
                    implicitHeight: previewSummary.implicitHeight + Theme.snapshot.spaceMd * 2
                    color: Theme.snapshot.colorSurfaceRaised
                    border.color: Theme.snapshot.colorBorder
                    radius: Theme.snapshot.radiusSm
                    ColumnLayout {
                        id: previewSummary
                        anchors.fill: parent
                        anchors.margins: Theme.snapshot.spaceMd
                        spacing: Theme.snapshot.spaceXs
                        Label {
                            Layout.fillWidth: true
                            text: (panel.previewData.template ? panel.previewData.template.title : "") +
                                  " · " + Number(panel.previewData.layer_count || 0) + " 层 · 总厚度 " +
                                  Number(panel.previewData.total_physical_thickness_nm || 0).toPrecision(8) + " nm"
                            wrapMode: Text.WordWrap
                            color: Theme.snapshot.colorText
                            font.weight: Theme.snapshot.fontWeightEmphasis
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "原理：" + String(panel.previewData.principle || "")
                            wrapMode: Text.WordWrap
                            color: Theme.snapshot.colorTextMuted
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "适用域：" + String(panel.previewData.applicability || "")
                            wrapMode: Text.WordWrap
                            color: Theme.snapshot.colorTextMuted
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "参数技巧：" + String(panel.previewData.parameter_tips || "")
                            wrapMode: Text.WordWrap
                            color: Theme.snapshot.colorTextMuted
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "限制：" + String(panel.previewData.limitations || "")
                            wrapMode: Text.WordWrap
                            color: Theme.snapshot.colorTextMuted
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "假设：" + (panel.previewData.assumptions || []).join("；")
                            wrapMode: Text.WordWrap
                            color: Theme.snapshot.colorTextMuted
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "中心解析参考：" + panel.referenceResultsText()
                            wrapMode: Text.WordWrap
                            color: Theme.snapshot.colorTextMuted
                        }
                        Label {
                            Layout.fillWidth: true
                            text: "入射侧 → 基片侧的层序与实际厚度："
                            color: Theme.snapshot.colorText
                            font.weight: Theme.snapshot.fontWeightEmphasis
                        }
                        Repeater {
                            model: panel.previewData.layers_incident_order || []
                            delegate: Label {
                                required property var modelData
                                Layout.fillWidth: true
                                text: Number(modelData.sequence) + ". " + String(modelData.role) + " · " +
                                      String(modelData.material_name) + " / " + String(modelData.dataset_name) +
                                      " · n=" + Number(modelData.n_at_wavelength).toPrecision(7) +
                                      " · d=" + Number(modelData.physical_thickness_nm).toPrecision(8) + " nm"
                                wrapMode: Text.WordWrap
                                color: Theme.snapshot.colorTextMuted
                            }
                        }
                        ThemeControls.ThemeButton {
                            objectName: "filmDesignGeneratorApply"
                            Layout.fillWidth: true
                            text: "应用并替换当前设计"
                            primary: true
                            onClicked: panel.askApply()
                        }
                    }
                }
            }
        }
    }

    ThemeControls.ThemeDialogBase {
        id: applyConfirmation
        objectName: "filmDesignGeneratorApplyConfirmation"
        title: "替换当前膜系设计？"
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: panel.applyPreview()
        onRejected: panel.clearPreview()
        contentItem: ColumnLayout {
            spacing: Theme.snapshot.spaceSm
            Label {
                Layout.fillWidth: true
                text: "将以此候选替换当前编辑设计。应用后会形成未保存的修改，并保留现有文件路径；可在其他编辑发生前撤销本次替换。"
                wrapMode: Text.WordWrap
                color: Theme.snapshot.colorText
            }
        }
    }
}
