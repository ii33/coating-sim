import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

Dialog {
    id: dialog
    objectName: "processEnvironmentDialog"
    required property var editor
    property int stepIndex: -1
    property var inherited: ({})
    property string errorMessage: ""
    readonly property bool isOverride: stepIndex >= 0
    readonly property bool transportEnabled: transportChoice.currentIndex === 2
        || (transportChoice.currentIndex === 0 && isOverride && inherited.transport_model === "ballistic_loss")
    modal: true
    title: isOverride ? "第 " + (stepIndex + 1) + " 层 · 温度与真空覆盖" : "工艺默认 · 温度与真空"
    width: Math.min(800, parent ? parent.width - 32 : 800)
    height: Math.min(transportEnabled ? 780 : 440, parent ? parent.height - 32 : 780)
    x: parent ? (parent.width - width) / 2 : 0
    y: parent ? (parent.height - height) / 2 : 0
    standardButtons: Dialog.NoButton
    ListModel { id: scalarRows }
    ListModel { id: gasRows }

    function present(value) { return value !== undefined && value !== null }
    function fieldState(raw, key) { return raw[key] === undefined ? 0 : (raw[key] === null ? 2 : 1) }
    function inheritedText(key, unit) {
        return present(inherited[key]) ? "工艺默认：" + inherited[key] + (unit ? " " + unit : "") : "工艺默认：未记录"
    }
    function fill(raw) {
        errorMessage = ""
        scalarRows.clear()
        const fields = [
            ["substrate_temperature_k", "基片温度", "K", "仅记录；未建模粘附、再蒸发或生长温度响应"],
            ["source_temperature_k", "蒸发源温度", "K", "仅记录；不由温度推算蒸发速率"],
            ["gas_temperature_k", "气体温度", "K", "启用损失模型时用于气体数密度；各组分共用此温度"],
            ["base_pressure_pa", "沉积前本底真空", "Pa（绝对压）", "仅记录；不会与工作总压相加"],
            ["operating_pressure_pa", "沉积时工作总压", "Pa（绝对压）", "参与输运损失；启用组分模型时须与分压之和一致"],
            ["effective_cross_section_m2", "有效混合气体碰撞截面", "m²", "无气体组分时使用；未知请留空，不能当作 0"],
            ["effective_cross_section_source", "混合截面依据", "", "缺少依据时标为用户假设 / 无来源，不能视作已校准；不自动猜测截面"]
        ]
        for (let i = 0; i < fields.length; ++i) {
            const f = fields[i]
            scalarRows.append({key: f[0], label: f[1], unit: f[2], hint: f[3],
                modeIndex: fieldState(raw, f[0]), valueText: present(raw[f[0]]) ? String(raw[f[0]]) : ""})
        }
        const model = raw.transport_model
        transportChoice.currentIndex = model === undefined ? 0 : model === "disabled" ? 1 : model === "ballistic_loss" ? 2 : 3
        gasMode.currentIndex = fieldState(raw, "gas_components")
        gasRows.clear()
        const components = raw.gas_components || []
        for (let i = 0; i < components.length; ++i) {
            const g = components[i]
            gasRows.append({species: g.species || "", partialText: present(g.partial_pressure_pa) ? String(g.partial_pressure_pa) : "",
                sectionText: present(g.effective_cross_section_m2) ? String(g.effective_cross_section_m2) : "",
                flowText: present(g.flow_sccm) ? String(g.flow_sccm) : "", sourceText: g.cross_section_source || ""})
        }
    }
    function prepare(index) {
        stepIndex = index
        const state = editor.environmentEditor(index)
        inherited = state.inherited || {}
        fill(state.raw || {})
        open()
    }
    function collect() {
        const value = {}
        if (transportChoice.currentIndex > 0)
            value.transport_model = [undefined, "disabled", "ballistic_loss", null][transportChoice.currentIndex]
        for (let i = 0; i < scalarRows.count; ++i) {
            const row = scalarRows.get(i)
            if (row.modeIndex === 1) value[row.key] = row.valueText.trim()
            else if (row.modeIndex === 2) value[row.key] = null
        }
        if (gasMode.currentIndex === 2) value.gas_components = null
        else if (gasMode.currentIndex === 1) {
            value.gas_components = []
            for (let i = 0; i < gasRows.count; ++i) {
                const row = gasRows.get(i)
                const gas = {species: row.species.trim()}
                if (row.partialText.trim().length) gas.partial_pressure_pa = row.partialText.trim()
                if (row.sectionText.trim().length) gas.effective_cross_section_m2 = row.sectionText.trim()
                if (row.flowText.trim().length) gas.flow_sccm = row.flowText.trim()
                if (row.sourceText.trim().length) gas.cross_section_source = row.sourceText.trim()
                value.gas_components.push(gas)
            }
        }
        return value
    }
    function applyDraft() {
        const result = editor.applyEnvironment(stepIndex, collect())
        errorMessage = result.message || ""
        if (result.ok) close()
        return result.ok
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 8
        ScrollView {
            id: environmentScroll
            objectName: "environmentScroll"
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: availableWidth
            clip: true
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ColumnLayout {
                width: environmentScroll.availableWidth
                spacing: 10
                Label {
                    Layout.fillWidth: true; wrapMode: Text.WordWrap
                    text: dialog.isOverride
                        ? "未设置的字段继承工艺默认；清空为未知值（null），阻止继承。气体组分按整组替换，不逐项合并。"
                        : "默认值用于未覆盖的膜层。温度使用开尔文 K；压力为绝对 Pa；碰撞截面为 m²。未设置保持旧工艺行为。"
                }
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "输运模型"; Layout.preferredWidth: 160 }
                    ComboBox {
                        id: transportChoice
                        objectName: "environmentTransportChoice"
                        Layout.fillWidth: true
                        model: [dialog.isOverride ? "继承工艺默认" : "未设置（关闭）", "disabled · 仅记录输入", "ballistic_loss · 零碰撞直达束", "清空（关闭且不继承）"]
                    }
                }
                Label {
                    visible: dialog.isOverride; Layout.fillWidth: true; wrapMode: Text.WordWrap
                    text: "工艺默认输运模型：" + (dialog.inherited.transport_model || "disabled")
                    color: palette.placeholderText
                }
                Label {
                    Layout.fillWidth: true; wrapMode: Text.WordWrap
                    text: "温度与真空属于各层的工艺条件；源设计保存发射几何与分布。关闭输运时隐藏碰撞参数，保留已有数值便于重新启用。"
                    color: palette.placeholderText
                }
                Label {
                    objectName: "environmentModelLimitations"
                    visible: dialog.transportEnabled
                    Layout.fillWidth: true; wrapMode: Text.WordWrap
                    text: "ballistic_loss 仅衰减零碰撞直达束，不模拟散射后沉积、反应化学或源区自碰撞。厚度仍按平均 / 中心 / 监测器目标归一化；不是绝对净生长速率预测，均匀损失可能被归一化抵消。速率与时间仍由工艺输入给定。"
                }
                Repeater {
                    id: environmentFields
                    model: scalarRows
                    delegate: ColumnLayout {
                        required property int index
                        required property string key
                        required property string label
                        required property string unit
                        required property string hint
                        required property int modeIndex
                        required property string valueText
                        objectName: "environmentRow_" + key
                        visible: dialog.transportEnabled || ["substrate_temperature_k", "source_temperature_k", "base_pressure_pa"].indexOf(key) >= 0
                        Layout.fillWidth: true
                        spacing: 3
                        RowLayout {
                            Layout.fillWidth: true
                            Label { text: label + (unit ? " / " + unit : ""); Layout.preferredWidth: 240; wrapMode: Text.WordWrap }
                            ComboBox {
                                objectName: "environmentState_" + key
                                Layout.preferredWidth: 140
                                model: [dialog.isOverride ? "继承" : "未设置", "设置数值 / 文本", "清空为未知"]
                                currentIndex: modeIndex
                                onActivated: function(choice) { scalarRows.setProperty(index, "modeIndex", choice) }
                            }
                            ThemeControls.ThemeField {
                                objectName: "environmentValue_" + key
                                Layout.fillWidth: true
                                enabled: modeIndex === 1
                                text: key === "effective_cross_section_source" ? valueText : ""
                                numericDisplayValue: key === "effective_cross_section_source" ? undefined : valueText
                                placeholderText: key === "effective_cross_section_source" ? "依据 / 假设来源" : "可用科学计数法"
                                inputMethodHints: key === "effective_cross_section_source" ? Qt.ImhNone : Qt.ImhFormattedNumbersOnly
                                onTextEdited: scalarRows.setProperty(index, "valueText", text)
                            }
                        }
                        Label {
                            Layout.fillWidth: true; wrapMode: Text.WordWrap
                            text: hint + (dialog.isOverride ? " · " + dialog.inheritedText(key, unit) : "")
                            color: palette.placeholderText
                        }
                    }
                }
                GroupBox {
                    objectName: "environmentGasGroup"
                    visible: dialog.transportEnabled
                    title: "工作气体 / 残余气体组分"
                    Layout.fillWidth: true
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 8
                        RowLayout {
                            Layout.fillWidth: true
                            ComboBox {
                                id: gasMode
                                objectName: "environmentGasMode"
                                Layout.fillWidth: true
                                model: [dialog.isOverride ? "继承整组气体" : "不指定组分（可使用有效混合截面）", "设置整组气体", "清空继承组分"]
                            }
                            Button {
                                objectName: "addEnvironmentGas"
                                text: "添加气体"
                                enabled: gasMode.currentIndex === 1
                                onClicked: gasRows.append({species: "", partialText: "", sectionText: "", flowText: "", sourceText: ""})
                            }
                        }
                        Label {
                            Layout.fillWidth: true; wrapMode: Text.WordWrap
                            text: "分压使用 Pa（绝对压），每种气体的截面须由用户提供。流量 sccm 仅记录，不换算压力；本底压力不与工作总压相加。启用组分损失时，分压之和必须等于工作总压。设置组分时请取消设置 / 清空继承的混合截面，避免双计。未提供截面依据时标为用户假设 / 无来源，须自行校准。"
                        }
                        Label {
                            visible: dialog.isOverride && gasMode.currentIndex === 0
                            Layout.fillWidth: true; wrapMode: Text.WordWrap
                            text: "继承组分：" + ((dialog.inherited.gas_components || []).map(function(g) { return g.species + " " + g.partial_pressure_pa + " Pa" }).join("；") || "未记录")
                            color: palette.placeholderText
                        }
                        Repeater {
                            id: environmentGases
                            model: gasRows
                            delegate: Frame {
                                required property int index
                                required property string species
                                required property string partialText
                                required property string sectionText
                                required property string flowText
                                required property string sourceText
                                visible: gasMode.currentIndex === 1
                                Layout.fillWidth: true
                                ColumnLayout {
                                    anchors.fill: parent
                                    RowLayout {
                                        Layout.fillWidth: true
                                        Label { text: "气体 " + (index + 1); font.bold: true; Layout.fillWidth: true }
                                        Button { objectName: "removeEnvironmentGas" + index; text: "移除"; onClicked: gasRows.remove(index) }
                                    }
                                    GridLayout {
                                        Layout.fillWidth: true
                                        columns: 4
                                        Label { text: "气体名称" }
                                        ThemeControls.ThemeField { objectName: "environmentGasSpecies" + index; text: species; Layout.fillWidth: true; onTextEdited: gasRows.setProperty(index, "species", text) }
                                        Label { text: "分压 / Pa" }
                                        ThemeControls.ThemeField { objectName: "environmentGasPressure" + index; numericDisplayValue: partialText; placeholderText: "未知留空"; Layout.fillWidth: true; onTextEdited: gasRows.setProperty(index, "partialText", text) }
                                        Label { text: "碰撞截面 / m²" }
                                        ThemeControls.ThemeField { objectName: "environmentGasSection" + index; numericDisplayValue: sectionText; placeholderText: "未知留空"; Layout.fillWidth: true; onTextEdited: gasRows.setProperty(index, "sectionText", text) }
                                        Label { text: "流量 / sccm" }
                                        ThemeControls.ThemeField { objectName: "environmentGasFlow" + index; numericDisplayValue: flowText; placeholderText: "可选，仅记录"; Layout.fillWidth: true; onTextEdited: gasRows.setProperty(index, "flowText", text) }
                                        Label { text: "截面依据" }
                                        ThemeControls.ThemeField { objectName: "environmentGasSource" + index; text: sourceText; placeholderText: "测量、文献或明确的假设来源"; Layout.fillWidth: true; Layout.columnSpan: 3; onTextEdited: gasRows.setProperty(index, "sourceText", text) }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        Label {
            objectName: "environmentError"
            visible: text.length > 0
            text: dialog.errorMessage
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: typeof Theme !== "undefined" ? Theme.snapshot.colorError
                                                : (palette.window.r < 0.5 ? "#ff9992" : "#b3261e")
        }
        RowLayout {
            Layout.fillWidth: true
            Button {
                objectName: "resetEnvironment"
                text: dialog.isOverride ? "全部恢复继承" : "全部取消设置"
                onClicked: dialog.fill({})
            }
            Item { Layout.fillWidth: true }
            Button { objectName: "cancelEnvironment"; text: "取消"; onClicked: dialog.reject() }
            Button { objectName: "applyEnvironment"; text: "应用"; onClicked: dialog.applyDraft() }
        }
    }
}
