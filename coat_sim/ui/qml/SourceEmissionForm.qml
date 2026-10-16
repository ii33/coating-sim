import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

Column {
    id: form
    required property var editor
    property var f: editor.emissionFields
    property var hole: editor.apertureFields
    spacing: 8

    function field(key) { return String(f[key] === undefined ? "" : f[key]) }
    function holeField(key) { return String(hole[key] === undefined ? "" : hole[key]) }

    TextMetrics {
        id: emissionLabelMetrics
        font.family: typeof Theme !== "undefined" ? Theme.snapshot.fontFamily : "sans-serif"
        font.pointSize: typeof Theme !== "undefined" ? Theme.snapshot.fontSizeBody : 12
        text: "设备有效最大沉积速率 / nm/s"
    }
    readonly property real minimumFieldColumnWidth:
        Math.max(160, emissionLabelMetrics.width * 0.58 + 92)

    ThemeControls.ThemeLabel {
        objectName: "sourceEmissionDetails"
        width: parent.width
        wrapMode: Text.Wrap
        text: "空间发射模型给出归一化的空间分布：uniform 采用空间均匀分布，电阻舟与电子束模型可依据沉积速率修正分布；角分布指数独立于面源宽度。未经校准的参数仅代表本项目模型设定，不代表设备实测；实际设备请以校准值为准。"
        muted: true
    }

    ThemeControls.ThemeLabel {
        objectName: "sourceGeometryModelWarning"
        width: parent.width
        visible: form.field("kind") !== "uniform" && form.editor.fields.kind === "point"
        text: "当前几何为点源，扫描模型不会扩展空间分布；请选择线源、圆盘源或矩形面源以表达空间展宽。"
        wrapMode: Text.Wrap
        color: typeof Theme !== "undefined" ? Theme.snapshot.colorWarning : "#b27721"
    }

    GridLayout {
        objectName: "sourceEmissionSharedParameters"
        width: parent.width
        columns: width >= 2 * form.minimumFieldColumnWidth + columnSpacing ? 2 : 1
        columnSpacing: 8
        rowSpacing: 4
        SourceValueField {
            objectName: "sourceSamplingOrder"
            Layout.fillWidth: true
            compact: true
            compactFieldWidth: 76
            fieldName: "sourceSamplingOrder"
            label: "采样阶数 (3–41)"
            value: String(form.editor.fields.sampling_order)
            hint: "采样阶数控制源面离散精度。允许范围为 3–41；提高阶数会增加同一模型的采样点数。"
            onEdited: value => form.editor.setField("sampling_order", value)
        }
        SourceValueField {
            objectName: "sourceQuadratureOrder"
            Layout.fillWidth: true
            compact: true
            compactFieldWidth: 76
            fieldName: "source_quadrature_order"
            label: "积分阶数 (3–21)"
            value: form.field("quadrature_order")
            hint: "模型积分使用的求积阶数；允许范围为 3–21。"
            onEdited: value => form.editor.setEmissionField("quadrature_order", value)
        }
        Repeater {
            model: [
                {key: "reference_rate_nm_s", label: "参考速率 / Å/s", hint: "速率指数以此参考速率为基准。"},
                {key: "rate_exponent_per_decade", label: "速率指数 / 每十倍", hint: "速率每变化十倍时，模型参数的变化指数。"},
                {key: "valid_rate_min_nm_s", label: "有效速率下限 / Å/s", hint: "标注此模型参数的有效速率范围。"},
                {key: "valid_rate_max_nm_s", label: "有效速率上限 / Å/s", hint: "标注此模型参数的有效速率范围。"}
            ]
            SourceValueField {
                required property var modelData
                Layout.fillWidth: true
                compact: true
                compactFieldWidth: 76
                fieldName: "source_" + modelData.key
                label: modelData.label
                value: form.field(modelData.key)
                hint: modelData.hint
                onEdited: value => form.editor.setEmissionField(modelData.key, value)
            }
        }
    }

    Column {
        objectName: "sourceResistanceBoatParameters"
        width: parent.width
        spacing: 8
        visible: form.field("kind") === "resistance_boat"

        GridLayout {
            width: parent.width
            columns: width >= 2 * form.minimumFieldColumnWidth + columnSpacing ? 2 : 1
            columnSpacing: 8
            rowSpacing: 4
            Repeater {
                model: [
                    {key: "active_fraction", label: "有效尺寸比例 (0–1)"},
                    {key: "rate_active_fraction_per_decade", label: "尺寸变化 / 每十倍"},
                    {key: "center_weight", label: "中心强度权重"}
                ]
                SourceValueField {
                    required property var modelData
                    Layout.fillWidth: true
                    compact: true
                    compactFieldWidth: 76
                    fieldName: "source_" + modelData.key
                    label: modelData.label
                    value: form.field(modelData.key)
                    hint: modelData.key === "active_fraction" ? "有效尺寸比例须在 0–1 范围内。" : "项目模型参数，使用前应核对适用工艺或校准来源。"
                    onEdited: value => form.editor.setEmissionField(modelData.key, value)
                }
            }
        }

        ThemeControls.ThemeLabel { text: "舟体 / 遮盖方式"; font.bold: true }
        ThemeControls.ThemeComboBox {
            objectName: "boatMode"
            width: parent.width
            model: [{text: "开放式", value: "open"}, {text: "带盖 / 有限开口", value: "covered"}]
            textRole: "text"
            valueRole: "value"
            currentIndex: ["open", "covered"].indexOf(form.field("boat_mode"))
            onActivated: form.editor.setEmissionField("boat_mode", currentValue)
        }

        Column {
            objectName: "sourceCoveredBoatParameters"
            width: parent.width
            spacing: 8
            visible: form.field("boat_mode") === "covered"

            GridLayout {
                width: parent.width
                columns: width >= 2 * form.minimumFieldColumnWidth + columnSpacing ? 2 : 1
                columnSpacing: 8
                rowSpacing: 4
                SourceValueField {
                    Layout.fillWidth: true
                    compact: true
                    compactFieldWidth: 76
                    fieldName: "cover_height_mm"
                    label: "盖高 / mm"
                    value: form.field("cover_height_mm")
                    onEdited: value => form.editor.setEmissionField("cover_height_mm", value)
                }
                SourceValueField {
                    Layout.fillWidth: true
                    compact: true
                    compactFieldWidth: 76
                    fieldName: "cover_thickness_mm"
                    label: "盖厚 / mm"
                    value: form.field("cover_thickness_mm")
                    onEdited: value => form.editor.setEmissionField("cover_thickness_mm", value)
                }
            }

            ThemeControls.ThemeLabel { text: "开口 (局部 mm)"; font.bold: true }
            ThemeControls.ThemeComboBox {
                objectName: "apertureList"
                width: parent.width
                model: form.editor.apertures.map(function(hole, index) {
                    return "开口 " + (index + 1) + " · " + (hole.kind === "disk" ? "圆形" : "矩形") + " · (" + hole.offset_x_mm + ", " + hole.offset_y_mm + ")"
                })
                currentIndex: form.editor.apertureIndex
                onActivated: index => form.editor.selectAperture(index)
            }
            Row {
                spacing: 8
                ThemeControls.ThemeButton {
                    objectName: "addApertureButton"
                    text: "添加开口"
                    enabled: form.editor.apertures.length < 100
                    onClicked: form.editor.addAperture()
                }
                ThemeControls.ThemeButton {
                    objectName: "removeApertureButton"
                    text: "删除开口"
                    enabled: form.editor.apertures.length > 1
                    onClicked: form.editor.removeAperture()
                }
            }
            ThemeControls.ThemeComboBox {
                objectName: "apertureShape"
                width: parent.width
                model: [{text: "矩形开口", value: "rectangle"}, {text: "圆形开口", value: "disk"}]
                textRole: "text"
                valueRole: "value"
                currentIndex: ["rectangle", "disk"].indexOf(form.holeField("kind"))
                onActivated: form.editor.setApertureField("kind", currentValue)
            }
            GridLayout {
                width: parent.width
                columns: width >= 2 * form.minimumFieldColumnWidth + columnSpacing ? 2 : 1
                columnSpacing: 8
                rowSpacing: 4
                Repeater {
                    model: form.holeField("kind") === "disk"
                        ? [{key: "radius_mm", label: "开口半径 / mm"}]
                        : [{key: "length_mm", label: "开口长 / mm"}, {key: "width_mm", label: "开口宽 / mm"}]
                    SourceValueField {
                        required property var modelData
                        Layout.fillWidth: true
                        compact: true
                        compactFieldWidth: 76
                        fieldName: "aperture_" + modelData.key
                        label: modelData.label
                        value: form.holeField(modelData.key)
                        onEdited: value => form.editor.setApertureField(modelData.key, value)
                    }
                }
                SourceValueField {
                    Layout.fillWidth: true
                    compact: true
                    compactFieldWidth: 76
                    fieldName: "aperture_offset_x_mm"
                    label: "开口偏移 X / mm"
                    value: form.holeField("offset_x_mm")
                    onEdited: value => form.editor.setApertureField("offset_x_mm", value)
                }
                SourceValueField {
                    Layout.fillWidth: true
                    compact: true
                    compactFieldWidth: 76
                    fieldName: "aperture_offset_y_mm"
                    label: "开口偏移 Y / mm"
                    value: form.holeField("offset_y_mm")
                    onEdited: value => form.editor.setApertureField("offset_y_mm", value)
                }
                SourceValueField {
                    Layout.fillWidth: true
                    compact: true
                    compactFieldWidth: 76
                    visible: form.holeField("kind") === "rectangle"
                    fieldName: "aperture_angle_deg"
                    label: "开口偏转 / °"
                    value: form.holeField("angle_deg")
                    onEdited: value => form.editor.setApertureField("angle_deg", value)
                }
            }
            ThemeControls.ThemeLabel {
                objectName: "sourceCoveredBoatRestriction"
                width: parent.width
                wrapMode: Text.Wrap
                text: "遮盖假设为沿法线直通孔。不同腔室之间不互通；模型不含反射、透射或盖下扩散。实际几何超出该假设时，应使用经验证的开口位置和尺寸。"
            }
        }
    }

    Column {
        objectName: "sourceElectronBeamParameters"
        width: parent.width
        spacing: 8
        visible: form.field("kind") === "electron_beam"

        GridLayout {
            width: parent.width
            columns: width >= 2 * form.minimumFieldColumnWidth + columnSpacing ? 2 : 1
            columnSpacing: 8
            rowSpacing: 4
            SourceValueField {
                Layout.fillWidth: true
                compact: true
                compactFieldWidth: 76
                fieldName: "spot_sigma_mm"
                label: "光斑 σ / mm"
                value: form.field("spot_sigma_mm")
                hint: "高斯光斑的空间标准差；未经校准时仅为模型参数。"
                onEdited: value => form.editor.setEmissionField("spot_sigma_mm", value)
            }
            SourceValueField {
                Layout.fillWidth: true
                compact: true
                compactFieldWidth: 76
                fieldName: "rate_spot_sigma_per_decade"
                label: "光斑变化 / 每十倍"
                value: form.field("rate_spot_sigma_per_decade")
                onEdited: value => form.editor.setEmissionField("rate_spot_sigma_per_decade", value)
            }
        }

        ThemeControls.ThemeLabel { text: "扫描轨迹"; font.bold: true }
        ThemeControls.ThemeComboBox {
            objectName: "scanPattern"
            width: parent.width
            model: [
                {text: "固定点", value: "point"},
                {text: "直线扫描", value: "line"},
                {text: "矩形栅格扫描", value: "raster"},
                {text: "圆环扫描", value: "ring"},
                {text: "自定义驻留点", value: "custom"}
            ]
            textRole: "text"
            valueRole: "value"
            currentIndex: ["point", "line", "raster", "ring", "custom"].indexOf(form.field("scan_pattern"))
            onActivated: form.editor.setEmissionField("scan_pattern", currentValue)
        }
        GridLayout {
            objectName: "sourceElectronBeamScanFields"
            width: parent.width
            columns: width >= 2 * form.minimumFieldColumnWidth + columnSpacing ? 2 : 1
            columnSpacing: 8
            rowSpacing: 4
            SourceValueField {
                Layout.fillWidth: true
                visible: form.field("scan_pattern") !== "point" && form.field("scan_pattern") !== "custom"
                compact: true
                compactFieldWidth: 76
                fieldName: "scan_width_mm"
                label: "扫描 X 宽 / mm"
                value: form.field("scan_width_mm")
                onEdited: value => form.editor.setEmissionField("scan_width_mm", value)
            }
            SourceValueField {
                Layout.fillWidth: true
                visible: form.field("scan_pattern") === "raster" || form.field("scan_pattern") === "ring"
                compact: true
                compactFieldWidth: 76
                fieldName: "scan_height_mm"
                label: "扫描 Y 高 / mm"
                value: form.field("scan_height_mm")
                onEdited: value => form.editor.setEmissionField("scan_height_mm", value)
            }
            SourceValueField {
                Layout.fillWidth: true
                compact: true
                compactFieldWidth: 76
                fieldName: "scan_center_x_mm"
                label: "扫描中心 X / mm"
                value: form.field("scan_center_x_mm")
                onEdited: value => form.editor.setEmissionField("scan_center_x_mm", value)
            }
            SourceValueField {
                Layout.fillWidth: true
                compact: true
                compactFieldWidth: 76
                fieldName: "scan_center_y_mm"
                label: "扫描中心 Y / mm"
                value: form.field("scan_center_y_mm")
                onEdited: value => form.editor.setEmissionField("scan_center_y_mm", value)
            }
            SourceValueField {
                Layout.fillWidth: true
                compact: true
                compactFieldWidth: 76
                fieldName: "scan_angle_deg"
                label: "扫描方向 / °"
                value: form.field("scan_angle_deg")
                onEdited: value => form.editor.setEmissionField("scan_angle_deg", value)
            }
            SourceValueField {
                Layout.fillWidth: true
                compact: true
                compactFieldWidth: 76
                fieldName: "dwell_center_fraction"
                label: "中心驻留占比 (0–1)"
                value: form.field("dwell_center_fraction")
                onEdited: value => form.editor.setEmissionField("dwell_center_fraction", value)
            }
        }
        ThemeControls.ThemeLabel {
            objectName: "sourceCustomDwellLimit"
            width: parent.width
            visible: form.field("scan_pattern") === "custom"
            text: "每行：局部 X / mm、局部 Y / mm、驻留权重。最多 100 点；权重须在 0–1。"
            wrapMode: Text.Wrap
        }
        TextArea {
            objectName: "customDwellPoints"
            width: parent.width
            visible: form.field("scan_pattern") === "custom"
            text: form.field("dwell_points")
            placeholderText: "-2, 0, 1\n2, 0, 1"
            wrapMode: TextEdit.Wrap
            selectByMouse: true
            onTextChanged: if (activeFocus) form.editor.setEmissionField("dwell_points", text)
        }
    }

    ThemeControls.ThemeLabel { text: "来源 / 校准状态"; font.bold: true }
    ThemeControls.ThemeComboBox {
        objectName: "sourceEmissionProvenanceStatus"
        width: parent.width
        model: [
            {text: "未验证 / 项目约定", value: "unvalidated"},
            {text: "理论模型参数", value: "theoretical"},
            {text: "用户提供实测校准", value: "calibrated"}
        ]
        textRole: "text"
        valueRole: "value"
        currentIndex: ["unvalidated", "theoretical", "calibrated"].indexOf(form.field("provenance_status"))
        onActivated: form.editor.setEmissionField("provenance_status", currentValue)
    }
    GridLayout {
        width: parent.width
        columns: width >= 2 * form.minimumFieldColumnWidth + columnSpacing ? 2 : 1
        columnSpacing: 8
        rowSpacing: 4
        SourceValueField {
            Layout.fillWidth: true
            compact: true
            compactFieldWidth: 100
            fieldName: "source_emission_provenance_reference"
            label: "来源 / 校准记录"
            numeric: false
            value: form.field("provenance_reference")
            onEdited: value => form.editor.setEmissionField("provenance_reference", value)
        }
        SourceValueField {
            Layout.fillWidth: true
            compact: true
            compactFieldWidth: 100
            fieldName: "source_emission_provenance_notes"
            label: "假设 / 缺失项"
            numeric: false
            value: form.field("provenance_notes")
            onEdited: value => form.editor.setEmissionField("provenance_notes", value)
        }
    }
    ThemeControls.ThemeLabel {
        objectName: "sourceEmissionCalibrationNote"
        width: parent.width
        wrapMode: Text.Wrap
        text: "设备响应系数只表达比例；无可靠来源时，不能据默认分布推断实测沉积。实际设备应以工艺标定为准。"
        opacity: 0.75
    }
}
