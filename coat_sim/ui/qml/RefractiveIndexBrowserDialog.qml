pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs as FileDialogs
import QtQuick.Layouts
import "." as LocalComponents
import "theme_controls" as ThemeControls

LocalComponents.ThemePageDialog {
    id: dialog
    objectName: "refractiveIndexBrowserDialog"
    required property var browser
    readonly property var tokens: themeTokens
    readonly property var resultRows: browser.results.entries || []
    readonly property var source: browser.entry || ({})
    readonly property var choices: source.conversion ? source.conversion.choices || [] : []
    readonly property var importRows: browser.importSummary.rows || []
    property bool showInstall: false
    property bool showVendor: false
    property bool showReferences: false
    readonly property bool vendorMode: browser.sourceKind === "vendor"
    readonly property var rawSource: source.raw_source || source.raw_yaml || ({})
    property int detailTab: 0
    title: "光学资料离线浏览"
    modal: true
    dim: true
    width: Math.min(1180, parent ? parent.width - 24 : 1180)
    height: Math.min(860, parent ? parent.height - 24 : 860)
    anchors.centerIn: parent
    visible: browser.active
    closePolicy: browser.applying ? Popup.NoAutoClose : Popup.CloseOnEscape
    onClosed: browser.close()

    function asText(value) {
        if (value === undefined || value === null || value === "") return "未提供"
        return typeof value === "string" ? value : JSON.stringify(value, null, 2)
    }
    function wavelengthRangeLabel(blockData) {
        const range = blockData.range_um || (dialog.vendorMode ? dialog.source.wavelength_range_um : null)
        if (!range || range.length !== 2 || !Number.isFinite(range[0]) || !Number.isFinite(range[1])) return ""
        return "\nλ：" + Number(range[0]).toPrecision(6) + "–" + Number(range[1]).toPrecision(6)
            + " µm" + (dialog.vendorMode ? "（参考空气）" : "")
    }
    function capabilityLabel(value) {
        const labels = {supported: "可转换", selection_required: "须选数据块", unsupported: "仅查阅", invalid: "数据无效"}
        return labels[value] || String(value || "未判定")
    }
    function runSearch() {
        dialog.browser.search(query.text, catalog.currentValue, shelf.text, book.text, capability.currentValue, 0)
    }
    Connections {
        target: dialog.browser
        function onSourceChanged() {
            query.text = ""
            shelf.text = ""
            book.text = ""
            catalog.currentIndex = 0
            capability.currentIndex = 0
            blockChoice.currentIndex = -1
            referenceAirConsent.checked = false
            queryDebounce.stop()
        }
        function onSelectionReady() {
            materialName.text = String(dialog.source.name || "").slice(0, 120)
            blockChoice.currentIndex = -1
            referenceAirConsent.checked = false
            conflictChoice.currentIndex = 0
        }
    }
    Timer { id: queryDebounce; interval: 300; onTriggered: dialog.runSearch() }
    FileDialogs.FolderDialog {
        id: sourceFolderPicker
        title: "选择已下载的官方数据库快照目录"
        onAccepted: sourceFolder.text = selectedFolder.toString()
    }

    FileDialogs.FileDialog {
        id: vendorFilePicker
        title: "选择厂商 AGF 目录"
        nameFilters: ["AGF 玻璃目录 (*.agf *.AGF)"]
        onAccepted: vendorFile.text = selectedFile.toString()
    }

    contentItem: ScrollView {
        id: pageScroll
        objectName: "riiBrowserPageScroll"
        contentWidth: availableWidth
        clip: true
        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
        ColumnLayout {
            width: pageScroll.availableWidth
            spacing: dialog.tokens.spaceSm
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                textFormat: Text.PlainText
                wrapMode: Text.WrapAnywhere
                text: dialog.vendorMode
                    ? "厂商来源：" + String(dialog.browser.sourceStatus.manufacturer || "尚未读取")
                      + " · 版本 " + String(dialog.browser.sourceStatus.catalog_version || "未提供")
                      + " · " + String(dialog.browser.sourceStatus.entry_count || 0) + " 条 AGF\n"
                      + String(dialog.browser.sourceStatus.source_filename || "")
                      + " · 来源与版本由用户声明；授权见原厂条款。"
                    : "本地位置：" + dialog.browser.storePath + "\n"
                    + (dialog.browser.sourceStatus.status === "ready"
                       ? "快照：" + String(dialog.browser.sourceStatus.upstream_commit || "")
                         + " · " + String((dialog.browser.sourceStatus.counts || {}).indexed_entries || 0) + " 条来源 · CC0"
                       : "尚无可用的本地数据库。不会自动下载或创建材料。")
            }
            Flow {
                Layout.fillWidth: true
                spacing: dialog.tokens.spaceSm
                ThemeControls.ThemeButton {
                    objectName: "riiInstallToggle"
                    text: dialog.showInstall ? "收起快照导入" : "导入 RII 快照…"
                    visible: !dialog.vendorMode
                    enabled: !dialog.browser.applying && !dialog.browser.installing
                    onClicked: dialog.showInstall = !dialog.showInstall
                }
                ThemeControls.ThemeButton {
                    text: "refractiveindex.info"
                    visible: dialog.vendorMode
                    enabled: !dialog.browser.applying && !dialog.browser.installing
                    onClicked: dialog.browser.showRiiSource()
                }
                ThemeControls.ThemeButton {
                    objectName: "riiOpenVendorToggle"
                    text: dialog.showVendor ? "收起厂商导入" : "打开厂商目录…"
                    enabled: !dialog.browser.applying && !dialog.browser.installing
                    onClicked: dialog.showVendor = !dialog.showVendor
                }
                ThemeControls.ThemeButton {
                    objectName: "riiReferenceSourcesToggle"
                    text: dialog.showReferences ? "收起来源目录" : "来源目录 / Merck…"
                    enabled: !dialog.browser.applying
                    onClicked: {
                        dialog.showReferences = !dialog.showReferences
                        if (dialog.showReferences) dialog.browser.loadReferences()
                    }
                }
                ThemeControls.ThemeButton {
                    objectName: "riiRefreshButton"
                    text: "刷新"
                    enabled: !dialog.browser.applying && !dialog.browser.installing
                    onClicked: dialog.browser.refresh()
                }
                ThemeControls.ThemeButton {
                    text: "取消读取"
                    visible: dialog.browser.busy && !dialog.browser.applying
                    onClicked: dialog.browser.cancelReads()
                }
            }
            ColumnLayout {
                visible: dialog.showVendor
                Layout.fillWidth: true
                ThemeControls.ThemeLabel {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: "打开已下载的厂商 AGF 文件。须填写厂商与目录版本；只转换已支持的条目，未实现公式保留原文查阅。Merck PDF、BGF 与安装包没有数字导入路径，请查看来源目录。"
                }
                GridLayout {
                    columns: dialog.width > 900 ? 3 : 2
                    Layout.fillWidth: true
                    enabled: !dialog.browser.busy
                    ThemeControls.ThemeField {
                        id: vendorFile
                        objectName: "vendorCatalogFile"
                        Layout.fillWidth: true
                        placeholderText: "本地 AGF 文件路径"
                        accessibleLabel: "本地厂商 AGF 文件"
                    }
                    ThemeControls.ThemeButton { text: "选择 AGF…"; onClicked: vendorFilePicker.open() }
                    ThemeControls.ThemeField {
                        id: vendorManufacturer
                        objectName: "vendorManufacturer"
                        Layout.fillWidth: true
                        placeholderText: "厂商（必填）"
                        accessibleLabel: "厂商来源声明"
                        maximumLength: 200
                    }
                    ThemeControls.ThemeField {
                        id: vendorVersion
                        objectName: "vendorCatalogVersion"
                        Layout.fillWidth: true
                        placeholderText: "目录版本（必填）"
                        accessibleLabel: "厂商目录版本声明"
                        maximumLength: 200
                    }
                    ThemeControls.ThemeField {
                        id: vendorUrl
                        Layout.fillWidth: true
                        placeholderText: "官方来源 URL（可留空）"
                        accessibleLabel: "厂商资料来源网址"
                        maximumLength: 2000
                    }
                    ThemeControls.ThemeField {
                        id: vendorLicense
                        Layout.fillWidth: true
                        placeholderText: "许可说明（可留空）"
                        accessibleLabel: "厂商资料许可说明"
                        maximumLength: 2000
                    }
                    ThemeControls.ThemeComboBox {
                        id: vendorEncoding
                        Layout.fillWidth: true
                        model: [{text: "自动 UTF-8 / UTF-16 BOM", value: ""},
                                {text: "Windows-1252（显式）", value: "cp1252"},
                                {text: "Latin-1（显式）", value: "latin1"}]
                        textRole: "text"; valueRole: "value"
                    }
                    ThemeControls.ThemeButton {
                        objectName: "vendorOpenButton"
                        text: "读取厂商目录"
                        enabled: vendorFile.text.trim().length > 0 && vendorManufacturer.text.trim().length > 0 && vendorVersion.text.trim().length > 0
                        onClicked: dialog.browser.openVendorCatalog(vendorFile.text, vendorManufacturer.text,
                            vendorVersion.text, vendorUrl.text, vendorLicense.text, vendorEncoding.currentValue)
                    }
                }
            }
            ColumnLayout {
                visible: dialog.showReferences
                Layout.fillWidth: true
                ThemeControls.ThemeLabel {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: "来源目录仅记录官方入口、格式、授权和限制，不代表这些数据已安装。Merck Patinal 产品 PDF 只作参考，不从图表或混合物名称虚构 n/k。网址只供复制查阅，不会自动联网。"
                }
                ThemeControls.ThemeComboBox {
                    objectName: "opticalReferenceSourceChoice"
                    Layout.fillWidth: true
                    model: dialog.browser.references.items || []
                    textRole: "title"; valueRole: "id"
                    currentIndex: (dialog.browser.references.items || []).findIndex(row => row.id === dialog.browser.referenceId)
                    displayText: currentIndex < 0 ? "选择来源查看资料与限制" : currentText
                    enabled: !dialog.browser.applying
                    onActivated: dialog.browser.selectReference(currentValue)
                }
                ScrollView {
                    id: referenceScroll
                    visible: !!dialog.browser.referenceDetail.source
                    Layout.fillWidth: true
                    Layout.preferredHeight: 240
                    contentWidth: availableWidth
                    clip: true
                    TextArea {
                        objectName: "opticalReferenceSourceDetail"
                        width: referenceScroll.availableWidth
                        readOnly: true
                        selectByMouse: true
                        textFormat: TextEdit.PlainText
                        wrapMode: TextEdit.WrapAnywhere
                        color: dialog.tokens.colorText
                        text: !dialog.browser.referenceDetail.source ? "" : String(dialog.browser.referenceDetail.source.title)
                            + "\n" + String(dialog.browser.referenceDetail.source.summary)
                            + "\n\n资料可用性：" + dialog.asText(dialog.browser.referenceDetail.source.offline_status)
                            + " · 本机安装状态：未检查"
                            + "\n\n适用限制：\n" + dialog.asText(dialog.browser.referenceDetail.source.limitations)
                            + "\n\n官方文件：\n" + dialog.asText(dialog.browser.referenceDetail.source.files)
                            + "\n\n官方入口：\n" + dialog.asText(dialog.browser.referenceDetail.source.links)
                            + "\n\n授权：\n" + dialog.asText(dialog.browser.referenceDetail.source.rights)
                            + "\n\n待确认：\n" + dialog.asText(dialog.browser.referenceDetail.source.unknowns)
                    }
                }
            }
            ColumnLayout {
                visible: dialog.showInstall && !dialog.vendorMode
                Layout.fillWidth: true
                ThemeControls.ThemeLabel {
                    Layout.fillWidth: true
                    wrapMode: Text.Wrap
                    text: "选择已解压的官方数据库目录，填写对应的 40 位提交号。建立离线库会验证本地文件与结构；提交号属于来源声明，不代表已联网核验。"
                }
                RowLayout {
                    Layout.fillWidth: true
                    ThemeControls.ThemeField {
                        id: sourceFolder
                        objectName: "riiSnapshotFolder"
                        Layout.fillWidth: true
                        placeholderText: "官方数据库快照目录"
                        accessibleLabel: "官方数据库快照目录"
                        enabled: !dialog.browser.busy
                    }
                    ThemeControls.ThemeButton { text: "选择…"; enabled: !dialog.browser.busy; onClicked: sourceFolderPicker.open() }
                }
                RowLayout {
                    Layout.fillWidth: true
                    ThemeControls.ThemeField {
                        id: sourceCommit
                        objectName: "riiSnapshotCommit"
                        Layout.fillWidth: true
                        placeholderText: "40 位 upstream commit"
                        accessibleLabel: "快照提交号来源声明"
                        maximumLength: 40
                        enabled: !dialog.browser.busy
                    }
                    ThemeControls.ThemeButton {
                        text: "建立离线库"
                        enabled: !dialog.browser.busy && sourceFolder.text.length > 0 && /^[0-9a-fA-F]{40}$/.test(sourceCommit.text)
                        onClicked: dialog.browser.installSnapshot(sourceFolder.text, sourceCommit.text)
                    }
                }
            }
            ThemeControls.ThemeLabel {
                objectName: "riiBrowserStatus"
                Layout.fillWidth: true
                textFormat: Text.PlainText
                wrapMode: Text.WrapAnywhere
                text: dialog.browser.errorText ? dialog.browser.errorText : dialog.browser.statusText
                color: dialog.browser.errorText ? dialog.tokens.colorError : dialog.tokens.colorText
            }
            GridLayout {
                columns: dialog.width > 900 ? 4 : 2
                Layout.fillWidth: true
                enabled: !dialog.browser.applying && !dialog.browser.installing
                ThemeControls.ThemeField {
                    id: query
                    objectName: "riiSearchField"
                    Layout.fillWidth: true
                    placeholderText: "搜索材料、作者或数据页"
                    maximumLength: 200
                    accessibleLabel: "搜索本地折射率数据库"
                    onTextEdited: queryDebounce.restart()
                    onAccepted: { queryDebounce.stop(); dialog.runSearch() }
                }
                ThemeControls.ThemeComboBox {
                    id: catalog
                    visible: !dialog.vendorMode
                    objectName: "riiCatalogFilter"
                    Layout.fillWidth: true
                    model: [{text: "线性 n/k", value: "nk"}, {text: "非线性 n2（仅查阅）", value: "n2"}, {text: "所有来源", value: "all"}]
                    textRole: "text"; valueRole: "value"
                    onActivated: dialog.runSearch()
                }
                ThemeControls.ThemeField {
                    id: shelf
                    visible: !dialog.vendorMode
                    Layout.fillWidth: true
                    placeholderText: "分类 shelf，可留空"
                    maximumLength: 200
                    accessibleLabel: "来源分类 shelf"
                    onAccepted: dialog.runSearch()
                }
                ThemeControls.ThemeField {
                    id: book
                    visible: !dialog.vendorMode
                    Layout.fillWidth: true
                    placeholderText: "材料 book，可留空"
                    maximumLength: 200
                    accessibleLabel: "来源材料 book"
                    onAccepted: dialog.runSearch()
                }
                ThemeControls.ThemeComboBox {
                    id: capability
                    Layout.fillWidth: true
                    model: [{text: "所有格式", value: ""}, {text: "可转换", value: "supported"},
                            {text: "须选数据块", value: "selection_required"}, {text: "仅查阅", value: "unsupported"},
                            {text: "数据无效", value: "invalid"}]
                    textRole: "text"; valueRole: "value"
                    onActivated: dialog.runSearch()
                }
                ThemeControls.ThemeButton { text: "搜索"; onClicked: { queryDebounce.stop(); dialog.runSearch() } }
            }
            GridLayout {
                Layout.fillWidth: true
                columns: dialog.width > 900 ? 2 : 1
                columnSpacing: dialog.tokens.spaceMd
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.preferredWidth: dialog.width > 900 ? 310 : 600
                    Layout.maximumWidth: dialog.width > 900 ? 350 : 2000
                    Layout.alignment: Qt.AlignTop
                    ListView {
                        id: resultsList
                        objectName: "riiSourceResults"
                        Layout.fillWidth: true
                        Layout.preferredHeight: dialog.width > 900 ? 430 : 180
                        clip: true
                        model: dialog.resultRows
                        enabled: !dialog.browser.applying && !dialog.browser.installing && !dialog.browser.searching
                        ScrollBar.vertical: ScrollBar { }
                        delegate: ItemDelegate {
                            id: resultDelegate
                            required property var modelData
                            width: resultsList.width
                            highlighted: dialog.browser.selectedId === modelData.entry_id
                            contentItem: ColumnLayout {
                                ThemeControls.ThemeLabel { Layout.fillWidth: true; text: String(resultDelegate.modelData.name); textFormat: Text.PlainText; wrapMode: Text.Wrap }
                                ThemeControls.ThemeLabel {
                                    Layout.fillWidth: true
                                    text: String(resultDelegate.modelData.source_path) + "\n"
                                        + dialog.capabilityLabel((resultDelegate.modelData.conversion || {}).status)
                                        + (resultDelegate.modelData.data_quality === "source_inconsistent" ? " · 来源数值不一致警告" : "")
                                    textFormat: Text.PlainText
                                    wrapMode: Text.WrapAnywhere
                                    muted: true
                                }
                            }
                            onClicked: dialog.browser.selectEntry(modelData.entry_id)
                        }
                        ThemeControls.ThemeLabel {
                            anchors.fill: parent
                            visible: resultsList.count === 0 && !dialog.browser.searching && !dialog.browser.errorText
                            text: dialog.browser.sourceStatus.status === "ready" ? "没有匹配来源。请调整筛选。" : "请先建立本地离线库。"
                            wrapMode: Text.Wrap
                            verticalAlignment: Text.AlignVCenter
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        ThemeControls.ThemeButton {
                            text: "上一页"
                            enabled: !dialog.browser.busy && Number(dialog.browser.results.offset || 0) > 0
                            onClicked: dialog.browser.page(Math.max(0, Number(dialog.browser.results.offset) - 50))
                        }
                        ThemeControls.ThemeLabel {
                            Layout.fillWidth: true
                            text: String(dialog.resultRows.length) + " / " + String(dialog.browser.results.total || 0)
                        }
                        ThemeControls.ThemeButton {
                            text: "下一页"
                            enabled: !dialog.browser.busy && !!dialog.browser.results.has_more
                            onClicked: dialog.browser.page(Number(dialog.browser.results.offset || 0) + 50)
                        }
                    }
                }
                ColumnLayout {
                    id: details
                    Layout.fillWidth: true
                    Layout.preferredWidth: 650
                    Layout.alignment: Qt.AlignTop
                    ThemeControls.ThemeLabel {
                        Layout.fillWidth: true
                        textFormat: Text.PlainText
                        wrapMode: Text.WrapAnywhere
                        text: dialog.browser.loadingEntry ? "正在读取详情…" : dialog.source.name || "选择一条来源查看原始资料和曲线。"
                        font.bold: true
                    }
                    ThemeControls.ThemeLabel {
                        Layout.fillWidth: true
                        visible: !!dialog.source.entry_id
                        textFormat: Text.PlainText
                        wrapMode: Text.WrapAnywhere
                        text: dialog.capabilityLabel((dialog.source.conversion || {}).status) + " · "
                            + ((dialog.source.data_types || []).join(" / ")) + "\n"
                            + ((dialog.source.conversion || {}).reason_codes || []).join("；")
                            + (dialog.source.data_quality === "source_inconsistent" ? "\n警告：原厂目录 n_d 与系数不一致，请核对原资料。" : "")
                            + (dialog.vendorMode ? "\nAGF n 与波长按参考空气约定，未做真空/压力/温度修正；k 未知。参考温度（℃）：" + dialog.asText((dialog.source.conditions || {}).reference_temperature_c) : "")
                    }
                    RowLayout {
                        visible: !!dialog.source.entry_id
                        ThemeControls.ThemeButton { text: "曲线与资料"; checked: dialog.detailTab === 0; checkable: true; onClicked: dialog.detailTab = 0 }
                        ThemeControls.ThemeButton { text: dialog.vendorMode ? "原始 AGF" : "原始 YAML"; checked: dialog.detailTab === 1; checkable: true; onClicked: dialog.detailTab = 1 }
                    }
                    ScrollView {
                        id: detailScroll
                        visible: dialog.detailTab === 0
                        Layout.fillWidth: true
                        Layout.preferredHeight: 390
                        contentWidth: availableWidth
                        clip: true
                        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                        ColumnLayout {
                            width: detailScroll.availableWidth
                            visible: dialog.detailTab === 0
                            Repeater {
                                model: dialog.browser.preview.blocks || []
                                ColumnLayout {
                                    id: block
                                    required property var modelData
                                    Layout.fillWidth: true
                                    ThemeControls.ThemeLabel {
                                        objectName: "riiCurveSummary"
                                        Layout.fillWidth: true
                                        textFormat: Text.PlainText
                                        wrapMode: Text.WrapAnywhere
                                        text: "块 " + String(block.modelData.index + 1) + " · " + String(block.modelData.type)
                                            + " · " + (block.modelData.columns || []).join(" / ") + "\n"
                                            + (block.modelData.calculated ? "公式域内显示采样" : "原始数据") + "："
                                            + String(block.modelData.display_point_count || 0) + " 点"
                                            + dialog.wavelengthRangeLabel(block.modelData)
                                            + (block.modelData.sampled && !block.modelData.calculated ? "（源 " + String(block.modelData.source_point_count) + " 点，已下采样）" : "")
                                            + (block.modelData.error ? "\n" + dialog.asText(block.modelData.error) : "")
                                    }
                                    TextArea {
                                        objectName: "riiNonlinearRawValues"
                                        Layout.fillWidth: true
                                        visible: block.modelData.quantity === "n2"
                                        readOnly: true
                                        selectByMouse: true
                                        textFormat: TextEdit.PlainText
                                        wrapMode: TextEdit.WrapAnywhere
                                        color: dialog.tokens.colorText
                                        text: "非线性 n2 原始数值，仅作资料查阅。单位尚未核实，不绘制物理量曲线、不作线性 n/k 导入。\n"
                                            + (block.modelData.points || []).map(row => row.join("  ")).join("\n")
                                    }
                                    Canvas {
                                        id: curve
                                        objectName: "riiSourceCurve"
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: 180
                                        visible: block.modelData.quantity !== "n2" && (block.modelData.points || []).length > 0
                                        property var points: block.modelData.points || []
                                        onPointsChanged: requestPaint()
                                        onWidthChanged: requestPaint()
                                        Connections { target: dialog; function onThemeTokensChanged() { curve.requestPaint() } }
                                        onPaint: {
                                            const ctx = getContext("2d")
                                            ctx.clearRect(0, 0, width, height)
                                            const rows = points
                                            const columns = block.modelData.columns || []
                                            const colors = [dialog.tokens.colorAccent, dialog.tokens.colorWarning, dialog.tokens.colorText]
                                            if (!rows.length || columns.length < 2) return
                                            const xs = rows.map(row => row[0]).filter(Number.isFinite)
                                            const ys = []
                                            for (const row of rows) for (let j = 1; j < row.length; ++j) if (Number.isFinite(row[j])) ys.push(row[j])
                                            if (!xs.length || !ys.length) return
                                            const xmin = Math.min(...xs), xmax = Math.max(...xs)
                                            const ymin = Math.min(...ys), ymax = Math.max(...ys)
                                            const dx = xmax - xmin || 1, dy = ymax - ymin || 1
                                            const left = 55, top = 18, w = Math.max(1, width - 70), h = height - 55
                                            ctx.strokeStyle = dialog.tokens.colorBorder
                                            ctx.strokeRect(left, top, w, h)
                                            ctx.font = String(dialog.tokens.fontSizeSmall * 4 / 3) + "px \"" + dialog.tokens.fontFamily + "\""
                                            ctx.fillStyle = dialog.tokens.colorText
                                            ctx.fillText(ymax.toPrecision(4), 1, top + 6)
                                            ctx.fillText(ymin.toPrecision(4), 1, top + h)
                                            ctx.fillText(xmin.toPrecision(4), left, height - 8)
                                            ctx.fillText(xmax.toPrecision(4), Math.max(left, width - 60), height - 8)
                                            for (let j = 1; j < columns.length; ++j) {
                                                ctx.strokeStyle = colors[(j - 1) % colors.length]
                                                ctx.fillStyle = ctx.strokeStyle
                                                ctx.fillText(String(columns[j]) + (rows.some(row => Number.isFinite(row[j])) ? "" : "（未知）"), left + (j - 1) * 110, 14)
                                                ctx.beginPath()
                                                let connected = false
                                                for (const row of rows) {
                                                    if (!Number.isFinite(row[0]) || !Number.isFinite(row[j])) { connected = false; continue }
                                                    const x = left + (row[0] - xmin) / dx * w, y = top + h - (row[j] - ymin) / dy * h
                                                    if (connected) ctx.lineTo(x, y); else ctx.moveTo(x, y)
                                                    connected = true
                                                    ctx.fillRect(x - 1, y - 1, 2, 2)
                                                }
                                                ctx.stroke()
                                            }
                                        }
                                    }
                                }
                            }
                            TextArea {
                                objectName: "riiSourceReferences"
                                Layout.fillWidth: true
                                readOnly: true
                                selectByMouse: true
                                textFormat: TextEdit.PlainText
                                wrapMode: TextEdit.WrapAnywhere
                                color: dialog.tokens.colorText
                                background: null
                                text: !dialog.source.entry_id ? "" : "分类：\n" + dialog.asText(dialog.source.classifications)
                                    + "\n\n引用：\n" + dialog.asText(dialog.source.references)
                                    + "\n\n说明：\n" + dialog.asText(dialog.source.comments)
                                    + "\n\n条件：\n" + dialog.asText(dialog.source.conditions)
                                    + "\n\n其他属性：\n" + dialog.asText(dialog.source.properties)
                                    + "\n\n来源：" + dialog.asText(dialog.source.source_url)
                                    + "\nSHA256：" + dialog.asText(dialog.source.source_sha256)
                            }
                        }
                    }
                    ScrollView {
                        id: rawScroll
                        Layout.fillWidth: true
                        Layout.preferredHeight: 390
                        visible: dialog.detailTab === 1
                        contentWidth: availableWidth
                        clip: true
                        TextArea {
                            objectName: "riiRawYaml"
                            width: rawScroll.availableWidth
                            readOnly: true
                            selectByMouse: true
                            textFormat: TextEdit.PlainText
                            wrapMode: TextEdit.WrapAnywhere
                            color: dialog.tokens.colorText
                            text: String(dialog.rawSource.text || "")
                                + (dialog.rawSource.truncated ? "\n\n[预览已截断，总字符数 " + String(dialog.rawSource.total_chars) + "]" : "")
                        }
                    }
                    ThemeControls.ThemeLabel {
                        Layout.fillWidth: true
                        visible: !!dialog.source.entry_id
                        wrapMode: Text.Wrap
                        text: "预览曲线不代表可用于当前计算；n2 仅供查阅，未知 k 不补零。导入创建独立光学资料，不推断膜料/基片用途。"
                        muted: true
                    }
                    ThemeControls.ThemeComboBox {
                        id: blockChoice
                        objectName: "riiBlockChoice"
                        Layout.fillWidth: true
                        visible: dialog.choices.length > 0
                        enabled: !dialog.browser.applying
                        model: dialog.choices.map(function(row) {
                            return {text: row.label || "块 " + row.block_selection.map(i => i + 1).join(" + ") + (row.has_k ? " · n/k" : " · k 未知")}
                        })
                        textRole: "text"
                        currentIndex: -1
                        displayText: currentIndex < 0 ? "请选择要转换的数据块" : currentText
                        onActivated: dialog.browser.cancelImportPreview()
                    }
                    CheckBox {
                        id: referenceAirConsent
                        objectName: "vendorReferenceAirConsent"
                        Layout.fillWidth: true
                        visible: dialog.vendorMode && dialog.choices.length > 0
                        enabled: !dialog.browser.applying
                        checked: false
                        focusPolicy: Qt.StrongFocus
                        text: "我确认：以空气参考 n / 波长数值近似用于真空基准；未作温压或空气折射率换算，仅供明确接受此近似的计算。"
                        Accessible.name: text
                        indicator: Rectangle {
                            implicitWidth: Math.max(22, dialog.tokens.fontSizeBody * 1.5)
                            implicitHeight: implicitWidth
                            x: referenceAirConsent.leftPadding
                            y: (referenceAirConsent.height - height) / 2
                            radius: dialog.tokens.radiusSm
                            color: referenceAirConsent.checked ? dialog.tokens.colorAccent : dialog.tokens.colorSurface
                            border.width: referenceAirConsent.activeFocus ? dialog.tokens.focusRingWidth : dialog.tokens.borderWidth
                            border.color: referenceAirConsent.activeFocus ? dialog.tokens.colorFocusRing : dialog.tokens.colorBorderStrong
                            Text {
                                anchors.centerIn: parent
                                text: referenceAirConsent.checked ? "✓" : ""
                                color: dialog.tokens.colorTextOnAccent
                                font.pixelSize: parent.height * 0.8
                            }
                        }
                        contentItem: ThemeControls.ThemeLabel {
                            text: referenceAirConsent.text
                            wrapMode: Text.Wrap
                            leftPadding: referenceAirConsent.indicator.width + referenceAirConsent.spacing
                        }
                        onToggled: dialog.browser.cancelImportPreview()
                    }
                    ThemeControls.ThemeField {
                        id: materialName
                        objectName: "riiImportMaterialName"
                        Layout.fillWidth: true
                        visible: dialog.choices.length > 0
                        enabled: !dialog.browser.applying
                        maximumLength: 120
                        accessibleLabel: "导入材料名称"
                        placeholderText: "导入材料名称"
                        onTextEdited: dialog.browser.cancelImportPreview()
                    }
                }
            }
        }
    }
    footer: ColumnLayout {
        spacing: dialog.tokens.spaceSm
        ThemeControls.ThemeLabel {
            objectName: "riiImportTargetLabel"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            Layout.preferredWidth: 0
            Layout.maximumWidth: Math.max(0, dialog.width - dialog.leftPadding - dialog.rightPadding)
            Layout.leftMargin: dialog.leftPadding
            Layout.rightMargin: dialog.rightPadding
            clip: true
            horizontalAlignment: Text.AlignLeft
            textFormat: Text.PlainText
            text: "导入目标固定为" + dialog.browser.targetLabel
            elide: Text.ElideMiddle
            ToolTip.visible: targetHover.hovered
            ToolTip.text: text
            HoverHandler { id: targetHover }
        }
        ScrollView {
            id: importSummaryScroll
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(150, dialog.height * 0.22)
            visible: dialog.importRows.length > 0
            contentWidth: availableWidth
            clip: true
            TextArea {
                objectName: "riiImportPreviewSummary"
                width: importSummaryScroll.availableWidth
                readOnly: true
                selectByMouse: true
                textFormat: TextEdit.PlainText
                wrapMode: TextEdit.WrapAnywhere
                color: dialog.tokens.colorText
                background: null
            text: dialog.importRows.length ? "待导入：" + dialog.browser.importSummary.name
                + " · " + (dialog.importRows[0].conflict ? "同 ID 已存在" : "新建独立材料")
                + "\n" + dialog.asText(dialog.browser.importSummary.provenance.source_path)
                + (dialog.vendorMode ? " · AGF 单条系数记录" : " · 块 " + dialog.browser.importSummary.provenance.block_selection.map(i => i + 1).join(" + "))
                + (dialog.vendorMode ? "\n已明确接受：空气参考数值近似用于真空基准，未作温压/空气折射率换算。" : "")
                + "\n缺失：" + dialog.asText(dialog.browser.importSummary.missing)
                + "；限制：" + dialog.asText(dialog.browser.importSummary.warnings) : ""
            }
        }
        Flow {
            Layout.fillWidth: true
            spacing: dialog.tokens.spaceSm
            ThemeControls.ThemeComboBox {
                id: conflictChoice
                objectName: "riiImportConflictChoice"
                visible: dialog.importRows.length > 0 && !!dialog.importRows[0].conflict
                enabled: !dialog.browser.applying
                model: [{text: "跳过（默认）", value: "skip"}, {text: "保存为副本", value: "copy"}, {text: "覆盖现有材料", value: "overwrite"}]
                textRole: "text"; valueRole: "value"
            }
            ThemeControls.ThemeButton {
                objectName: "riiPreviewImportButton"
                text: "预览导入…"
                visible: dialog.importRows.length === 0
                enabled: !dialog.browser.busy && blockChoice.currentIndex >= 0 && materialName.text.trim().length > 0
                    && (!dialog.vendorMode || referenceAirConsent.checked)
                onClicked: { conflictChoice.currentIndex = 0; dialog.browser.previewImport(blockChoice.currentIndex, materialName.text, dialog.vendorMode && referenceAirConsent.checked) }
            }
            ThemeControls.ThemeButton {
                objectName: "riiConfirmImportButton"
                text: dialog.browser.applying ? "正在写入…" : (dialog.importRows.length && dialog.importRows[0].conflict && conflictChoice.currentValue === "overwrite" ? "确认覆盖" : "确认处理")
                primary: true
                visible: dialog.importRows.length > 0
                enabled: !dialog.browser.busy
                onClicked: dialog.browser.applyImport(dialog.importRows[0].conflict ? conflictChoice.currentValue : "import")
            }
            ThemeControls.ThemeButton {
                text: "取消预览"
                visible: dialog.importRows.length > 0
                enabled: !dialog.browser.applying
                onClicked: dialog.browser.cancelImportPreview()
            }
            ThemeControls.ThemeButton {
                objectName: "riiCloseButton"
                text: "关闭"
                enabled: !dialog.browser.applying
                onClicked: dialog.browser.close()
            }
        }
    }
}
