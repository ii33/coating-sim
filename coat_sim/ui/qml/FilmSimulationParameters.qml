import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

Rectangle {
    id: root
    property var controller
    property int columns: width < 660 ? 2 : 4
    property var draftSettings: ({})
    property int draftLightSourceIndex: 0
    property string samplingPriority: "step"
    property string draftError: ""
    implicitHeight: parameters.implicitHeight + Theme.snapshot.spaceMd * 2
    radius: Theme.snapshot.radiusMd
    color: Theme.snapshot.colorSurface
    border.color: Theme.snapshot.colorBorder

    function loadDraft() {
        draftSettings = JSON.parse(JSON.stringify(controller.settings))
        draftSettings.samples = controller.sampleCountText
        draftLightSourceIndex = controller.lightSourceIndex
        samplingPriority = "step"
        draftError = ""
        if (!Number.isSafeInteger(Number(draftSettings.samples)))
            draftError = "采样点数超出此界面的精确整数范围。"
    }
    function updateDraft(key, value, priority) {
        var next = JSON.parse(JSON.stringify(draftSettings))
        next[key] = value
        draftSettings = next
        draftError = ""
        if (priority)
            syncSamplingDraft(priority)
    }
    function syncSamplingDraft(priority) {
        var span = Number(draftSettings.wavelength_max_nm)
                  - Number(draftSettings.wavelength_min_nm)
        if (!Number.isFinite(span) || span <= 0)
            return

        var count
        if (priority === "samples") {
            var countText = String(draftSettings.samples).trim()
            count = Number(countText)
            if (!/^[0-9]+$/.test(countText) || !Number.isSafeInteger(count) || count < 2) {
                draftError = "采样点数须为此界面可精确表示且至少为 2 的整数。"
                return
            }
        } else {
            var step = Number(draftSettings.step_nm)
            var intervals = span / step
            if (!Number.isFinite(intervals) || step <= 0) {
                draftError = "采样步长必须为有限正数。"
                return
            }
            count = Math.floor(intervals + 0.5) + 1
            if (count < 2 || !Number.isSafeInteger(count)) {
                draftError = "该步长所需采样点数超出此界面的精确整数范围。"
                return
            }
        }

        var next = JSON.parse(JSON.stringify(draftSettings))
        next.samples = priority === "samples" ? String(draftSettings.samples).trim() : String(count)
        next.step_nm = span / (count - 1)
        draftSettings = next
        samplingPriority = priority
        draftError = ""
    }
    function setDraftLightSourceIndex(index) {
        var source = JSON.parse(JSON.stringify(controller.lightSourcePayload(index)))
        if (!source || !source.kind)
            return
        var next = JSON.parse(JSON.stringify(draftSettings))
        var previous = next.light_source || {}
        if (source.kind === "blackbody" && previous.kind === "blackbody"
                && Number.isFinite(Number(previous.temperature_k)))
            source.temperature_k = Number(previous.temperature_k)
        next.light_source = source
        draftSettings = next
        draftLightSourceIndex = index
        draftError = ""
    }
    function setDraftLightTemperature(value) {
        var next = JSON.parse(JSON.stringify(draftSettings))
        var source = next.light_source || {kind: "blackbody"}
        source.temperature_k = value
        next.light_source = source
        draftSettings = next
        draftError = ""
    }
    function draftPayload() {
        if (draftError.length > 0)
            return null
        var next = JSON.parse(JSON.stringify(draftSettings))
        var countText = String(next.samples).trim()
        var count = Number(countText)
        if (!/^[0-9]+$/.test(countText) || !Number.isSafeInteger(count) || count < 2) {
            draftError = "采样点数须为此界面可精确表示且至少为 2 的整数。"
            return null
        }
        // Count edits submit the exact integer. Independent step edits retain
        // the existing service precedence; derived equal steps are recognized
        // by the controller without a lossy count round-trip.
        if (samplingPriority === "samples")
            delete next.step_nm
        next.samples = countText
        return next
    }
    function applyDraft() {
        var next = draftPayload()
        if (!next)
            return false
        var applied = controller.setSimulationSettings(next)
        if (applied) {
            draftError = ""
        } else {
            draftError = controller.validationError || "参数无效。"
        }
        return applied
    }

    GridLayout {
        id: parameters
        anchors.fill: parent
        anchors.margins: Theme.snapshot.spaceMd
        columns: root.columns
        columnSpacing: Theme.snapshot.spaceMd
        rowSpacing: Theme.snapshot.spaceSm

        Label { text: "最小波长（nm）"; color: Theme.snapshot.colorText }
        ThemeControls.ThemeField {
            Layout.fillWidth: true
            accessibleLabel: "最小波长（nm）"
            numericDisplayValue: root.draftSettings.wavelength_min_nm
            inputMethodHints: Qt.ImhFormattedNumbersOnly
            validator: DoubleValidator { bottom: 0.000001; top: 1.0e12; decimals: 10 }
            onEditingFinished: if (numericValueEdited) root.updateDraft("wavelength_min_nm", Number(text), "step")
        }
        Label { text: "最大波长（nm）"; color: Theme.snapshot.colorText }
        ThemeControls.ThemeField {
            Layout.fillWidth: true
            accessibleLabel: "最大波长（nm）"
            numericDisplayValue: root.draftSettings.wavelength_max_nm
            inputMethodHints: Qt.ImhFormattedNumbersOnly
            validator: DoubleValidator { bottom: 0.000001; top: 1.0e12; decimals: 10 }
            onEditingFinished: if (numericValueEdited) root.updateDraft("wavelength_max_nm", Number(text), "step")
        }

        Label { text: "步长（nm）"; color: Theme.snapshot.colorText }
        ThemeControls.ThemeField {
            Layout.fillWidth: true
            accessibleLabel: "采样步长（nm）"
            numericDisplayValue: root.draftSettings.step_nm
            numericDisplayDecimals: 15
            inputMethodHints: Qt.ImhFormattedNumbersOnly
            validator: DoubleValidator {
                bottom: 0; top: 1.0e12; decimals: 15
                notation: DoubleValidator.ScientificNotation
            }
            onEditingFinished: if (numericValueEdited) root.updateDraft("step_nm", Number(text), "step")
        }
        Label { text: "采样点数"; color: Theme.snapshot.colorText }
        ThemeControls.ThemeField {
            Layout.fillWidth: true
            accessibleLabel: "采样点数"
            numericDisplayValue: root.draftSettings.samples
            inputMethodHints: Qt.ImhDigitsOnly
            validator: RegularExpressionValidator { regularExpression: /^[0-9]*$/ }
            onEditingFinished: if (numericValueEdited) root.updateDraft("samples", String(text), "samples")
        }

Label { text: "入射角（°）"; color: Theme.snapshot.colorText }
        ThemeControls.ThemeField {
            Layout.fillWidth: true
            accessibleLabel: "入射角（度）"
            numericDisplayValue: root.draftSettings.angle_deg
            inputMethodHints: Qt.ImhFormattedNumbersOnly
            validator: DoubleValidator { bottom: 0; top: 89.999999; decimals: 8 }
            onEditingFinished: if (numericValueEdited) root.updateDraft("angle_deg", Number(text))
        }
        Label { text: "偏振方式"; color: Theme.snapshot.colorText }
        ComboBox {
            Layout.fillWidth: true
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeBody
            palette.window: Theme.snapshot.colorWindow
            palette.windowText: Theme.snapshot.colorText
            palette.base: Theme.snapshot.colorSurface
            palette.alternateBase: Theme.snapshot.colorSurfaceRaised
            palette.text: Theme.snapshot.colorText
            palette.button: Theme.snapshot.colorSurfaceRaised
            palette.buttonText: Theme.snapshot.colorText
            palette.highlight: Theme.snapshot.colorSelection
            palette.highlightedText: Theme.snapshot.colorText
            palette.mid: Theme.snapshot.colorBorder
            model: [
                {"label": "非偏振", "value": "unpolarized"},
                {"label": "s 偏振", "value": "s"},
                {"label": "p 偏振", "value": "p"}
            ]
            textRole: "label"
            valueRole: "value"
            currentIndex: root.draftSettings.polarization === "s" ? 1
                          : root.draftSettings.polarization === "p" ? 2 : 0
            onActivated: root.updateDraft("polarization", currentValue)
        }

        Label { text: "首个入射面"; color: Theme.snapshot.colorText }
        ComboBox {
            Layout.fillWidth: true
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeBody
            palette.window: Theme.snapshot.colorWindow
            palette.windowText: Theme.snapshot.colorText
            palette.base: Theme.snapshot.colorSurface
            palette.alternateBase: Theme.snapshot.colorSurfaceRaised
            palette.text: Theme.snapshot.colorText
            palette.button: Theme.snapshot.colorSurfaceRaised
            palette.buttonText: Theme.snapshot.colorText
            palette.highlight: Theme.snapshot.colorSelection
            palette.highlightedText: Theme.snapshot.colorText
            palette.mid: Theme.snapshot.colorBorder
            model: [
                {"label": "正面先入射", "value": "front"},
                {"label": "背面先入射", "value": "back"}
            ]
            textRole: "label"
            valueRole: "value"
            currentIndex: root.draftSettings.first_surface === "back" ? 1 : 0
            onActivated: root.updateDraft("first_surface", currentValue)
        }
        Label { text: "光源"; color: Theme.snapshot.colorText }
        ComboBox {
            Layout.fillWidth: true
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeBody
            palette.window: Theme.snapshot.colorWindow
            palette.windowText: Theme.snapshot.colorText
            palette.base: Theme.snapshot.colorSurface
            palette.alternateBase: Theme.snapshot.colorSurfaceRaised
            palette.text: Theme.snapshot.colorText
            palette.button: Theme.snapshot.colorSurfaceRaised
            palette.buttonText: Theme.snapshot.colorText
            palette.highlight: Theme.snapshot.colorSelection
            palette.highlightedText: Theme.snapshot.colorText
            palette.mid: Theme.snapshot.colorBorder
            model: root.controller.lightSources
            textRole: "label"
            currentIndex: root.draftLightSourceIndex
            onActivated: root.setDraftLightSourceIndex(index)
        }
        Label { text: "黑体温度（K）"; color: Theme.snapshot.colorText }
        ThemeControls.ThemeField {
            Layout.fillWidth: true
            enabled: !!root.draftSettings.light_source
                     && root.draftSettings.light_source.kind === "blackbody"
            accessibleLabel: "黑体温度（K）"
            numericDisplayValue: (root.draftSettings.light_source || {}).temperature_k || 5778
            inputMethodHints: Qt.ImhFormattedNumbersOnly
            validator: DoubleValidator { bottom: 1; top: 1.0e7; decimals: 6 }
            onEditingFinished: if (numericValueEdited) root.setDraftLightTemperature(Number(text))
        }

        Label { text: "n 超出数据域"; color: Theme.snapshot.colorText }
        ComboBox {
            Layout.fillWidth: true
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeBody
            palette.window: Theme.snapshot.colorWindow
            palette.windowText: Theme.snapshot.colorText
            palette.base: Theme.snapshot.colorSurface
            palette.alternateBase: Theme.snapshot.colorSurfaceRaised
            palette.text: Theme.snapshot.colorText
            palette.button: Theme.snapshot.colorSurfaceRaised
            palette.buttonText: Theme.snapshot.colorText
            palette.highlight: Theme.snapshot.colorSelection
            palette.highlightedText: Theme.snapshot.colorText
            palette.mid: Theme.snapshot.colorBorder
            model: [
                {"label": "最近有效端点保持", "value": "endpoint_hold"},
                {"label": "超出范围时报错", "value": "error"}
            ]
            textRole: "label"
            valueRole: "value"
            currentIndex: root.draftSettings.extrapolation_policy === "error" ? 1 : 0
            onActivated: root.updateDraft("extrapolation_policy", currentValue)
        }
        Label {
            Layout.columnSpan: Math.min(parameters.columns, 2)
            Layout.fillWidth: true
            text: "平坦、黑体及 CIE 表格光源；点数与步长互相换算，波段改动优先保留步长。"
            color: Theme.snapshot.colorTextMuted
            wrapMode: Text.WordWrap
        }
        Label {
            Layout.columnSpan: parameters.columns
            Layout.fillWidth: true
            text: root.controller.lightSourceNotes
            color: Theme.snapshot.colorTextMuted
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeSmall
            wrapMode: Text.WordWrap
        }
        Label {
            Layout.columnSpan: parameters.columns
            Layout.fillWidth: true
            text: root.controller.resourceEstimate
            color: Theme.snapshot.colorTextMuted
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeSmall
            wrapMode: Text.WordWrap
        }
        Label {
            Layout.columnSpan: parameters.columns
            Layout.fillWidth: true
            visible: root.draftError.length > 0
            text: root.draftError
            color: Theme.snapshot.colorError
            wrapMode: Text.WordWrap
        }
    }
}
