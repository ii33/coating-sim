import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "."
import "theme_controls" as ThemeControls

AppWindow {
    id: window
    property var settingsBridge: null
    titleBase: "设置"
    statusText: bridge.statusMessage
    statusSeverity: "info"
    statusDetail: bridge.statusMessage
    width: 960
    height: 760
    minimumWidth: 560
    minimumHeight: 420
    onClosing: {
        bridge.closeModelProbe()
        bridge.closeAssistantSettingsBootstrap()
        bridge.closeMcpConnections()
    }
    onVisibleChanged: if (visible) bridge.resumeModelProbe()

    component Category: ThemeControls.ThemeSection {
        id: category
        property real sectionAvailableWidth: Math.max(0, width - 2 * Theme.snapshot.spaceMd)
        property var settingsBridge: null
        property var fontFamilyFieldControl: null
    }

    component ComputeServerSettingsForm: Item {
        id: computeForm
        property var settingsBridge: null
        width: parent ? parent.width : 0
        implicitHeight: computeServerFields.implicitHeight

        function restoreFields() {
            if (!computeForm.settingsBridge)
                return
            computeServerUrlField.text = computeForm.settingsBridge.computeServerUrl
            computeServerTlsCheck.checked = computeForm.settingsBridge.computeServerTls
            computeServerCaField.text = computeForm.settingsBridge.computeServerCaFile
            computeServerTokenField.text = ""
        }
        function saveSettingsDraft() {
            if (!computeForm.settingsBridge)
                return
            computeForm.settingsBridge.setComputeServerSettings(
                computeServerUrlField.text,
                computeServerTlsCheck.checked,
                computeServerCaField.text)
        }
        ColumnLayout {
            id: computeServerFields
            width: parent.width
            spacing: 8
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: "地址留空时使用本机计算。远程服务器需填写单一地址；TLS 默认开启。此页不会自动探测连接。"
            }
            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeLabel { text: "服务器地址"; Layout.preferredWidth: 90 }
                ThemeControls.ThemeField {
                    id: computeServerUrlField
                    objectName: "computeServerUrlField"
                    Layout.fillWidth: true
                    text: computeForm.settingsBridge
                          ? computeForm.settingsBridge.computeServerUrl : ""
                    placeholderText: "留空使用本机；远程示例：https://compute.example"
                    selectByMouse: true
                    Accessible.name: "计算服务器地址，留空使用本机计算"
                }
            }
            CheckBox {
                id: computeServerTlsCheck
                objectName: "computeServerTlsCheck"
                text: "启用 TLS 证书验证"
                checked: computeForm.settingsBridge
                         ? computeForm.settingsBridge.computeServerTls : true
                Accessible.name: "启用计算服务器 TLS 证书验证"
            }
            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeLabel { text: "自定义 CA 文件"; Layout.preferredWidth: 90 }
                ThemeControls.ThemeField {
                    id: computeServerCaField
                    objectName: "computeServerCaField"
                    Layout.fillWidth: true
                    text: computeForm.settingsBridge
                          ? computeForm.settingsBridge.computeServerCaFile : ""
                    placeholderText: "可选；留空使用系统证书库"
                    selectByMouse: true
                    Accessible.name: "可选的计算服务器 CA 文件路径"
                }
            }
            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeLabel { text: "访问令牌"; Layout.preferredWidth: 90 }
                ThemeControls.ThemeField {
                    id: computeServerTokenField
                    objectName: "computeServerTokenField"
                    Layout.fillWidth: true
                    echoMode: TextInput.Password
                    placeholderText: computeForm.settingsBridge
                                     && computeForm.settingsBridge.computeServerHasToken
                                     ? "令牌已安全保存；输入以替换"
                                     : "输入服务端提供的令牌"
                    Accessible.name: "计算服务器访问令牌，不在项目文件中保存"
                }
                ThemeControls.ThemeButton {
                    objectName: "saveComputeServerTokenButton"
                    text: "保存令牌"
                    enabled: computeForm.settingsBridge !== null
                             && computeServerTokenField.text.length > 0
                             && computeServerUrlField.text.trim() === computeForm.settingsBridge.computeServerUrl
                             && computeForm.settingsBridge.computeServerUrl.length > 0
                    onClicked: {
                        if (computeForm.settingsBridge
                                && computeForm.settingsBridge.saveComputeServerToken(computeServerTokenField.text))
                            computeServerTokenField.text = ""
                    }
                }
                ThemeControls.ThemeButton {
                    objectName: "deleteComputeServerTokenButton"
                    text: "删除"
                    enabled: computeForm.settingsBridge
                             ? computeForm.settingsBridge.computeServerHasToken : false
                    onClicked: if (computeForm.settingsBridge)
                        computeForm.settingsBridge.deleteComputeServerToken()
                }
            }
            ThemeControls.ThemeLabel {
                objectName: "computeServerTokenStatus"
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: (computeForm.settingsBridge
                       ? computeForm.settingsBridge.computeServerTokenStatus : "令牌状态待检查。")
                      + " 令牌只保存在系统安全凭据库；连接状态尚未检查。"
                color: palette.placeholderText
            }
        }
    }

    component ThemedItemDelegate: ItemDelegate {
        id: item
        hoverEnabled: true
        focusPolicy: Qt.StrongFocus
        font.family: Theme.snapshot.fontFamily
        font.pointSize: Theme.snapshot.fontSizeBody
        padding: Theme.snapshot.spaceSm
        contentItem: ThemeControls.ThemeLabel {
            text: item.text
            font: item.font
            color: !item.enabled ? Theme.snapshot.colorDisabledText
                                 : item.highlighted ? Theme.snapshot.colorTextOnAccent
                                                    : Theme.snapshot.colorText
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            radius: Theme.snapshot.radiusSm
            color: !item.enabled ? Theme.snapshot.colorDisabledSurface
                  : item.down || item.highlighted ? Theme.snapshot.colorAccent
                  : item.hovered ? Theme.snapshot.colorSurfaceRaised
                                 : Theme.snapshot.colorSurface
            border.width: item.activeFocus ? Theme.snapshot.focusRingWidth : Theme.snapshot.borderWidth
            border.color: item.activeFocus ? Theme.snapshot.colorFocusRing
                         : item.hovered && item.enabled ? Theme.snapshot.colorBorderStrong
                                                        : Theme.snapshot.colorBorder
        }
    }

    // Shared widths keep label/input pairs aligned across both numeric sections.
    readonly property int numericLabelWidth: Math.ceil(numericLabelMetrics.width) + 8
    readonly property int numericControlWidth: Math.max(156, Math.ceil(numericInputMetrics.width) + 64)

    TextMetrics {
        id: numericLabelMetrics
        text: "上下文容量 / K"
        font.pointSize: bridge.fontPointSize
    }
    TextMetrics {
        id: numericInputMetrics
        text: "2,000.999"
        font.pointSize: bridge.fontPointSize
    }

    component NumericGrid: GridLayout {
        required property real availableContentWidth
        Layout.fillWidth: true
        columnSpacing: 18
        rowSpacing: 8
        readonly property int pairWidth: window.numericLabelWidth + window.numericControlWidth + 10
        // Measure the allocated category, never this layout's own implicit size.
        columns: availableContentWidth >= 3 * pairWidth + 2 * columnSpacing ? 3
               : availableContentWidth >= 2 * pairWidth + columnSpacing ? 2 : 1
        readonly property real cellAvailableWidth: Math.max(0,
            (availableContentWidth - (columns - 1) * columnSpacing) / columns)
    }

    component NumericField: GridLayout {
        required property real availableContentWidth
        Layout.alignment: Qt.AlignLeft | Qt.AlignTop
        columnSpacing: 10
        rowSpacing: 4
        // A field stays together even when a large font requires stacking.
        columns: availableContentWidth >= window.numericLabelWidth
                                          + window.numericControlWidth + columnSpacing ? 2 : 1
    }

    component NumericLabel: ThemeControls.ThemeLabel {
        font.pointSize: bridge.fontPointSize
        Layout.minimumWidth: window.numericLabelWidth
        Layout.preferredWidth: window.numericLabelWidth
        Layout.maximumWidth: window.numericLabelWidth
        Layout.alignment: Qt.AlignLeft | Qt.AlignVCenter
        wrapMode: Text.WordWrap
    }

    // Values remain integer Tokens. Only their presentation is divided by 1000.
    component TokenSpin: ThemeControls.ThemeSpinBox {
        id: tokenControl
        from: 0
        to: 2000000
        stepSize: 1000
        editable: true
        Layout.minimumWidth: window.numericControlWidth
        Layout.preferredWidth: window.numericControlWidth
        Layout.maximumWidth: window.numericControlWidth
        Layout.alignment: Qt.AlignLeft | Qt.AlignVCenter
        validator: DoubleValidator {
            bottom: 0
            top: tokenControl.to / 1000
            decimals: 3
            notation: DoubleValidator.StandardNotation
            locale: tokenControl.locale.name
        }
        inputMethodHints: Qt.ImhFormattedNumbersOnly
        textFromValue: function(value, locale) {
            const formatted = Number(value / 1000).toLocaleString(locale, "f", 3)
            const split = formatted.lastIndexOf(locale.decimalPoint)
            if (split < 0) return formatted
            const fraction = formatted.slice(split + 1).replace(/0+$/, "")
            return fraction.length ? formatted.slice(0, split + 1) + fraction
                                   : formatted.slice(0, split)
        }
        valueFromText: function(text, locale) {
            if (text.trim().length === 0) return 0
            const count = Number.fromLocaleString(locale, text)
            return isFinite(count) ? Math.round(count * 1000) : tokenControl.value
        }
    }

    function syncModelLimits() {
        if (settingsContentLoader.item)
            settingsContentLoader.item.syncModelLimits()
    }
    function saveModelLimits() {
        if (settingsContentLoader.item)
            settingsContentLoader.item.saveModelLimits()
    }
    function saveContextProfile() {
        if (settingsContentLoader.item)
            settingsContentLoader.item.saveContextProfile()
    }
    function kTokenText(value) {
        const count = Number(value)
        return isFinite(count)
             ? (count / 1000).toLocaleString(Qt.locale(), "f", 3) + " K"
             : "未知"
    }
    function knownKTokenText(value) {
        return Number(value) > 0 ? window.kTokenText(value) : "未知"
    }
    function syncAssistantTone() {
        if (settingsContentLoader.item)
            settingsContentLoader.item.syncAssistantTone()
    }
    function restoreFields() {
        if (settingsContentLoader.item)
            settingsContentLoader.item.restoreFields()
    }

    FileDialog {
        id: themePackageDialog
        objectName: "themePackagePicker"
        title: "安装本地主题包"
        fileMode: FileDialog.OpenFile
        nameFilters: ["CoatSim 主题包 (*.zip)"]
        onAccepted: {
            const path = window.settingsBridge.localFilePath(selectedFile)
            if (path.length > 0) window.settingsBridge.installThemePackage(path)
        }
    }

    property string mcpExpandedId: ""
    property string mcpEditPreset: ""
    property string mcpEditDescription: ""
    property string mcpEditId: ""
    property bool mcpHasCredential: false
    function editMcpServer(row) {
        if (settingsContentLoader.item)
            settingsContentLoader.item.editMcpServer(row)
    }

    Connections {
        target: bridge
        Component.onCompleted: window.settingsBridge = target
        function onAssistantSettingsReloaded() { window.restoreFields() }
        function onAssistantStateChanged() {
            window.syncModelLimits()
            window.syncAssistantTone()
        }
    }

    Loader {
        id: settingsContentLoader
        objectName: "settingsContentLoader"
        anchors.fill: parent
        asynchronous: false
        sourceComponent: Component {
            Item {
                id: settingsContent
                anchors.fill: parent
                property var fontSpinControl: appearanceSettingsLoader.item ? appearanceSettingsLoader.item.fontSpinControl : null
                property var dpiSpinControl: appearanceSettingsLoader.item ? appearanceSettingsLoader.item.dpiSpinControl : null
                property var densitySpinControl: appearanceSettingsLoader.item ? appearanceSettingsLoader.item.densitySpinControl : null
                property var themeModeComboControl: appearanceSettingsLoader.item ? appearanceSettingsLoader.item.themeModeComboControl : null
                property alias assistantSwitchControl: assistantSwitch
                property alias userNameFieldControl: userNameField
                property alias assistantToneComboControl: assistantToneCombo
                property alias iconSizeSpinControl: iconSizeSpin
                property alias assistantNotificationVolumeSpinControl: assistantNotificationVolumeSpin
                property var settingsWindow: window
                property var settingsBridge: settingsWindow.settingsBridge
                property var appearanceForm: appearanceSettingsLoader.item as Category
                onSettingsBridgeChanged: {
                    if (settingsContent.appearanceForm)
                        settingsContent.appearanceForm.settingsBridge = settingsContent.settingsBridge
                }
                property bool hasPendingMcpEdit: false
                property var pendingMcpEditRow: null
                property var assistantUrlFieldControl: settingsAdvancedLoader.item
                    ? settingsAdvancedLoader.item.assistantUrlFieldControl : null
                property var assistantModelFieldControl: settingsAdvancedLoader.item
                    ? settingsAdvancedLoader.item.assistantModelFieldControl : null
                property var contextWindowTokensSpinControl: settingsAdvancedLoader.item
                    ? settingsAdvancedLoader.item.contextWindowTokensSpinControl : null
                property var reservedOutputTokensSpinControl: settingsAdvancedLoader.item
                    ? settingsAdvancedLoader.item.reservedOutputTokensSpinControl : null
                property var compressionReserveTokensSpinControl: settingsAdvancedLoader.item
                    ? settingsAdvancedLoader.item.compressionReserveTokensSpinControl : null
                property var historyKeepTokensSpinControl: settingsAdvancedLoader.item
                    ? settingsAdvancedLoader.item.historyKeepTokensSpinControl : null
                property var contextCompactionThresholdSpinControl: settingsAdvancedLoader.item
                    ? settingsAdvancedLoader.item.contextCompactionThresholdSpinControl : null
                property var dailyTokenSpinControl: settingsAdvancedLoader.item
                    ? settingsAdvancedLoader.item.dailyTokenSpinControl : null
                property var periodTokenSpinControl: settingsAdvancedLoader.item
                    ? settingsAdvancedLoader.item.periodTokenSpinControl : null
                property var periodMinutesSpinControl: settingsAdvancedLoader.item
                    ? settingsAdvancedLoader.item.periodMinutesSpinControl : null
                property var taskModeComboControl: settingsAdvancedLoader.item
                    ? settingsAdvancedLoader.item.taskModeComboControl : null
                property var reasoningComboControl: settingsAdvancedLoader.item
                    ? settingsAdvancedLoader.item.reasoningComboControl : null
                function syncModelLimits() {
                    if (settingsAdvancedLoader.item)
                        settingsAdvancedLoader.item.syncModelLimits()
                }
                function saveModelLimits() {
                    if (settingsAdvancedLoader.item)
                        settingsAdvancedLoader.item.saveModelLimits()
                }
                function saveContextProfile() {
                    if (settingsAdvancedLoader.item)
                        settingsAdvancedLoader.item.saveContextProfile()
                }
                function syncAssistantTone() {
                    const index = ["serious", "standard", "lively", "intimate"].indexOf(bridge.assistantTone)
                    assistantToneCombo.currentIndex = index >= 0 ? index : 1
                }
                function restoreFields() {
                    if (appearanceSettingsLoader.item)
                        appearanceSettingsLoader.item.restoreFields()
                    const computeForm = computeServerSettingsLoader.item as ComputeServerSettingsForm
                    if (computeForm)
                        computeForm.restoreFields()
                    assistantSwitch.checked = bridge.assistantEnabled
                    assistantNotificationVolumeSpin.value = bridge.assistantNotificationVolumePercent
                    userNameField.text = bridge.assistantUserName
                    iconSizeSpin.value = bridge.assistantIconSize
                    syncAssistantTone()
                    if (settingsAdvancedLoader.item)
                        settingsAdvancedLoader.item.restoreFields()
                }
                function updateAssistantConfig(enabled, apiUrl, model) {
                    const advancedForm = settingsAdvancedLoader.item
                    const resolvedUrl = apiUrl === undefined
                        ? (advancedForm ? advancedForm.assistantUrlFieldControl.text : bridge.assistantApiUrl)
                        : apiUrl
                    const resolvedModel = model === undefined
                        ? (advancedForm ? advancedForm.assistantModelFieldControl.text : bridge.assistantModel)
                        : model
                    bridge.setAssistantConfig(enabled, resolvedUrl, resolvedModel)
                }
                function editMcpServer(row) {
                    pendingMcpEditRow = row
                    hasPendingMcpEdit = true
                    advancedSettingsCategory.expanded = true
                    if (settingsAdvancedLoader.status === Loader.Ready)
                        dispatchPendingMcpEdit()
                }
                function dispatchPendingMcpEdit() {
                    if (!hasPendingMcpEdit || !settingsAdvancedLoader.item)
                        return
                    hasPendingMcpEdit = false
                    settingsAdvancedLoader.item.editMcpServer(pendingMcpEditRow)
                    pendingMcpEditRow = null
                }
                function saveSettingsDraft() {
                    const advancedForm = settingsAdvancedLoader.item
                    const appearance = settingsContent.appearanceForm
                    if (appearance) {
                        bridge.setFontPointSize(appearance.fontSpinControl.value)
                        bridge.setDpiScalePercent(appearance.dpiSpinControl.value)
                        bridge.setDensityScalePercent(appearance.densitySpinControl.value)
                        bridge.setThemeMode(appearance.themeModeComboControl.valueAt(
                            appearance.themeModeComboControl.currentIndex))
                        if (appearance.settingsBridge)
                            appearance.settingsBridge.setFontFamily(
                                appearance.fontFamilyFieldControl.text.trim())
                    }
                    updateAssistantConfig(assistantSwitch.checked)
                    bridge.setAssistantIconSize(iconSizeSpin.value)
                    bridge.setAssistantNotificationVolumePercent(assistantNotificationVolumeSpin.value)
                    bridge.setAssistantUserName(userNameField.text)
                    bridge.setAssistantTone(assistantToneCombo.currentValue || "standard")
                    const computeForm = computeServerSettingsLoader.item as ComputeServerSettingsForm
                    if (computeForm)
                        computeForm.saveSettingsDraft()
                    if (advancedForm)
                        advancedForm.saveSettingsDraft()
                    bridge.save()
                }
                Component.onCompleted: restoreFields()
                ScrollView {
        id: settingsScroll
        objectName: "settingsScroll"
        palette: window.palette
        anchors.fill: parent
        anchors.topMargin: window.contentTopMargin
        anchors.bottomMargin: saveActionBar.implicitHeight + 2 * Theme.snapshot.spaceMd
        clip: true
        contentWidth: availableWidth

        ColumnLayout {
            x: Math.max(16, (settingsScroll.availableWidth - width) / 2)
            y: 12
            width: Math.max(0, Math.min(1180, settingsScroll.availableWidth - 32))
            spacing: 12

            GridLayout {
                Layout.fillWidth: true
                columns: width >= 760 ? 2 : 1
                columnSpacing: 12
                rowSpacing: 12

            Loader {
                id: appearanceSettingsLoader
                objectName: "settingsAppearanceLoader"
                Layout.fillWidth: true
                Layout.preferredHeight: item ? item.implicitHeight : 0
                asynchronous: false
                onLoaded: {
                    if (settingsContent.appearanceForm) {
                        settingsContent.appearanceForm.settingsBridge = settingsContent.settingsBridge
                        settingsContent.appearanceForm.restoreFields()
                    }
                }
                sourceComponent: Component {
                    Category {
                        id: appearanceCategory
                        property alias fontSpinControl: fontSpin
                        property alias dpiSpinControl: dpiSpin
                        property alias densitySpinControl: densitySpin
                        property alias themeModeComboControl: themeModeCombo
                        fontFamilyFieldControl: fontFamilyField
                        function restoreFields() {
                            fontSpin.value = bridge.fontPointSize
                            dpiSpin.value = bridge.dpiScalePercent
                            densitySpin.value = bridge.densityScalePercent
                            if (appearanceCategory.settingsBridge)
                                fontFamilyField.text = appearanceCategory.settingsBridge.fontFamily
                            let modeIndex = 0
                            for (let index = 0; index < themeModeCombo.count; ++index) {
                                if (themeModeCombo.valueAt(index) === bridge.themeMode) {
                                    modeIndex = index
                                    break
                                }
                            }
                            themeModeCombo.currentIndex = modeIndex
                        }

                        title: "界面与显示"
                        Layout.fillWidth: true
                        Layout.alignment: Qt.AlignTop
                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 8
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "主题" }
                                ThemeControls.ThemeComboBox {
                                    id: themeModeCombo
                                    Layout.fillWidth: true
                                    textRole: "label"
                                    valueRole: "value"
                                    model: ListModel {
                                        ListElement { label: "跟随系统"; value: "system" }
                                        ListElement { label: "浅色"; value: "light" }
                                        ListElement { label: "深色"; value: "dark" }
                                        ListElement { label: "高对比度浅色"; value: "high-contrast-light" }
                                        ListElement { label: "高对比度深色"; value: "high-contrast-dark" }
                                    }
                                    currentIndex: {
                                        const selectedMode = bridge.themeMode
                                        for (let index = 0; index < count; ++index) {
                                            if (valueAt(index) === selectedMode)
                                                return index
                                        }
                                        return 0
                                    }
                                    onActivated: function(index) {
                                        bridge.setThemeMode(valueAt(index))
                                    }
                                    Accessible.name: "颜色主题"
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "主题包" }
                                ThemeControls.ThemeComboBox {
                                    id: themePackageCombo
                                    Layout.fillWidth: true
                                    textRole: "name"
                                    valueRole: "id"
                                    model: bridge.themePackages
                                    currentIndex: {
                                        for (let index = 0; index < count; ++index) {
                                            if (valueAt(index) === bridge.themePackageId)
                                                return index
                                        }
                                        return 0
                                    }
                                    onActivated: function(index) {
                                        bridge.setThemePackage(valueAt(index))
                                    }
                                    Accessible.name: "选择外观主题包"
                                }
                                ThemeControls.ThemeButton {
                                    text: "安装 ZIP"
                                    onClicked: themePackageDialog.open()
                                    Accessible.name: "安装本地主题包 ZIP 文件"
                                }
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                visible: text.length > 0
                                text: bridge.themePackageStatus
                                wrapMode: Text.WordWrap
                                color: window.themeColors ? window.themeColors.colorTextMuted : window.palette.placeholderText
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                text: "主题包的浅色/深色资源随上方模式切换；系统模式跟随系统。保存后应用所选主题。"
                                wrapMode: Text.WordWrap
                                color: window.themeColors ? window.themeColors.colorTextMuted : window.palette.placeholderText
                            }
                            GridLayout {
                                Layout.fillWidth: true
                                columns: width >= 480 ? 4 : 2
                                columnSpacing: 8
                                ThemeControls.ThemeLabel { text: "字号 / pt" }
                                ThemeControls.ThemeSpinBox {
                                    id: fontSpin
                                    from: 8; to: 32
                                    editable: true
                                    value: bridge.fontPointSize
                                    onValueModified: bridge.setFontPointSize(value)
                                    Accessible.name: "默认字号，单位 pt"
                                }
                                ThemeControls.ThemeLabel { text: "缩放 / %" }
                                ThemeControls.ThemeSpinBox {
                                    id: dpiSpin
                                    from: 75; to: 200; stepSize: 5
                                    editable: true
                                    value: bridge.dpiScalePercent
                                    onValueModified: bridge.setDpiScalePercent(value)
                                    Accessible.name: "DPI 界面缩放百分比"
                                }
                                ThemeControls.ThemeLabel { text: "控件间距 / %" }
                                ThemeControls.ThemeSpinBox {
                                    id: densitySpin
                                    from: 75; to: 150; stepSize: 5
                                    editable: true
                                    value: bridge.densityScalePercent
                                    onValueModified: bridge.setDensityScalePercent(value)
                                    Accessible.name: "控件间距与密度百分比"
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8
                                ThemeControls.ThemeLabel { text: "字体" }
                                ThemeControls.ThemeField {
                                    id: fontFamilyField
                                    objectName: "fontFamilyField"
                                    Layout.fillWidth: true
                                    text: appearanceCategory.settingsBridge
                                          ? appearanceCategory.settingsBridge.fontFamily : ""
                                    placeholderText: "留空使用 Noto 或所选主题字体"
                                    maximumLength: 128
                                    Accessible.name: "界面字体族"
                                    onEditingFinished: {
                                        if (appearanceCategory.settingsBridge)
                                            appearanceCategory.settingsBridge.setFontFamily(text.trim())
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel {
                                text: "所有模块共用；留空使用 Noto 或所选主题字体，填写字体族名称可覆盖主题包。字号和控件间距即时更新，DPI 缩放更改后需重新打开程序。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                color: window.themeColors ? window.themeColors.colorTextMuted : window.palette.placeholderText
                            }
                        }
                    }
                }
            }

                Category {
                    id: identityCategory
                    title: "看板娘与身份"
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 8
                        RowLayout {
                            Layout.fillWidth: true
                            Switch {
                                id: assistantSwitch
                                text: "启用看板娘"
                                checked: bridge.assistantEnabled
                                onToggled: settingsContent.updateAssistantConfig(checked)
                            }
                            Item { Layout.fillWidth: true }
                            ThemeControls.ThemeLabel { text: "浮标 / px" }
                            ThemeControls.ThemeSpinBox {
                                id: iconSizeSpin
                                from: 48; to: 256; stepSize: 8
                                value: bridge.assistantIconSize
                                editable: true
                                Layout.preferredWidth: 110
                                onValueModified: bridge.setAssistantIconSize(value)
                                Accessible.name: "看板娘悬浮图标大小，单位 px"
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            ThemeControls.ThemeLabel { text: "通知音量 / %" }
                            ThemeControls.ThemeSpinBox {
                                id: assistantNotificationVolumeSpin
                                objectName: "assistantNotificationVolumeSpin"
                                from: 0
                                to: 100
                                stepSize: 5
                                value: bridge.assistantNotificationVolumePercent
                                editable: true
                                Layout.preferredWidth: 110
                                onValueModified: bridge.setAssistantNotificationVolumePercent(value)
                                Accessible.name: "看板娘通知音量，单位百分比"
                            }
                            ThemeControls.ThemeButton {
                                objectName: "assistantNotificationSoundPreview"
                                text: "试听"
                                onClicked: bridge.previewAssistantNotificationSound()
                            }
                            Item { Layout.fillWidth: true }
                        }
                        GridLayout {
                            Layout.fillWidth: true
                            columns: identityCategory.sectionAvailableWidth >= 520 ? 2 : 1
                            columnSpacing: 12
                            rowSpacing: 6
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "用户称呼" }
                                ThemeControls.ThemeField {
                                    id: userNameField
                                    Layout.fillWidth: true
                                    text: bridge.assistantUserName
                                    placeholderText: "用户"
                                    maximumLength: 40
                                    selectByMouse: true
                                    onTextEdited: bridge.setAssistantUserName(text)
                                    Accessible.name: "对话中的用户名"
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "交流语气" }
                                ThemeControls.ThemeComboBox {
                                    id: assistantToneCombo
                                    objectName: "assistantToneCombo"
                                    Layout.preferredWidth: 140
                                    model: [
                                        {text: "严肃", value: "serious"},
                                        {text: "标准", value: "standard"},
                                        {text: "轻快", value: "lively"},
                                        {text: "亲密", value: "intimate"}
                                    ]
                                    textRole: "text"
                                    valueRole: "value"
                                    onActivated: bridge.setAssistantTone(currentValue)
                                    Accessible.name: "看板娘交流语气"
                                    ToolTip.visible: hovered
                                    ToolTip.text: "严肃稳重、标准自然、轻快有分寸、亲密多些感情和闲聊；只改变表达，不改变权限或计算边界。"
                                }
                            }
                        }
                    }
                }
            }

            Category {
                id: computeServerCategory
                objectName: "computeServerSettingsCategory"
                title: "计算服务器连接"
                Layout.fillWidth: true
                expanded: false
                property bool contentRequested: false
                onExpandedChanged: if (expanded) {
                    settingsContent.settingsBridge.refreshComputeServerTokenStatus()
                    contentRequested = true
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    Loader {
                        id: computeServerSettingsLoader
                        objectName: "computeServerSettingsLoader"
                        Layout.fillWidth: true
                        asynchronous: false
                        active: computeServerCategory.contentRequested
                        onLoaded: {
                            const computeForm = item as ComputeServerSettingsForm
                            if (!computeForm)
                                return
                            computeForm.settingsBridge = settingsContent.settingsBridge
                            computeForm.restoreFields()
                        }
                        sourceComponent: Component {
                            ComputeServerSettingsForm { }
                        }
                    }
                }
            }

            Category {
                id: advancedSettingsCategory
                objectName: "settingsAdvancedCategory"
                title: "模型、用量与连接"
                Layout.fillWidth: true
                expanded: false
                property bool contentRequested: false
                onExpandedChanged: if (expanded) {
                    bridge.prepareAssistantSettings()
                    contentRequested = true
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    Loader {
                        id: settingsAdvancedLoader
                        objectName: "settingsAdvancedSectionsLoader"
                        Layout.fillWidth: true
                        asynchronous: false
                        active: advancedSettingsCategory.contentRequested
                        onLoaded: {
                            item.settingsPage = settingsContent
                            settingsContent.dispatchPendingMcpEdit()
                        }
                        sourceComponent: Component {
                            Item {
                                id: advancedSettingsForm
                                property var settingsPage: null
                                property alias assistantUrlFieldControl: assistantUrlField
                                property alias assistantModelFieldControl: assistantModelField
                                property alias contextWindowTokensSpinControl: contextWindowTokensSpin
                                property alias reservedOutputTokensSpinControl: reservedOutputTokensSpin
                                property alias compressionReserveTokensSpinControl: compressionReserveTokensSpin
                                property alias historyKeepTokensSpinControl: historyKeepTokensSpin
                                property alias contextCompactionThresholdSpinControl: contextCompactionThresholdSpin
                                property alias dailyTokenSpinControl: dailyTokenSpin
                                property alias periodTokenSpinControl: periodTokenSpin
                                property alias periodMinutesSpinControl: periodMinutesSpin
                                property alias taskModeComboControl: taskModeCombo
                                property alias reasoningComboControl: reasoningCombo
                                width: settingsAdvancedLoader.width
                                implicitHeight: advancedSections.implicitHeight

                                function syncModelLimits() {
                                    const values = bridge.assistantContextProfileOverrides
                                    contextWindowTokensSpin.value = values.context_window_tokens || 0
                                    reservedOutputTokensSpin.value = values.reserved_output_tokens || 0
                                    compressionReserveTokensSpin.value = values.compression_reserve_tokens || 0
                                    historyKeepTokensSpin.value = values.history_keep_tokens || 0
                                    contextCompactionThresholdSpin.value = values.compaction_threshold_percent || 0
                                }
                                function saveModelLimits() {
                                    saveContextProfile()
                                }
                                function saveContextProfile() {
                                    bridge.setAssistantContextProfile(
                                        contextWindowTokensSpin.value,
                                        reservedOutputTokensSpin.value,
                                        compressionReserveTokensSpin.value,
                                        historyKeepTokensSpin.value,
                                        contextCompactionThresholdSpin.value)
                                }
                                function restoreFields() {
                                    assistantUrlField.text = bridge.assistantApiUrl
                                    assistantModelField.text = bridge.assistantModel
                                    dailyTokenSpin.value = bridge.assistantDailyTokenLimit
                                    periodTokenSpin.value = bridge.assistantPeriodTokenLimit
                                    periodMinutesSpin.value = bridge.assistantPeriodMinutes
                                    syncModelLimits()
                                }
                                function editMcpServer(row) {
                                    settingsPage.settingsWindow.mcpEditId = row ? row.id : ""
                                    settingsPage.settingsWindow.mcpHasCredential = row ? row.credential_revision.length > 0 : false
                                    mcpName.text = row ? row.name : ""
                                    mcpTransport.currentIndex = row ? ["stdio", "http", "sse"].indexOf(row.transport) : 0
                                    mcpCommand.text = row ? row.command : ""
                                    mcpArgs.text = row ? JSON.stringify(row.args) : "[]"
                                    mcpCwd.text = row ? row.cwd : ""
                                    mcpUrl.text = row ? row.url : ""
                                    mcpEnabled.checked = row ? row.enabled : false
                                    mcpAllowTools.checked = row ? row.allow_tools : false
                                    mcpAllowProject.checked = row ? row.allow_project_data : false
                                    mcpCredentialName.text = row ? row.credential_field : ""
                                    mcpSecret.text = ""
                                    mcpTimeout.value = row ? row.timeout_s : 60
                                    settingsPage.settingsWindow.mcpEditPreset = row ? (row.preset_id || "") : ""
                                    settingsPage.settingsWindow.mcpEditDescription = row ? (row.description || "") : ""
                                    mcpDailyLimit.value = row ? (row.daily_call_limit || 0) : 0
                                    mcpPaidConsent.checked = row ? row.allow_paid_calls === true : false
                                    mcpEditor.visible = true
                                }
                                function saveSettingsDraft() {
                                    bridge.setAssistantTokenLimits(
                                        dailyTokenSpin.value, periodTokenSpin.value,
                                        periodMinutesSpin.value)
                                    if (assistantModelField.text.trim().length)
                                        saveContextProfile()
                                    bridge.setAssistantTaskOptions(
                                        taskModeCombo.currentValue || "chat",
                                        reasoningCombo.currentValue || "auto")
                                }
                                Component.onCompleted: restoreFields()

                                ColumnLayout {
                                    id: advancedSections
                                    width: parent.width
                                    spacing: 12
                    Category {
                        title: "服务商与连接"
                        Layout.fillWidth: true
                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 8
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "API 地址"; Layout.preferredWidth: 82 }
                                ThemeControls.ThemeComboBox {
                                    id: assistantUrlField
                                    objectName: "assistantUrlField"
                                    property alias text: assistantUrlField.editText
                                    property bool applyingProviderPreset: false
                                    editable: true
                                    model: bridge.assistantApiUrls
                                    editText: bridge.assistantApiUrl
                                    currentIndex: bridge.assistantApiUrls.indexOf(bridge.assistantApiUrl)
                                    Layout.fillWidth: true
                                    enabled: !bridge.modelDiscoveryBusy
                                    onEditTextChanged: {
                                        if (!applyingProviderPreset
                                                && (activeFocus || (contentItem && contentItem.activeFocus))
                                                && editText !== bridge.assistantApiUrl)
                                            settingsPage.updateAssistantConfig(settingsPage.assistantSwitchControl.checked, editText,
                                                                      assistantModelField.text)
                                    }
                                    onActivated: {
                                        editText = currentText
                                        settingsPage.updateAssistantConfig(settingsPage.assistantSwitchControl.checked, editText,
                                                                  assistantModelField.text)
                                        bridge.rememberAssistantApiUrl(editText)
                                    }
                                    onAccepted: bridge.rememberAssistantApiUrl(editText)
                                    Accessible.name: "API 服务器链接，可选择已保存地址"
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "服务商目录"; Layout.preferredWidth: 82 }
                                ThemeControls.ThemeComboBox {
                                    id: providerPresetCombo
                                    objectName: "providerPresetCombo"
                                    Layout.fillWidth: true
                                    model: bridge.providerPresets
                                    textRole: "name"
                                    currentIndex: bridge.providerPresetIndexForUrl(assistantUrlField.text)
                                    enabled: !bridge.modelDiscoveryBusy
                                    delegate: ThemedItemDelegate {
                                        required property var modelData
                                        width: providerPresetCombo.width
                                        text: modelData.name + (modelData.selectable ? "" : " · 仅信息")
                                        enabled: modelData.selectable
                                        hoverEnabled: true
                                        ToolTip.visible: hovered
                                        ToolTip.text: modelData.usage_restriction
                                    }
                                    onActivated: function(index) {
                                        const preset = bridge.providerPresets[index]
                                        if (!preset || !preset.selectable)
                                            return
                                        assistantUrlField.applyingProviderPreset = true
                                        assistantUrlField.editText = preset.base_url
                                        assistantUrlField.applyingProviderPreset = false
                                        assistantKeyField.text = ""
                                    }
                                    Accessible.name: "从本地服务商目录选择 API 地址；选择只更改尚未保存的地址"
                                }
                            }
                            ThemeControls.ThemeLabel {
                                objectName: "providerPresetInfo"
                                text: bridge.providerInfoForUrl(assistantUrlField.text)
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                color: settingsPage && settingsPage.settingsWindow.themeColors ? settingsPage.settingsWindow.themeColors.colorTextMuted : Theme.snapshot.colorTextMuted
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "API 密钥"; Layout.preferredWidth: 82 }
                                ThemeControls.ThemeField {
                                    id: assistantKeyField
                                    objectName: "assistantKeyField"
                                    Layout.fillWidth: true
                                    echoMode: TextInput.Password
                                    placeholderText: !bridge.assistantKeyStatusReady
                                                     ? "\u6b63\u5728\u68c0\u67e5\u7cfb\u7edf\u5bc6\u94a5\u72b6\u6001\u2026"
                                                     : bridge.hasAssistantApiKey
                                                       ? "\u5df2\u5b89\u5168\u4fdd\u5b58\uff1b\u8f93\u5165\u4ee5\u66ff\u6362"
                                                       : "\u8f93\u5165\u670d\u52a1\u5546\u5bc6\u94a5"
                                    Accessible.name: "看板娘 API 密钥"
                                }
                                ThemeControls.ThemeButton {
                                    text: "保存密钥"
                                    enabled: assistantKeyField.text.length > 0
                                             && assistantUrlField.text.trim() === bridge.assistantApiUrl
                                    onClicked: {
                                        if (bridge.saveAssistantApiKey(assistantKeyField.text))
                                            assistantKeyField.text = ""
                                    }
                                }
                                ThemeControls.ThemeButton {
                                    text: "删除"
                                    enabled: bridge.hasAssistantApiKey
                                    onClicked: bridge.clearAssistantApiKey()
                                }
                            }
                            ThemeControls.ThemeLabel {
                                text: (assistantUrlField.text.trim() !== bridge.assistantApiUrl
                                       ? "地址更改尚未保存；先保存设置或刷新模型，再按新地址保存密钥。\n"
                                       : bridge.assistantKeyMessage + "\n")
                                      + "密钥按服务商地址绑定并保存在系统安全凭据库，不写入项目文件。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                color: settingsPage && settingsPage.settingsWindow.themeColors ? settingsPage.settingsWindow.themeColors.colorTextMuted : Theme.snapshot.colorTextMuted
                            }
                        }
                    }

                    Category {
                        id: modelCategory
                        objectName: "modelCategory"
                        title: "模型与推理"
                        Layout.fillWidth: true
                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 8
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "当前模型" }
                                ThemeControls.ThemeField {
                                    id: assistantModelField
                                    objectName: "assistantModelField"
                                    Layout.fillWidth: true
                                    Layout.maximumWidth: 530
                                    text: bridge.assistantModel
                                    enabled: !bridge.modelDiscoveryBusy
                                    placeholderText: "填写模型名，或选择探测结果"
                                    selectByMouse: true
                                    onTextEdited: bridge.setAssistantConfig(settingsPage.assistantSwitchControl.checked,
                                                                           assistantUrlField.text, text)
                                    Accessible.name: "看板娘当前模型，可手动填写"
                                }
                                ThemeControls.ThemeButton {
                                    objectName: "refreshModelsButton"
                                    text: bridge.modelDiscoveryBusy && bridge.modelDiscoveryKind === "models"
                                          ? "探测中" : "刷新模型"
                                    enabled: !bridge.modelDiscoveryBusy && assistantUrlField.text.trim().length > 0
                                             && bridge.providerProbeAllowedForUrl(assistantUrlField.text)
                                    onClicked: {
                                        bridge.setAssistantConfig(settingsPage.assistantSwitchControl.checked, assistantUrlField.text,
                                                                  assistantModelField.text)
                                        bridge.discoverModels()
                                    }
                                }
                                BusyIndicator {
                                    running: bridge.modelDiscoveryBusy
                                    visible: running
                                    Layout.preferredWidth: 24
                                    Layout.preferredHeight: 24
                                }
                                ThemeControls.ThemeButton {
                                    text: "取消"
                                    visible: bridge.modelDiscoveryBusy
                                    onClicked: bridge.cancelModelProbe()
                                }
                                Item { Layout.fillWidth: true }
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "目录建议" }
                                ThemeControls.ThemeComboBox {
                                    id: providerModelCatalog
                                    objectName: "providerModelCatalog"
                                    Layout.fillWidth: true
                                    Layout.maximumWidth: 530
                                    model: bridge.providerModelSuggestions(assistantUrlField.text)
                                    textRole: "id"
                                    valueRole: "id"
                                    delegate: ThemedItemDelegate {
                                        required property var modelData
                                        width: providerModelCatalog.width
                                        text: modelData.id + ({primary: " · 主要", additional: " · 其他", preview: " · 预览"})[modelData.category]
                                    }
                                    onActivated: assistantModelField.text = currentValue
                                    Accessible.name: "官方目录中的模型 ID 建议；选择后仍未验证账号可用性"
                                }
                                ThemeControls.ThemeLabel {
                                    text: "目录 ID 未验证账号权限；选择后请手动保存或刷新验证。"
                                    Layout.fillWidth: true
                                    wrapMode: Text.WordWrap
                                    color: settingsPage && settingsPage.settingsWindow.themeColors ? settingsPage.settingsWindow.themeColors.colorTextMuted : Theme.snapshot.colorTextMuted
                                }
                            }

                            GridLayout {
                                Layout.fillWidth: true
                                columns: width >= 760 && discoveredModelList.visible ? 2 : 1
                                columnSpacing: 18
                                rowSpacing: 8
                                ListView {
                                    id: discoveredModelList
                                    objectName: "discoveredModelList"
                                    Layout.fillWidth: true
                                    Layout.maximumWidth: 430
                                    Layout.preferredWidth: 320
                                    Layout.preferredHeight: Math.min(144, bridge.availableModels.length * 36)
                                    Layout.alignment: Qt.AlignTop
                                    visible: bridge.availableModels.length > 0
                                    clip: true
                                    model: bridge.availableModels
                                    delegate: ThemedItemDelegate {
                                        objectName: "discoveredModelDelegate"
                                        required property var modelData
                                        width: ListView.view.width
                                        height: Math.max(36, implicitHeight)
                                        text: modelData.id
                                        highlighted: modelData.id === bridge.assistantModel
                                        hoverEnabled: true
                                        ToolTip.visible: hovered
                                        ToolTip.text: modelData.id
                                        onClicked: {
                                            assistantModelField.text = modelData.id
                                            bridge.setAssistantConfig(settingsPage.assistantSwitchControl.checked,
                                                                      assistantUrlField.text, modelData.id)
                                        }
                                    }
                                    ScrollBar.vertical: ScrollBar { }
                                    WheelHandler {
                                        target: null
                                        blocking: true
                                        acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                                        onWheel: function(event) {
                                            const delta = event.pixelDelta.y !== 0 ? event.pixelDelta.y
                                                        : event.angleDelta.y / 120 * 80
                                            discoveredModelList.contentY = Math.max(0,
                                                Math.min(Math.max(0, discoveredModelList.contentHeight
                                                                     - discoveredModelList.height),
                                                         discoveredModelList.contentY - delta))
                                            event.accepted = true
                                        }
                                    }
                                }
                                GridLayout {
                                    Layout.fillWidth: true
                                    Layout.alignment: Qt.AlignTop
                                    columns: 2
                                    columnSpacing: 10
                                    rowSpacing: 6
                                    ThemeControls.ThemeLabel { text: "默认模式" }
                                    ThemeControls.ThemeComboBox {
                                        id: taskModeCombo
                                        Layout.fillWidth: true
                                        model: [
                                            {text: "讨论", value: "chat"},
                                            {text: "操作", value: "operate"},
                                            {text: "攻关", value: "research"}
                                        ]
                                        textRole: "text"
                                        valueRole: "value"
                                        currentIndex: ["chat", "operate", "research"].indexOf(bridge.assistantTaskMode)
                                        onActivated: bridge.setAssistantTaskOptions(currentValue,
                                                                                   reasoningCombo.currentValue || "auto")
                                    }
                                    ThemeControls.ThemeLabel { text: "推理强度" }
                                    ThemeControls.ThemeComboBox {
                                        id: reasoningCombo
                                        objectName: "reasoningCombo"
                                        Layout.fillWidth: true
                                        model: bridge.assistantReasoningOptions.map(function(value) {
                                            return {value: value, text: ({
                                                auto: "自动", none: "无推理 · none", minimal: "最低 · minimal",
                                                low: "低 · low", medium: "中 · medium", high: "高 · high",
                                                xhigh: "更高 · xhigh", max: "最大 · max", ultra: "极高 · ultra",
                                                thinking_disabled: "关闭思考", thinking_enabled: "开启思考"
                                            })[value] || value}
                                        })
                                        textRole: "text"
                                        valueRole: "value"
                                        currentIndex: bridge.assistantReasoningOptions.indexOf(bridge.assistantReasoningStrength)
                                        enabled: bridge.assistantReasoningSupported
                                        onActivated: bridge.setAssistantTaskOptions(taskModeCombo.currentValue, currentValue)
                                    }
                                    ThemeControls.ThemeButton {
                                        text: bridge.modelDiscoveryBusy && bridge.modelDiscoveryKind === "reasoning"
                                              ? "测试中" : "测试推理档位（消耗额度）"
                                        Layout.columnSpan: 2
                                        enabled: !bridge.modelDiscoveryBusy && assistantUrlField.text.trim().length > 0
                                                 && bridge.providerProbeAllowedForUrl(assistantUrlField.text)
                                                 && assistantModelField.text.trim().length > 0
                                        onClicked: {
                                            bridge.setAssistantConfig(settingsPage.assistantSwitchControl.checked, assistantUrlField.text,
                                                                      assistantModelField.text)
                                            bridge.discoverReasoning()
                                        }
                                    }
                                }
                            }

                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                text: "模型列表先读取 /models；无可用列表时，刷新或自动探测会用至多 12 个候选模型发送短聊天请求并消耗额度（可能计费）。推理测试另会发送至多 14 次请求。"
                                wrapMode: Text.WordWrap
                                color: settingsPage && settingsPage.settingsWindow.themeColors ? settingsPage.settingsWindow.themeColors.colorTextMuted : Theme.snapshot.colorTextMuted
                            }

                            ThemeControls.ThemeSection {
                                id: modelContextSection
                                objectName: "modelContextSection"
                                title: "模型上下文与压缩"
                                expanded: false
                                Layout.fillWidth: true

                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: Theme.snapshot.spaceSm

                                    ThemeControls.ThemeLabel {
                                        text: "1 K = 1000 Token。空白或 0 使用当前模型自动值；手动值按规范化后的完整 API 地址和精确模型 ID 保存。每项最多精确到 0.001 K。"
                                        Layout.fillWidth: true
                                        wrapMode: Text.WordWrap
                                        color: settingsPage && settingsPage.settingsWindow.themeColors
                                               ? settingsPage.settingsWindow.themeColors.colorTextMuted
                                               : Theme.snapshot.colorTextMuted
                                    }

                                    NumericGrid {
                                        id: contextProfileFields
                                        objectName: "contextProfileFields"
                                        availableContentWidth: modelCategory.sectionAvailableWidth

                                        NumericField {
                                            availableContentWidth: contextProfileFields.cellAvailableWidth
                                            NumericLabel { text: "上下文窗口 C / K" }
                                            TokenSpin {
                                                id: contextWindowTokensSpin
                                                objectName: "contextWindowTokensSpin"
                                                value: bridge.assistantContextProfileOverrides.context_window_tokens || 0
                                                enabled: bridge.assistantModel.length > 0
                                                onValueModified: window.saveContextProfile()
                                                Accessible.name: "模型上下文窗口，单位 K Token，空白或 0 使用自动值"
                                            }
                                        }
                                        NumericField {
                                            availableContentWidth: contextProfileFields.cellAvailableWidth
                                            NumericLabel { text: "预留输出 O / K" }
                                            TokenSpin {
                                                id: reservedOutputTokensSpin
                                                objectName: "reservedOutputTokensSpin"
                                                value: bridge.assistantContextProfileOverrides.reserved_output_tokens || 0
                                                enabled: bridge.assistantModel.length > 0
                                                onValueModified: window.saveContextProfile()
                                                Accessible.name: "模型预留输出，单位 K Token，空白或 0 使用自动值"
                                            }
                                        }
                                        NumericField {
                                            availableContentWidth: contextProfileFields.cellAvailableWidth
                                            NumericLabel { text: "压缩预留 R / K" }
                                            TokenSpin {
                                                id: compressionReserveTokensSpin
                                                objectName: "compressionReserveTokensSpin"
                                                value: bridge.assistantContextProfileOverrides.compression_reserve_tokens || 0
                                                enabled: bridge.assistantModel.length > 0
                                                onValueModified: window.saveContextProfile()
                                                Accessible.name: "压缩请求预留，单位 K Token，空白或 0 使用自动值"
                                            }
                                        }
                                        NumericField {
                                            availableContentWidth: contextProfileFields.cellAvailableWidth
                                            NumericLabel { text: "压缩保留历史 H / K" }
                                            TokenSpin {
                                                id: historyKeepTokensSpin
                                                objectName: "historyKeepTokensSpin"
                                                value: bridge.assistantContextProfileOverrides.history_keep_tokens || 0
                                                enabled: bridge.assistantModel.length > 0
                                                onValueModified: window.saveContextProfile()
                                                Accessible.name: "压缩后保留的原始历史目标，单位 K Token，空白或 0 使用自动值"
                                            }
                                        }
                                        NumericField {
                                            availableContentWidth: contextProfileFields.cellAvailableWidth
                                            NumericLabel { text: "自动压缩阈值 p / %" }
                                            ThemeControls.ThemeSpinBox {
                                                id: contextCompactionThresholdSpin
                                                objectName: "contextCompactionThresholdSpin"
                                                from: 0
                                                to: 100
                                                stepSize: 1
                                                editable: true
                                                value: bridge.assistantContextProfileOverrides.compaction_threshold_percent || 0
                                                valueFromText: function(text, locale) {
                                                    if (text.trim().length === 0) return 0
                                                    const count = Number.fromLocaleString(locale, text)
                                                    return isFinite(count) ? Math.round(count)
                                                                         : contextCompactionThresholdSpin.value
                                                }
                                                enabled: bridge.assistantModel.length > 0
                                                Layout.minimumWidth: window.numericControlWidth
                                                Layout.preferredWidth: window.numericControlWidth
                                                Layout.maximumWidth: window.numericControlWidth
                                                onValueModified: window.saveContextProfile()
                                                Accessible.name: "自动压缩触发阈值，百分比，空白或 0 使用自动值"
                                            }
                                        }
                                    }

                                    RowLayout {
                                        Layout.fillWidth: true
                                        ThemeControls.ThemeButton {
                                            objectName: "resetContextProfileButton"
                                            text: "重置为模型默认"
                                            enabled: bridge.assistantModel.length > 0
                                            onClicked: bridge.resetAssistantContextProfile()
                                        }
                                        Item { Layout.fillWidth: true }
                                    }

                                    ThemeControls.ThemeLabel {
                                        objectName: "contextBudgetAutoDefaults"
                                        Layout.fillWidth: true
                                        wrapMode: Text.WordWrap
                                        text: {
                                            const budget = bridge.assistantContextBudget
                                            const defaults = budget.defaults || ({})
                                            return "当前自动值：C " + window.knownKTokenText(defaults.context_window_tokens)
                                                + " · O " + window.knownKTokenText(defaults.reserved_output_tokens)
                                                + " · R " + window.knownKTokenText(defaults.compression_reserve_tokens)
                                                + " · H " + window.knownKTokenText(defaults.history_keep_tokens)
                                                + " · p " + String(defaults.compaction_threshold_percent || 0) + "%"
                                        }
                                        color: settingsPage && settingsPage.settingsWindow.themeColors
                                               ? settingsPage.settingsWindow.themeColors.colorTextMuted
                                               : Theme.snapshot.colorTextMuted
                                    }

                                    ThemeControls.ThemeLabel {
                                        objectName: "contextBudgetHardLimits"
                                        Layout.fillWidth: true
                                        wrapMode: Text.WordWrap
                                        text: {
                                            const budget = bridge.assistantContextBudget
                                            const limits = budget.limits || ({})
                                            const defaults = budget.defaults || ({})
                                            return "只读硬限：C " + window.knownKTokenText(defaults.context_window_tokens)
                                                + " · I " + window.knownKTokenText(limits.provider_max_input_tokens)
                                                + " · Omax " + window.knownKTokenText(limits.max_output_tokens)
                                                + "。旧版 maxI 输入上限提示："
                                                + window.knownKTokenText(limits.legacy_max_input_tokens)
                                                + "；当前有效 I：" + window.knownKTokenText(limits.max_input_tokens) + "。"
                                        }
                                    }

                                    ThemeControls.ThemeLabel {
                                        objectName: "contextBudgetPreview"
                                        Layout.fillWidth: true
                                        wrapMode: Text.WordWrap
                                        text: {
                                            const budget = bridge.assistantContextBudget
                                            const b = window.kTokenText(budget.budget_tokens)
                                            const t = window.kTokenText(budget.trigger_tokens)
                                            return "只读预算预览：有效输入 B = " + b
                                                + (budget.budget_known ? "" : "（模型容量未知，B 使用运行时回退）")
                                                + "；实际压缩触发 T = " + t + "。"
                                        }
                                    }

                                    ThemeControls.ThemeLabel {
                                        objectName: "contextBudgetProfileInfo"
                                        Layout.fillWidth: true
                                        wrapMode: Text.WordWrap
                                        color: settingsPage && settingsPage.settingsWindow.themeColors
                                               ? settingsPage.settingsWindow.themeColors.colorTextMuted
                                               : Theme.snapshot.colorTextMuted
                                        text: {
                                            const profile = bridge.assistantContextBudget.profile || ({})
                                            const rawSources = profile.source || []
                                            const sources = Array.isArray(rawSources) ? rawSources
    : (typeof rawSources === "string" ? [rawSources]
       : (rawSources && typeof rawSources.length === "number"
          ? Array.prototype.slice.call(rawSources) : [rawSources]))
                                            const sourceText = sources.map(function(source) {
                                                if (typeof source === "string") return source
                                                if (source && typeof source === "object") {
                                                    const title = source.title || source.name || ""
                                                    const url = source.url || source.href || ""
                                                    return [title, url].filter(function(part) { return !!part }).join(" — ")
                                                }
                                                return String(source || "")
                                            }).filter(function(source) { return source.length > 0 }).join("; ")
                                            const match = profile.match || ({})
                                            const matchText = match.matched ? "\u7cbe\u786e\u5339\u914d" : "\u672a\u5339\u914d"
                                            const rule = match.rule ? String(match.rule) : "\u5339\u914d\u89c4\u5219\u672a\u8bb0\u5f55"
                                            return "\u5b98\u65b9\u6765\u6e90\uff1a" + (sourceText || "\u672a\u8bb0\u5f55")
                                                + "\n\u8d44\u6599\u65e5\u671f\uff1a" + String(profile.date || "\u672a\u8bb0\u5f55")
                                                + "\uff1b\u7248\u672c\uff1a" + String(profile.version || "\u672a\u8bb0\u5f55")
                                                + "\uff1b\u5f53\u524d\u7ed3\u679c\uff1a" + matchText
                                                + "\n\u5339\u914d\u89c4\u5219\uff1a" + rule
                                                + (profile.unknown_reason ? "\n\u5bb9\u91cf\u672a\u77e5\u539f\u56e0\uff1a" + String(profile.unknown_reason) : "")
                                                + (profile.rounding_note ? "\n" + String(profile.rounding_note) : "")
                                                + (profile.notes ? "\n" + String(profile.notes) : "")
                                        }
                                    }

                                    ThemeControls.ThemeLabel {
                                        objectName: "contextBudgetValidation"
                                        visible: bridge.assistantContextProfileErrors.length > 0
                                        Layout.fillWidth: true
                                        wrapMode: Text.WordWrap
                                        text: "配置需修正：" + bridge.assistantContextProfileErrors.join("；")
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel {
                                text: bridge.modelDiscoveryMessage
                                visible: text.length > 0
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            ThemeControls.ThemeLabel {
                                text: bridge.probeUsage
                                visible: text.length > 0
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            ThemeControls.ThemeButton { flat: true;
                                id: probeDetailsToggle
                                text: checked ? "探测与缓存详情 ▾" : "探测与缓存详情 ▸"
                                checkable: true
                                checked: false
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                visible: probeDetailsToggle.checked
                                spacing: 6
                                ThemeControls.ThemeLabel {
                                    text: "模型缓存按服务商、地址和凭据区分，最长 7 天；密钥变化后失效。自动探测和手动刷新先读取 /models；没有可用列表时会依次用至多 12 个候选模型发送短聊天请求，单次输出上限 32 Token，并消耗额度、可能计费。远程服务需密钥，本机回环服务可匿名探测。推理强度测试仅由上方按钮手动发起，最多 14 次请求；单次输出上限 32 Token、单次超时 15 秒，总时限 120 秒。参数被接受不代表内部推理效果。"
                                    Layout.fillWidth: true
                                    wrapMode: Text.WordWrap
                                }
                                ThemeControls.ThemeLabel {
                                    text: bridge.assistantReasoningDescription
                                    Layout.fillWidth: true
                                    wrapMode: Text.WordWrap
                                }
                                ThemeControls.ThemeLabel {
                                    text: bridge.assistantNativeReasoningOptions.length
                                          ? "原生档位：" + bridge.assistantNativeReasoningOptions.join(" · ") : ""
                                    visible: text.length > 0
                                    Layout.fillWidth: true
                                    wrapMode: Text.WordWrap
                                }
                                Repeater {
                                    model: bridge.probeResults
                                    delegate: ThemeControls.ThemeLabel {
                                        required property var modelData
                                        text: modelData.label + "：" + modelData.status_text
                                              + (modelData.http_status ? "（HTTP " + modelData.http_status + "）" : "")
                                              + (modelData.detail ? " · " + modelData.detail : "")
                                        Layout.fillWidth: true
                                        wrapMode: Text.WordWrap
                                    }
                                }
                            }
                        }
                    }

                    Category {
                        id: quotaCategory
                        title: "用量与额度"
                        Layout.fillWidth: true
                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 8
                            NumericGrid {
                                id: quotaFields
                                availableContentWidth: quotaCategory.sectionAvailableWidth
                                NumericField {
                                    availableContentWidth: quotaFields.cellAvailableWidth
                                    NumericLabel { text: "每日限额 / K" }
                                    TokenSpin {
                                        id: dailyTokenSpin
                                        to: 2000000000
                                        value: bridge.assistantDailyTokenLimit
                                        onValueModified: bridge.setAssistantTokenLimits(value,
                                                                                       periodTokenSpin.value,
                                                                                       periodMinutesSpin.value)
                                        Accessible.name: "每日 Token 限额，单位 K，0 为不限"
                                    }
                                }
                                NumericField {
                                    availableContentWidth: quotaFields.cellAvailableWidth
                                    NumericLabel { text: "时段限额 / K" }
                                    TokenSpin {
                                        id: periodTokenSpin
                                        to: 2000000000
                                        value: bridge.assistantPeriodTokenLimit
                                        onValueModified: bridge.setAssistantTokenLimits(dailyTokenSpin.value, value,
                                                                                       periodMinutesSpin.value)
                                        Accessible.name: "每时段 Token 限额，单位 K，0 为不限"
                                    }
                                }
                                NumericField {
                                    availableContentWidth: quotaFields.cellAvailableWidth
                                    NumericLabel { text: "时段 / 分钟" }
                                    ThemeControls.ThemeSpinBox {
                                        id: periodMinutesSpin
                                        from: 1; to: 10080; stepSize: 5
                                        value: bridge.assistantPeriodMinutes
                                        editable: true
                                        Layout.minimumWidth: window.numericControlWidth
                                        Layout.preferredWidth: window.numericControlWidth
                                        Layout.maximumWidth: window.numericControlWidth
                                        Layout.alignment: Qt.AlignLeft | Qt.AlignVCenter
                                        onValueModified: bridge.setAssistantTokenLimits(dailyTokenSpin.value,
                                                                                       periodTokenSpin.value, value)
                                        Accessible.name: "Token 限额时段长度，单位分钟"
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel {
                                text: "每日和时段限额按输入与输出总量计，0 表示不限；每任务请求、累计 Token、工具调用及子助手委派默认不设次数上限，云模型调用可能继续计费。统计自动使用 K / M，未返回用量时标明估算。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                color: settingsPage && settingsPage.settingsWindow.themeColors ? settingsPage.settingsWindow.themeColors.colorTextMuted : Theme.snapshot.colorTextMuted
                            }
                        }
                    }

                    Category {
                        objectName: "assistantToolkitCategory"
                        title: "助手独立工作空间"
                        expanded: false
                        Layout.fillWidth: true
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            CheckBox {
                                objectName: "assistantToolkitEnabledCheck"
                                text: "启用助手工作空间与工具集"
                                checked: bridge.assistantToolkitEnabled
                                onClicked: bridge.setAssistantToolkitConfig(checked,
                                    bridge.assistantToolkitWorkspaceRoot, bridge.assistantToolkitAllowModelContext)
                            }
                            ThemeControls.ThemeField {
                                objectName: "assistantToolkitWorkspaceRootField"
                                Layout.fillWidth: true
                                text: bridge.assistantToolkitWorkspaceRoot
                                placeholderText: bridge.assistantToolkitDefaultWorkspaceRoot
                                onTextEdited: bridge.setAssistantToolkitConfig(bridge.assistantToolkitEnabled,
                                    text, bridge.assistantToolkitAllowModelContext)
                                Accessible.name: "工具集授权工作区"
                            }
                            Button {
                                objectName: "assistantToolkitResetWorkspaceButton"
                                text: "使用默认独立工作空间"
                                onClicked: bridge.resetAssistantToolkitWorkspace()
                            }
                            CheckBox {
                                objectName: "assistantToolkitContextCheck"
                                text: "允许所选工作区工具结果发送给当前模型服务"
                                checked: bridge.assistantToolkitAllowModelContext
                                onClicked: bridge.setAssistantToolkitConfig(bridge.assistantToolkitEnabled,
                                    bridge.assistantToolkitWorkspaceRoot, checked)
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                text: "默认工作空间长期保留，与当前工程和软件目录分开。操作/攻关模式可直接读写、保存和可恢复删除文件；讨论模式只读。长期记忆仅保存精简的重要偏好、约束或结论，不保存凭据和整段对话。仅两列都选中的能力生效；任一列表为空即全部关闭。执行、安装、网络、远程与 OCR 需独立提供器，不会自动开启。"
                            }
                            Repeater {
                                model: bridge.assistantToolkitCapabilityOptions
                                delegate: RowLayout {
                                    required property var modelData
                                    Layout.fillWidth: true
                                    ThemeControls.ThemeLabel {
                                        text: modelData.id + " · " + modelData.label
                                        Layout.fillWidth: true
                                        wrapMode: Text.WordWrap
                                    }
                                    CheckBox {
                                        objectName: "toolkitGlobal_" + modelData.id
                                        text: "全局"
                                        checked: bridge.assistantToolkitCapabilities.indexOf(modelData.id) >= 0
                                        onClicked: bridge.setAssistantToolkitCapability(modelData.id, checked, false)
                                        Accessible.name: modelData.id + " 全局能力"
                                    }
                                    CheckBox {
                                        objectName: "toolkitProfile_" + modelData.id
                                        text: "配置"
                                        checked: bridge.assistantToolkitProfileCapabilities.indexOf(modelData.id) >= 0
                                        onClicked: bridge.setAssistantToolkitCapability(modelData.id, checked, true)
                                        Accessible.name: modelData.id + " 配置能力"
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel {
                                objectName: "assistantToolkitStatusLabel"
                                text: bridge.assistantToolkitStatus
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                        }
                    }

                    Category {
                        objectName: "researchLibraryCategory"
                        title: "研究资料库"
                        Layout.fillWidth: true
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            ThemeControls.ThemeField {
                                objectName: "researchLibraryRootField"
                                Layout.fillWidth: true
                                text: bridge.assistantResearchLibraryRoot
                                placeholderText: "可选：已授权外部研究正文目录的绝对路径"
                                onTextEdited: bridge.setAssistantResearchLibraryRoot(text)
                                Accessible.name: "可选外部研究库目录"
                            }
                            ThemeControls.ThemeLabel {
                                text: "内置公开研究资料随应用只读提供，无需填写路径。此处可选配置已授权的外部资料目录；留空时保留已有本机 sidecar 配置，无此配置则使用内置资料。保存后下一轮生效，实际来源、版本和覆盖范围以研究库状态为准。仅按需读取，不自动下载远程仓库；外部文献 MCP 和当前项目研究证据仍是独立来源。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                color: settingsPage && settingsPage.settingsWindow.themeColors ? settingsPage.settingsWindow.themeColors.colorTextMuted : Theme.snapshot.colorTextMuted
                            }
                        }
                    }

                    Category {
                        objectName: "externalMcpCategory"
                        title: "外部 MCP 服务"
                        Layout.fillWidth: true
                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 8
                            ThemeControls.ThemeLabel {
                                text: "为看板娘连接外部工具。新增服务默认停用；服务仅接收调用时的参数，不自动转发历史、附件或项目根目录。服务程序使用当前用户权限运行，请使用可信来源。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                color: settingsPage && settingsPage.settingsWindow.themeColors ? settingsPage.settingsWindow.themeColors.colorTextMuted : Theme.snapshot.colorTextMuted
                            }
                            Repeater {
                                model: bridge.mcpServers
                                delegate: Frame {
                                    id: mcpRow
                                    required property var modelData
                                    readonly property bool expanded: settingsPage.settingsWindow.mcpExpandedId === modelData.id
                                    objectName: "mcpRow_" + modelData.id
                                    background: ThemeControls.ThemeSurface { padding: 0; contentSpacing: 0 }
                                    Layout.fillWidth: true
                                    ColumnLayout {
                                        anchors.fill: parent
                                        spacing: 4
                                        RowLayout {
                                            Layout.fillWidth: true
                                            ThemeControls.ThemeButton {
                                                id: mcpNameButton
                                                objectName: "mcpName_" + modelData.id
                                                text: modelData.name
                                                flat: true
                                                Layout.fillWidth: true
                                                Layout.minimumWidth: 0
                                                Accessible.name: modelData.name
                                                Accessible.description: mcpRow.expanded ? "收起服务详细配置" : "展开服务详细配置"
                                                contentItem: RowLayout {
                                                    spacing: 4
                                                    Canvas {
                                                        Layout.preferredWidth: 12
                                                        Layout.preferredHeight: 12
                                                        Accessible.ignored: true
                                                        property bool expanded: mcpRow.expanded
                                                        onExpandedChanged: requestPaint()
                                                        onPaint: {
                                                            const ctx = getContext("2d")
                                                            ctx.clearRect(0, 0, width, height)
                                                            ctx.strokeStyle = mcpNameButton.themeTokens.colorText
                                                            ctx.lineWidth = 1.5
                                                            ctx.beginPath()
                                                            ctx.moveTo(width * .25, height * (expanded ? .35 : .25))
                                                            ctx.lineTo(width * (expanded ? .5 : .65), height * .5)
                                                            ctx.lineTo(width * .25, height * (expanded ? .35 : .75))
                                                            if (expanded) {
                                                                ctx.moveTo(width * .5, height * .5)
                                                                ctx.lineTo(width * .75, height * .35)
                                                            }
                                                            ctx.stroke()
                                                        }
                                                    }
                                                    Text {
                                                        Layout.fillWidth: true
                                                        text: mcpNameButton.text
                                                        font: mcpNameButton.font
                                                        color: mcpNameButton.themeTokens.colorText
                                                        elide: Text.ElideRight
                                                        Accessible.ignored: true
                                                    }
                                                }
                                                function toggleDetails() {
                                                    settingsPage.settingsWindow.mcpExpandedId = mcpRow.expanded ? "" : modelData.id
                                                }
                                                onClicked: toggleDetails()
                                                Keys.onReturnPressed: toggleDetails()
                                                Keys.onEnterPressed: toggleDetails()
                                            }
                                            CheckBox {
                                                objectName: "mcpEnabled_" + modelData.id
                                                text: "启用"
                                                Accessible.name: "启用 " + modelData.name
                                                checked: modelData.enabled
                                                onToggled: bridge.setMcpPermissions(modelData.id, checked, modelData.allow_tools)
                                            }
                                        }
                                        ColumnLayout {
                                            objectName: "mcpDetails_" + modelData.id
                                            visible: mcpRow.expanded
                                            Layout.fillWidth: true
                                            ThemeControls.ThemeLabel {
                                                text: ({stdio: "stdio", http: "HTTP", sse: "SSE"})[modelData.transport]
                                                    + (modelData.description ? " · " + modelData.description : "")
                                                Layout.fillWidth: true
                                                wrapMode: Text.WrapAnywhere
                                            }
                                            CheckBox {
                                                text: "允许工具调用"
                                                checked: modelData.allow_tools
                                                onToggled: bridge.setMcpPermissions(modelData.id, modelData.enabled, checked)
                                            }
                                            Flow {
                                                Layout.fillWidth: true
                                                spacing: 4
                                                ThemeControls.ThemeButton { text: "编辑"; onClicked: settingsPage.settingsWindow.editMcpServer(modelData) }
                                                ThemeControls.ThemeButton {
                                                    text: "检查连接"
                                                    enabled: !bridge.mcpProbeBusy
                                                    onClicked: bridge.checkMcpServer(modelData.id)
                                                }
                                                ThemeControls.ThemeButton { text: "删除"; onClicked: bridge.deleteMcpServer(modelData.id) }
                                            }
                                            ThemeControls.ThemeLabel {
                                                text: modelData.status
                                                Layout.fillWidth: true
                                                wrapMode: Text.WordWrap
                                            }
                                        }
                                    }
                                }
                            }
                            RowLayout {
                                ThemeControls.ThemeButton { text: "新增服务"; onClicked: settingsPage.settingsWindow.editMcpServer(null) }
                                ThemeControls.ThemeButton { text: "取消检查"; visible: bridge.mcpProbeBusy; onClicked: bridge.cancelMcpProbe() }
                                BusyIndicator {
                                    running: bridge.mcpProbeBusy
                                    visible: running
                                    Layout.preferredWidth: 24
                                    Layout.preferredHeight: 24
                                }
                            }
                            Frame {
                                id: mcpEditor
                                background: ThemeControls.ThemeSurface { padding: 0; contentSpacing: 0 }
                                visible: false
                                Layout.fillWidth: true
                                ColumnLayout {
                                    width: parent.width
                                    spacing: 8
                                    ThemeControls.ThemeLabel { text: (settingsPage && settingsPage.settingsWindow.mcpEditId.length) ? "编辑服务" : "新增服务"; font.bold: true }
                                    GridLayout {
                                        Layout.fillWidth: true
                                        columns: 2
                                        columnSpacing: 10
                                        ThemeControls.ThemeLabel { text: "名称" }
                                        ThemeControls.ThemeField { id: mcpName; Layout.fillWidth: true; placeholderText: "服务显示名称"; selectByMouse: true }
                                        ThemeControls.ThemeLabel { text: "传输" }
                                        ThemeControls.ThemeComboBox {
                                            id: mcpTransport
                                            Layout.fillWidth: true
                                            model: ["stdio · 启动本地程序", "HTTP · Streamable HTTP", "SSE · 兼容旧服务"]
                                        }
                                        ThemeControls.ThemeLabel { text: "程序"; visible: mcpTransport.currentIndex === 0 }
                                        ThemeControls.ThemeField {
                                            id: mcpCommand
                                            visible: mcpTransport.currentIndex === 0
                                            Layout.fillWidth: true
                                            placeholderText: "可执行程序绝对路径，或 PATH 中的命令"
                                            selectByMouse: true
                                        }
                                        ThemeControls.ThemeLabel { text: "参数"; visible: mcpTransport.currentIndex === 0 }
                                        ThemeControls.ThemeField {
                                            id: mcpArgs
                                            visible: mcpTransport.currentIndex === 0
                                            Layout.fillWidth: true
                                            placeholderText: '["-m", "example_server"]，不可填写密钥'
                                            selectByMouse: true
                                        }
                                        ThemeControls.ThemeLabel { text: "工作目录"; visible: mcpTransport.currentIndex === 0 }
                                        ThemeControls.ThemeField {
                                            id: mcpCwd
                                            visible: mcpTransport.currentIndex === 0
                                            Layout.fillWidth: true
                                            placeholderText: "绝对路径；留空使用临时目录"
                                            selectByMouse: true
                                        }
                                        ThemeControls.ThemeLabel { text: "服务地址"; visible: mcpTransport.currentIndex !== 0 }
                                        ThemeControls.ThemeField {
                                            id: mcpUrl
                                            visible: mcpTransport.currentIndex !== 0
                                            Layout.fillWidth: true
                                            placeholderText: "https://example.com/mcp，或本机 HTTP"
                                            selectByMouse: true
                                        }
                                        ThemeControls.ThemeLabel { text: mcpTransport.currentIndex === 0 ? "凭据变量名" : "凭据头名称" }
                                        ThemeControls.ThemeField {
                                            id: mcpCredentialName
                                            Layout.fillWidth: true
                                            placeholderText: mcpTransport.currentIndex === 0 ? "例如 API_KEY，可留空" : "通常为 Authorization，可留空"
                                            selectByMouse: true
                                        }
                                        ThemeControls.ThemeLabel { text: "凭据" }
                                        ThemeControls.ThemeField {
                                            id: mcpSecret
                                            Layout.fillWidth: true
                                            echoMode: TextInput.Password
                                            placeholderText: (settingsPage && settingsPage.settingsWindow.mcpHasCredential) ? "已保存；留空保留，填写则替换" : "可留空；Authorization 填令牌即可"
                                            selectByMouse: true
                                        }
                                        ThemeControls.ThemeLabel { text: "工具超时 / 秒" }
                                        ThemeControls.ThemeSpinBox { id: mcpTimeout; from: 5; to: 600; value: 60; editable: true }
                                    }
                                    ThemeControls.ThemeLabel {
                                        text: settingsPage.settingsWindow.mcpEditDescription
                                        visible: text.length > 0
                                        Layout.fillWidth: true
                                        wrapMode: Text.WrapAnywhere
                                    }
                                    RowLayout {
                                        visible: settingsPage.settingsWindow.mcpEditPreset.length > 0
                                        ThemeControls.ThemeLabel { text: "每日调用上限（UTC；0 为禁止）"; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                                        ThemeControls.ThemeSpinBox { id: mcpDailyLimit; from: 0; to: 10000; value: 0; editable: true }
                                    }
                                    CheckBox {
                                        id: mcpPaidConsent
                                        visible: settingsPage.settingsWindow.mcpEditPreset === "zhihu"
                                        text: "我了解限免后可能扣余额，允许预算内计费调用"
                                    }
                                    CheckBox { id: mcpEnabled; text: "启用连接" }
                                    CheckBox { id: mcpAllowTools; text: "允许看板娘调用此服务的工具" }
                                    CheckBox { id: mcpAllowProject; text: "允许向此服务发送或让其访问项目内容" }
                                    ThemeControls.ThemeLabel {
                                        text: "凭据只保存到系统安全凭据库，请勿把密钥放进程序参数或地址。项目内容许可默认关闭；路径检查无法识别所有复制的文本或数值。检查连接会启动配置的程序或连接服务并读取工具目录。"
                                        Layout.fillWidth: true
                                        wrapMode: Text.WordWrap
                                        color: settingsPage && settingsPage.settingsWindow.themeColors ? settingsPage.settingsWindow.themeColors.colorTextMuted : Theme.snapshot.colorTextMuted
                                    }
                                    RowLayout {
                                        ThemeControls.ThemeButton {
                                            text: "保存服务"
                                            onClicked: {
                                                const saved = bridge.saveMcpServer({
                                                    id: settingsPage.settingsWindow.mcpEditId, name: mcpName.text,
                                                    transport: ["stdio", "http", "sse"][mcpTransport.currentIndex],
                                                    command: mcpCommand.text, args_json: mcpArgs.text,
                                                    cwd: mcpCwd.text, url: mcpUrl.text,
                                                    enabled: mcpEnabled.checked, allow_tools: mcpAllowTools.checked,
                                                    allow_project_data: mcpAllowProject.checked,
                                                    credential_field: mcpCredentialName.text, timeout_s: mcpTimeout.value,
                                                    daily_call_limit: mcpDailyLimit.value, allow_paid_calls: mcpPaidConsent.checked
                                                }, mcpSecret.text)
                                                if (saved) {
                                                    mcpSecret.text = ""
                                                    mcpEditor.visible = false
                                                }
                                            }
                                        }
                                        ThemeControls.ThemeButton {
                                            text: "删除已存凭据"
                                            enabled: settingsPage.settingsWindow.mcpEditId.length > 0 && settingsPage.settingsWindow.mcpHasCredential
                                            onClicked: if (bridge.clearMcpCredential(settingsPage.settingsWindow.mcpEditId)) settingsPage.settingsWindow.mcpHasCredential = false
                                        }
                                        ThemeControls.ThemeButton { text: "取消编辑"; onClicked: { mcpSecret.text = ""; mcpEditor.visible = false } }
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel { text: bridge.mcpMessage; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                        }
                    }

                    Category {
                        title: "本软件对外 MCP"
                        Layout.fillWidth: true
                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 8
                            ThemeControls.ThemeLabel {
                                text: "支持外部 MCP 客户端读取和保存设计、控制项目窗口、提交与查询计算作业、管理 data 文件及生成导出。默认使用 stdio，由客户端启动服务进程，所有路径仍受项目服务的规则限制。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                color: settingsPage && settingsPage.settingsWindow.themeColors ? settingsPage.settingsWindow.themeColors.colorTextMuted : Theme.snapshot.colorTextMuted
                            }
                            ThemeControls.ThemeButton { text: "复制 stdio 客户端配置"; onClicked: bridge.copyMcpText(bridge.ownMcpStdioConfig) }
                            ThemeControls.ThemeButton { flat: true; id: ownMcpDetails; text: checked ? "查看 stdio 配置 ▾" : "查看 stdio 配置 ▸"; checkable: true }
                            TextArea {
                                visible: ownMcpDetails.checked
                                text: bridge.ownMcpStdioConfig
                                Layout.fillWidth: true
                                readOnly: true
                                selectByMouse: true
                                wrapMode: TextEdit.WrapAnywhere
                                font.family: "monospace"
                            }
                            ThemeControls.ThemeLabel {
                                text: "本机 HTTP 仅在点击启动后监听 127.0.0.1，使用本次启动的临时 Bearer 令牌；关闭设置窗口或退出程序会停止服务。复制的配置含令牌，请只提供给可信客户端。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            Flow {
                                Layout.fillWidth: true
                                spacing: 8
                                Row {
                                    spacing: 8
                                    ThemeControls.ThemeLabel { text: "本机端口"; anchors.verticalCenter: parent.verticalCenter }
                                    ThemeControls.ThemeSpinBox { id: ownMcpPort; from: 1024; to: 65535; value: 8766; editable: true; enabled: !bridge.ownMcpHttpRunning }
                                }
                                ThemeControls.ThemeButton { text: "启动本机 HTTP"; enabled: !bridge.ownMcpHttpRunning; onClicked: bridge.startOwnMcpHttp(ownMcpPort.value) }
                                ThemeControls.ThemeButton { text: "停止 HTTP"; enabled: bridge.ownMcpHttpRunning; onClicked: bridge.stopOwnMcpHttp() }
                                ThemeControls.ThemeButton { text: "复制 HTTP 客户端配置"; enabled: bridge.ownMcpHttpRunning; onClicked: bridge.copyOwnMcpHttpConfig() }
                            }
                            ThemeControls.ThemeLabel { text: bridge.ownMcpHttpStatus; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                        }
                    }

                    Category {
                        title: "开发选项"
                        Layout.fillWidth: true
                        visible: false
                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 6
                            Switch {
                                text: "启用开发模式"
                                checked: bridge.developerEnabled
                                enabled: !bridge.developerLaunchEnabled
                                onToggled: bridge.setDeveloperEnabled(checked)
                            }
                            ThemeControls.ThemeLabel {
                                text: bridge.developerLaunchEnabled
                                    ? "本次已通过启动参数开启开发模式。关闭时请不带开发参数重新启动；发行构建不可启用。"
                                    : "允许固定诊断、测试、人设调整及会话内授权的只读文件检查。保存启用后下一轮生效；发行构建不可启用。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                                color: settingsPage && settingsPage.settingsWindow.themeColors ? settingsPage.settingsWindow.themeColors.colorTextMuted : Theme.snapshot.colorTextMuted
                            }
                        }
                    }
                                }
                            }
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        visible: settingsAdvancedLoader.status === Loader.Loading
                                 || settingsAdvancedLoader.status === Loader.Error
                        BusyIndicator {
                            visible: settingsAdvancedLoader.status === Loader.Loading
                            running: visible
                            Layout.preferredWidth: 24
                            Layout.preferredHeight: 24
                        }
                        ThemeControls.ThemeLabel {
                            Layout.fillWidth: true
                            text: settingsAdvancedLoader.status === Loader.Error
                                  ? "高级设置加载失败。可重试，当前已打开的设置内容仍保留。"
                                  : "正在加载模型、用量与连接设置…"
                            wrapMode: Text.WordWrap
                        }
                        ThemeControls.ThemeButton {
                            visible: settingsAdvancedLoader.status === Loader.Error
                            text: "重试"
                            onClicked: {
                                advancedSettingsCategory.contentRequested = false
                                Qt.callLater(function() {
                                    advancedSettingsCategory.contentRequested = true
                                })
                            }
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                ThemeControls.ThemeButton {
                    objectName: "restoreDefaultsButton"
                    text: "恢复默认"
                    onClicked: {
                        bridge.restoreDefaults()
                        window.restoreFields()
                    }
                }
            }
            Item { Layout.preferredHeight: 12 }
        }
    }

            }
        }
        onLoaded: window.restoreFields()
    }
    ColumnLayout {
        anchors.centerIn: parent
        visible: settingsContentLoader.status === Loader.Loading
        spacing: Theme.snapshot.spaceSm
        BusyIndicator { running: parent.visible; Layout.alignment: Qt.AlignHCenter }
        ThemeControls.ThemeLabel {
            text: "正在加载设置…"
            color: window.themeColors ? window.themeColors.colorTextMuted : window.palette.placeholderText
        }
    }

    ThemeControls.ThemeSurface {
        id: saveActionBar
        objectName: "saveSettingsActionBar"
        raised: true
        z: 10
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.rightMargin: Theme.snapshot.spaceMd
        anchors.bottomMargin: Theme.snapshot.spaceMd

        ThemeControls.ThemeButton {
            objectName: "saveSettingsButton"
            primary: true
            enabled: settingsContentLoader.status === Loader.Ready
            text: "保存"
            onClicked: {
                const form = settingsContentLoader.item
                if (form) form.saveSettingsDraft()
            }
        }
    }
}
