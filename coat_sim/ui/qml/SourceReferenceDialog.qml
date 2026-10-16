import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ThemePageDialog {
    id: dialog
    required property var calculator
    title: "ZnS文献参考计算"
    modal: true
    anchors.centerIn: parent
    width: Math.min(940, parent.width - 24)
    height: Math.min(820, parent.height - 32)
    closePolicy: Popup.NoAutoClose
    property bool gasModel: field("model_id") === "zns_ri8301_gas_v1"
    property bool sealedGas: field("closure") === "sealed_stoichiometric_gas"
    property bool ideal: field("closure") === "ideal_effusion"
    property bool exitArea: ideal || field("closure") === "congruent_outgoing"
    function field(key) { return String(calculator.fields[key] === undefined ? "" : calculator.fields[key]) }
    onOpened: { calculator.begin(); tabs.currentIndex = 0 }
    onRejected: calculator.cancel()

    contentItem: ColumnLayout {
        spacing: 8
        ThemeControls.ThemeLabel { Layout.fillWidth: true; text: "文献条件下的压力 / 理论源通量参考。无功率→温度求解，不是绝对沉积速率；不会更改工艺中的 nm/s。未知系数留空，不自动填零或一。切换模式后，仅当前可见的适用输入会写入场景。"; wrapMode: Text.Wrap }
        TabBar {
            id: tabs
            Layout.fillWidth: true
            TabButton { text: "输入场景" }
            TabButton { text: "计算结果与证据" }
            TabButton { text: "嵌入的文献记录" }
        }
        ScrollView {
            id: scroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: availableWidth
            clip: true
            ScrollBar.vertical.policy: ScrollBar.AlwaysOn
            Column {
                width: scroll.availableWidth - 12
                spacing: 12
                Column {
                    visible: tabs.currentIndex === 0
                    width: parent.width
                    spacing: 10
                    ThemeControls.ThemeLabel { width: parent.width; wrapMode: Text.Wrap; visible: !dialog.calculator.available; text: "当前源没有完整 ZnS 文献快照。请先在材料库选用 ZnS 文献参考或其完整副本。"; color: typeof Theme !== "undefined" ? Theme.snapshot.colorError : "#c46840" }
                    ThemeControls.ThemeLabel { text: "参考模式（两种来源不拼接、不平均）"; font.bold: true }
                    ThemeControls.ThemeComboBox {
                        objectName: "referenceModelCombo"
                        width: parent.width
                        model: [{text: "供应商压力插值 · 1223.15–1343.15 K（不能转成 HK 通量）", value: "zns_vendor_pressure_v1"}, {text: "Zn(g) + S₂(g) 条件平衡 / HK · 1180–1210 K", value: "zns_ri8301_gas_v1"}]
                        textRole: "text"; valueRole: "value"
                        currentIndex: dialog.gasModel ? 1 : 0
                        onActivated: dialog.calculator.setField("model_id", currentValue)
                    }
                    SourceValueField { width: parent.width; fieldName: "referenceTemperatureField"; label: "均匀指定温度 / K（留空表示未知）"; value: dialog.field("temperature_K"); hint: dialog.gasModel ? "仅 1180–1210 K；ZnS 闪锌矿，理想气体，仅 Zn 与 S₂；禁止外推" : "仅 1223.15–1343.15 K；压力定义未辨明，不能当作总压或物种分压"; onEdited: value => dialog.calculator.setField("temperature_K", value) }
                    Column {
                        width: parent.width
                        spacing: 10
                        visible: dialog.gasModel
                        ThemeControls.ThemeLabel { text: "必须明确选择气体组成 / 逸出闭合条件"; font.bold: true }
                        ThemeControls.ThemeComboBox {
                            objectName: "referenceClosureCombo"
                            width: parent.width
                            model: [{text: "未指定（不能推断物种分压）", value: ""}, {text: "指定 pZn / pS₂：条件边界情景", value: "pressure_ratio"}, {text: "封闭化学计量气体：仅压力，无净逸出", value: "sealed_stoichiometric_gas"}, {text: "一致化学计量逸出：显式物种系数", value: "congruent_outgoing"}, {text: "显式理想小孔逸出情景（α=τ=1）", value: "ideal_effusion"}]
                            textRole: "text"; valueRole: "value"
                            currentIndex: ["", "pressure_ratio", "sealed_stoichiometric_gas", "congruent_outgoing", "ideal_effusion"].indexOf(dialog.field("closure"))
                            onActivated: dialog.calculator.setField("closure", currentValue)
                        }
                        SourceValueField { visible: dialog.field("closure") === "pressure_ratio"; width: parent.width; fieldName: "referencePressureRatioField"; label: "物种分压比 pZn / pS₂（必须 > 0）"; value: dialog.field("pressure_ratio"); onEdited: value => dialog.calculator.setField("pressure_ratio", value) }
                        ThemeControls.ThemeLabel { visible: dialog.sealedGas; width: parent.width; wrapMode: Text.Wrap; text: "封闭气体的 pZn/pS₂=2 只描述此压力闭合；不代表净逸出，不输出通量。" }
                        ThemeControls.ThemeLabel { visible: dialog.ideal; width: parent.width; wrapMode: Text.Wrap; text: "你已明确选择理想小孔模型，αZn=αS₂=τZn=τS₂=1。它是理想出口情景，不是实测 ZnS 开放表面升华系数，也不表示装置已标定。"; color: typeof Theme !== "undefined" ? Theme.snapshot.colorWarning : "#b07730" }
                        Column {
                            width: parent.width
                            spacing: 10
                            visible: !dialog.sealedGas && !dialog.ideal
                            ThemeControls.ThemeLabel { width: parent.width; wrapMode: Text.Wrap; text: "α 为物种有效逸出系数（非已测表面升华系数）。τ 为用户显式给出的物种透过情景；不能由盖板 / 挡板几何自动推断。留空不会假定为 1。一致化学计量逸出闭合要求这些系数大于 0。" }
                            Repeater {
                                model: [{key:"alpha_Zn", label:"Zn 有效逸出系数 αZn（0–1，未知留空）"}, {key:"alpha_S2", label:"S₂ 有效逸出系数 αS₂（0–1，未知留空）"}, {key:"transmission_Zn", label:"Zn 透过系数 τZn（0–1，未知留空）"}, {key:"transmission_S2", label:"S₂ 透过系数 τS₂（0–1，未知留空）"}]
                                SourceValueField { required property var modelData; width: parent.width; fieldName: "reference_" + modelData.key; label: modelData.label; value: dialog.field(modelData.key); onEdited: value => dialog.calculator.setField(modelData.key, value) }
                            }
                        }
                        Column {
                            width: parent.width
                            spacing: 10
                            visible: !dialog.sealedGas
                            ThemeControls.ThemeLabel { text: dialog.exitArea ? "实际出口 / 小孔面积（非源料面）" : "总量使用的物理面积"; font.bold: true }
                            ThemeControls.ThemeComboBox {
                                objectName: "referenceAreaBasisCombo"
                                width: parent.width
                                model: dialog.exitArea ? [{text:"显式出口 / 小孔面积", value:"explicit"}] : [{text:"显式面积（留空仅显示单位面积通量）", value:"explicit"}, {text:"使用源几何的物理面积（均匀 T 的边界假设）", value:"source_geometry"}]
                                textRole: "text"; valueRole: "value"
                                currentIndex: dialog.field("area_basis") === "source_geometry" && !dialog.exitArea ? 1 : 0
                                onActivated: dialog.calculator.setField("area_basis", currentValue)
                            }
                            SourceValueField { visible: dialog.field("area_basis") === "explicit"; width: parent.width; fieldName: "referenceAreaField"; label: (dialog.exitArea ? "出口面积" : "物理发射面积") + " / mm²（未知留空）"; value: dialog.field("emitting_area_mm2"); hint: "仅用于乘算总粒子率 / 总质量率，保存单位为 m²；不使用 active_fraction 或束斑 σ 代替物理面积"; onEdited: value => dialog.calculator.setField("emitting_area_mm2", value) }
                            ThemeControls.ThemeLabel { width: parent.width; wrapMode: Text.Wrap; text: "面积积分假设所选边界均匀温度。源几何只适用于显式分压比边界；点源 / 线源无可推得的面积。盖板 / 挡板的 τ 仍是独立标量情景，没有热-流-沉积耦合求解。"; opacity: 0.8 }
                        }
                    }
                    ThemeControls.ThemeButton { objectName: "calculateReferenceButton"; text: "计算文献参考结果"; enabled: dialog.calculator.available; onClicked: { dialog.calculator.calculate(); tabs.currentIndex = 1 } }
                }
                Column {
                    width: parent.width
                    visible: tabs.currentIndex === 1
                    spacing: 10
                    ThemeControls.ThemeLabel { width: parent.width; wrapMode: Text.Wrap; text: dialog.calculator.status; font.bold: true }
                    ThemeControls.ThemeLabel { width: parent.width; wrapMode: Text.Wrap; visible: dialog.calculator.error.length > 0; text: dialog.calculator.error; color: typeof Theme !== "undefined" ? Theme.snapshot.colorError : "#c46840" }
                    ReferenceSections { width: parent.width; sections: dialog.calculator.resultSections }
                    ThemeControls.ThemeLabel { width: parent.width; wrapMode: Text.Wrap; visible: !dialog.calculator.hasResult; text: "没有当前有效结果。修改输入、材料或源几何后，旧结果会清除；请返回输入场景重新计算。" }
                }
                ReferenceSections { width: parent.width; visible: tabs.currentIndex === 2; sections: dialog.calculator.referenceSections }
            }
        }
        ThemeControls.ThemeLabel { Layout.fillWidth: true; wrapMode: Text.Wrap; visible: tabs.currentIndex !== 1; text: dialog.calculator.error || dialog.calculator.status; color: dialog.calculator.error.length > 0 ? (typeof Theme !== "undefined" ? Theme.snapshot.colorError : "#c46840") : Theme.snapshot.colorText }
    }
    footer: DialogButtonBox {
        ThemeControls.ThemeButton { objectName: "applyReferenceButton"; text: "应用场景到源设计"; enabled: dialog.calculator.available; onClicked: { if (dialog.calculator.apply()) dialog.close() } }
        ThemeControls.ThemeButton { objectName: "clearReferenceButton"; text: "移除已存场景"; enabled: dialog.calculator.configured; onClicked: { dialog.calculator.clear(); dialog.close() } }
        ThemeControls.ThemeButton { objectName: "cancelReferenceButton"; text: "取消（不改变源）"; onClicked: { dialog.calculator.cancel(); dialog.close() } }
    }
}
