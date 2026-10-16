import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "." as LocalComponents
import "theme_controls" as ThemeControls
import "theme_controls/NumericFormat.js" as NumericFormat

LocalComponents.AppWindow {
    id: window
    // The bridge is installed on QQmlContext by both launcher entry points.
    // qmllint disable unqualified
    readonly property var editorBridge: bridge
    // qmllint enable unqualified
    property var projectNavigation: null
    property var projectResources: null
    property bool projectPendingTextEdited: false
    property bool projectCloseAuthorized: false
    LocalComponents.ProjectPendingEdits { editorWindow: window }
    property var editorFields: bridge.fields
    property bool materialLibraryOpenRequested: false
    property bool materialReferenceOpenRequested: false
    Connections {
        target: typeof Theme !== "undefined" ? Theme : null
        function onSnapshotChanged() { preview.requestPaint() }
    }
    titleBase: "基片" + (window.editorBridge.dirty ? " *" : "")
    captionBase: "基片 — " + window.editorBridge.displayName + (window.editorBridge.dirty ? " *" : "")
    projectNameForTitle: projectNavigation ? projectNavigation.projectName : ""
    statusText: bridge.valid ? (bridge.status || "参数有效，可保存。") : bridge.error
    statusSeverity: bridge.valid ? "info" : "error"
    statusDetail: bridge.valid ? "" : bridge.error
    minimumWidth: 1000
    minimumHeight: 650
    onClosing: close => {
        if (window.projectCloseAuthorized) {
            window.projectCloseAuthorized = false
            close.accepted = true
            return
        }
        if (window.projectResources !== null) {
            close.accepted = window.projectResources.requestClose()
        }
    }
    function commitPendingEdits() {
        if (contentItem) contentItem.forceActiveFocus()
    }

    function numericInputText(field) {
        const value = field.numericValueEdited ? field.numericEditText
                                                : field.numericDisplayValue
        return value === undefined || value === null ? "" : String(value)
    }

    function refreshSelectedPoint() {
        const index = bridge.selectedIndex
        if (index < 0) {
            if (pointX.numericDisplayValue !== "") pointX.setNumericSourceValue("")
            if (pointY.numericDisplayValue !== "") pointY.setNumericSourceValue("")
            return
        }
        const points = bridge.points
        if (index < points.length) {
            pointX.setNumericSourceValue(points[index].x)
            pointY.setNumericSourceValue(points[index].y)
        }
    }

    function openMaterialLibrary() {
        if (materialLibraryLoader.item) {
            materialLibraryLoader.item.open()
            return
        }
        materialLibraryOpenRequested = true
        materialLibraryLoader.active = true
    }

    function openMaterialReferenceDialog() {
        if (materialReferenceLoader.item) {
            materialReferenceLoader.item.open()
            return
        }
        materialReferenceOpenRequested = true
        materialReferenceLoader.active = true
    }

    Connections {
        target: bridge
        function onDocumentChanged() {
            window.refreshSelectedPoint()
            preview.requestPaint()
        }
        function onChanged() {
            window.refreshSelectedPoint()
            preview.requestPaint()
        }
    }

    Loader {
        id: materialReferenceLoader
        asynchronous: true
        active: false
        sourceComponent: Component {
            LocalComponents.MaterialReferenceDialog {
                objectName: "substrateMaterialReferenceDialog"
                sections: bridge.materialReferenceSections
            }
        }
        onLoaded: {
            if (!window.materialReferenceOpenRequested) return
            window.materialReferenceOpenRequested = false
            item.open()
        }
        onStatusChanged: {
            if (status !== Loader.Error) return
            window.materialReferenceOpenRequested = false
            active = false
            console.error("Unable to create Substrate material reference dialog")
        }
    }
    Loader {
        id: materialLibraryLoader
        asynchronous: true
        active: false
        sourceComponent: Component {
            LocalComponents.MaterialLibraryDialog {
                objectName: "substrateMaterialLibraryDialog"
                library: bridge.materialLibrary
            }
        }
        onLoaded: {
            if (!window.materialLibraryOpenRequested) return
            window.materialLibraryOpenRequested = false
            item.open()
        }
        onStatusChanged: {
            if (status !== Loader.Error) return
            window.materialLibraryOpenRequested = false
            active = false
            console.error("Unable to create Substrate material library dialog")
        }
    }
    Connections {
        target: bridge
        function onOverwriteRequested(url) { overwriteDialog.targetPath = url.toString(); overwriteDialog.open() }
    }
    LocalComponents.ThemePageDialog {
        id: overwriteDialog
        objectName: "substrateOverwriteDialog"
        property string targetPath: ""
        title: "覆盖已有基片设计？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.No
        onAccepted: bridge.confirmSaveAs(true)
        onRejected: bridge.confirmSaveAs(false)
        ThemeControls.ThemeLabel { text: "文件已存在，确认覆盖？\n" + overwriteDialog.targetPath; wrapMode: Text.WrapAnywhere; width: 440 }
    }

    Connections {
        target: bridge
        function onReloadRequested() { reloadDialog.open() }
        function onDocumentReplaceRequested() { documentReplaceDialog.open() }
    }
    LocalComponents.ThemePageDialog {
        id: documentReplaceDialog
        title: "替换未保存的设计？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.Cancel
        onAccepted: window.editorBridge.confirmDocumentReplace(true)
        onRejected: window.editorBridge.confirmDocumentReplace(false)
        ThemeControls.ThemeLabel { text: "当前基片设计有未保存修改。继续会放弃这些修改并打开或新建设计。"; width: 420; wrapMode: Text.Wrap }
    }
    LocalComponents.ThemePageDialog {
        id: reloadDialog
        objectName: "reloadConfirmDialog"
        title: "重新载入文件？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.Cancel
        onAccepted: bridge.confirmReload(true)
        onRejected: bridge.confirmReload(false)
        ThemeControls.ThemeLabel { text: "刷新将放弃当前设计的未保存修改，重新读取同一文件。取消会保留当前修改。"; width: 420; wrapMode: Text.Wrap }
    }

    FileDialog {
        id: openDialog
        currentFolder: bridge.defaultDirectory
        title: "打开基片"
        fileMode: FileDialog.OpenFile
        nameFilters: ["基片设计 (*.csub)"]
        onAccepted: bridge.openPath(selectedFile)
    }
    FileDialog {
        id: saveDialog
        objectName: "substrateSaveDialog"
        options: FileDialog.DontConfirmOverwrite
        onRejected: bridge.confirmSaveAs(false)
        currentFolder: bridge.defaultDirectory
        title: "基片另存为"
        fileMode: FileDialog.SaveFile
        nameFilters: ["基片设计 (*.csub)"]
        defaultSuffix: "csub"
        onAccepted: bridge.saveAsPath(selectedFile)
    }

    Pane {
        id: pagePane
        anchors.fill: parent
        padding: 0
        background: null
        palette: window.palette

        LocalComponents.ResponsiveOperationBar {
            id: actions
            objectName: "substrateActionArea"
            Accessible.name: "操作栏"
            x: 18
            y: window.contentTopMargin
            width: Math.max(0, parent.width - 36)
            height: childrenRect.height
            spacing: 8
            LocalComponents.ProjectModuleSelector {
                navigation: window.projectNavigation
                currentModuleKey: "substrate"
            }
            LocalComponents.ProjectResourceControls {
                projectResources: window.projectResources
                informationProvider: window.projectResources !== null ? window.projectResources : window.editorBridge.informationController
            }
            ThemeControls.ThemeButton { text: "新建"; visible: window.projectResources === null; onClicked: window.editorBridge.newDocument() }
            ThemeControls.ThemeButton { text: "打开"; visible: window.projectResources === null; onClicked: openDialog.open() }
            ThemeControls.ThemeButton {
                objectName: "reloadCurrentFile"
                text: "刷新"
                Accessible.name: "重新载入当前设计文件"
                enabled: bridge.canReload
                ToolTip.visible: hovered
                ToolTip.text: "从原路径重新读取设计；未保存修改需确认放弃"
                onClicked: bridge.reloadCurrent()
            }
            ThemeControls.ThemeButton {
                text: "保存"
                visible: window.projectResources === null
                enabled: bridge.valid
                onClicked: {
                    if (bridge.currentPath.length === 0) saveDialog.open()
                    else bridge.save()
                }
            }
            ThemeControls.ThemeButton { text: "另存为"; visible: window.projectResources === null; enabled: window.editorBridge.valid; onClicked: saveDialog.open() }
            ThemeControls.ThemeLabel {
                objectName: "substrateResourceDisplayName"
                property bool operationBarFlexible: true
                visible: window.projectResources === null
                text: (window.editorBridge.dirty ? "● " : "") + window.editorBridge.displayName
                textFormat: Text.PlainText
                width: Math.max(0, actions.width - 360)
                height: implicitHeight
                leftPadding: 10
                elide: Text.ElideMiddle
                verticalAlignment: Text.AlignVCenter
                HoverHandler { id: substrateNameHover }
                ToolTip.visible: substrateNameHover.hovered && window.editorBridge.currentPath.length > 0
                ToolTip.text: window.editorBridge.currentPath
            }
        }

        RowLayout {
            objectName: "substrateMainWorkspace"
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: actions.bottom
            anchors.bottom: parent.bottom
            anchors.leftMargin: 18
            anchors.rightMargin: 18
            anchors.topMargin: 14
            anchors.bottomMargin: 12
            spacing: 14

            ThemeControls.ThemeSurface {
                Layout.preferredWidth: Math.min(390, Math.max(330, window.width * 0.3))
                Layout.fillHeight: true
                contentSpacing: 0

                Flickable {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    contentWidth: width
                    contentHeight: form.implicitHeight + 12
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar { palette: window.palette }

                    Column {
                        id: form
                        width: parent.width - 12
                        spacing: 8
                        ThemeControls.ThemeLabel {
                            text: "基片参数"
                            heading: true
                        }
                        ThemeControls.ThemeLabel { visible: false; text: "名称" }
                        ThemeControls.ThemeField {
                            id: nameField
                            objectName: "substrateNameField"
                            visible: false
                            width: parent.width
                            text: window.editorFields.name
                            onTextEdited: bridge.setField("name", text)
                        }
                        ThemeControls.ThemeLabel { text: "基片材料（可选）" }
                        ThemeControls.ThemeComboBox {
                            objectName: "substrateMaterialPicker"
                            width: parent.width
                            model: bridge.materialLibrary.substrateEntries
                            textRole: "name"
                            valueRole: "key"
                            displayText: bridge.materialLibrary.loading ? "正在加载材料…" : bridge.materialName
                            enabled: !bridge.materialLibrary.loading
                            onActivated: bridge.materialLibrary.selectSubstrateMaterial(currentValue)
                        }
                        Flow {
                            width: parent.width
                            spacing: 6
                            ThemeControls.ThemeButton { text: "材料库"; onClicked: { bridge.materialLibrary.prepareForSubstrate(); window.openMaterialLibrary() } }
                            ThemeControls.ThemeButton { text: "参考"; enabled: bridge.hasMaterial && !bridge.materialReferenceLoading; onClicked: window.openMaterialReferenceDialog() }
                            ThemeControls.ThemeButton { text: "移除"; enabled: bridge.hasMaterial; onClicked: bridge.clearMaterial() }
                        ThemeControls.ThemeButton { text: "编辑快照"; visible: bridge.hasMaterial; onClicked: { bridge.editMaterialSnapshot(); window.openMaterialLibrary() } }
                        }
                        ThemeControls.ThemeLabel { width: parent.width; visible: bridge.hasMaterial; wrapMode: Text.Wrap; text: "材料与光学数据保存为独立快照；材料库更新不会更改此设计。"; opacity: 0.75 }
                        Rectangle { width: parent.width; height: 1; color: window.themeColors.colorBorder }
                        ThemeControls.ThemeLabel { text: "物理曲面" }
                        ThemeControls.ThemeComboBox {
                            id: surfaceKind
                            objectName: "substrateSurfaceKind"
                            KeyNavigation.tab: outlineKind
                            width: parent.width
                            model: [
                                { text: "平面", value: "flat" },
                                { text: "球面", value: "spherical" },
                                { text: "非球面", value: "asphere" }
                            ]
                            textRole: "text"
                            valueRole: "value"
                            currentIndex: ["flat", "spherical", "asphere"].indexOf(window.editorFields.surface_kind)
                            onActivated: bridge.setField("surface_kind", currentValue)
                        }
                        ThemeControls.ThemeLabel {
                            width: parent.width
                            wrapMode: Text.WordWrap
                            visible: surfaceKind.currentValue !== "flat" && outlineKind.currentValue !== "circle"
                            color: window.themeColors ? window.themeColors.colorError
                                                      : (window.palette.window.r < 0.5 ? "#ff9992" : "#b03936")
                            text: "球面和非球面只支持圆形轮廓。"
                        }
                        ThemeControls.ThemeLabel { text: "平面轮廓" }
                        ThemeControls.ThemeComboBox {
                            id: outlineKind
                            objectName: "substrateOutlineKind"
                            KeyNavigation.backtab: surfaceKind
                            width: parent.width
                            model: [
                                { text: "圆形", value: "circle" },
                                { text: "矩形", value: "rectangle" },
                                { text: "自定义多边形", value: "polygon" },
                                { text: "闭合曲线", value: "curve" }
                            ]
                            textRole: "text"
                            valueRole: "value"
                            currentIndex: ["circle", "rectangle", "polygon", "curve"].indexOf(window.editorFields.outline_kind)
                            onActivated: bridge.setField("outline_kind", currentValue)
                        }
                        ThemeControls.ThemeLabel { text: "圆半径 / mm"; visible: outlineKind.currentValue === "circle" }
                        ThemeControls.ThemeField {
                            id: radiusField
                            objectName: "substrateRadiusField"
                            width: parent.width
                            visible: outlineKind.currentValue === "circle"
                            numericDisplayValue: window.editorFields.radius_mm
                            inputMethodHints: Qt.ImhFormattedNumbersOnly
                            onTextEdited: bridge.setField("radius_mm", text)
                        }
                        ThemeControls.ThemeLabel { text: "矩形长 / mm"; visible: outlineKind.currentValue === "rectangle" }
                        ThemeControls.ThemeField {
                            id: widthField
                            width: parent.width
                            visible: outlineKind.currentValue === "rectangle"
                            numericDisplayValue: window.editorFields.width_mm
                            inputMethodHints: Qt.ImhFormattedNumbersOnly
                            onTextEdited: bridge.setField("width_mm", text)
                        }
                        ThemeControls.ThemeLabel { text: "矩形宽 / mm"; visible: outlineKind.currentValue === "rectangle" }
                        ThemeControls.ThemeField {
                            id: heightField
                            width: parent.width
                            visible: outlineKind.currentValue === "rectangle"
                            numericDisplayValue: window.editorFields.height_mm
                            inputMethodHints: Qt.ImhFormattedNumbersOnly
                            onTextEdited: bridge.setField("height_mm", text)
                        }

                        ThemeControls.ThemeLabel {
                            visible: outlineKind.currentValue === "polygon" || outlineKind.currentValue === "curve"
                            text: "轮廓编辑工具"
                        }
                        ThemeControls.ThemeComboBox {
                            id: toolKind
                            objectName: "coatcontrol:tool_index"
                            width: parent.width
                            visible: outlineKind.currentValue === "polygon" || outlineKind.currentValue === "curve"
                            model: [
                                { text: "选择 / 拖动", value: "select" },
                                { text: "鼠标添加点", value: "add" },
                                { text: "删除点", value: "delete_point" },
                                { text: "删除线", value: "delete_line" },
                                { text: "闭合", value: "close" }
                            ]
                            textRole: "text"
                            valueRole: "value"
                        }
                        ThemeControls.ThemeLabel {
                            width: parent.width
                            wrapMode: Text.WordWrap
                            visible: toolKind.visible
                            text: bridge.closed ? "轮廓已闭合。拖动控制点可调整形状。"
                                                : "轮廓已打开。编辑完成后点击“闭合轮廓”。"
                        }
                        Row {
                            spacing: 7
                            visible: toolKind.visible
                            ThemeControls.ThemeButton { text: "闭合轮廓"; onClicked: bridge.closeOutline() }
                            ThemeControls.ThemeButton { text: "删除选中点"; enabled: bridge.selectedIndex >= 0; onClicked: bridge.deleteSelectedPoint() }
                        }
                        ThemeControls.ThemeLabel { text: "顶点 / 控制点"; visible: toolKind.visible }
                        ThemeControls.ThemeComboBox {
                            id: pointSelector
                            objectName: "coatcontrol:selected_point_index"
                            width: parent.width
                            visible: toolKind.visible
                            currentIndex: bridge.selectedIndex
                            model: bridge.points.map((p, i) => (i + 1) + ": (" +
                                                     NumericFormat.format(p.x, Qt.locale(), 3) + ", " +
                                                     NumericFormat.format(p.y, Qt.locale(), 3) + ")")
                            onActivated: bridge.selectPoint(currentIndex)
                        }
                        Row {
                            spacing: 8
                            visible: toolKind.visible
                            ThemeControls.ThemeField {
                                id: pointX
                                objectName: "coatcontrol:point_x_mm"
                                width: (form.width - 8) / 2
                                placeholderText: "X / mm"
                                inputMethodHints: Qt.ImhFormattedNumbersOnly
                                onTextEdited: numericDisplayValue = text
                            }
                            ThemeControls.ThemeField {
                                id: pointY
                                objectName: "coatcontrol:point_y_mm"
                                width: pointX.width
                                placeholderText: "Y / mm"
                                inputMethodHints: Qt.ImhFormattedNumbersOnly
                                onTextEdited: numericDisplayValue = text
                            }
                        }
                        Row {
                            spacing: 8
                            visible: toolKind.visible
                            ThemeControls.ThemeButton { text: "添加点"; onClicked: bridge.addPoint(window.numericInputText(pointX), window.numericInputText(pointY)) }
                            ThemeControls.ThemeButton {
                                text: "更新选中点"
                                enabled: bridge.selectedIndex >= 0
                                onClicked: bridge.updateSelectedPoint(window.numericInputText(pointX), window.numericInputText(pointY))
                            }
                        }

                        ThemeControls.ThemeLabel { text: "曲率半径 R / mm"; visible: surfaceKind.currentValue !== "flat" }
                        ThemeControls.ThemeField {
                            id: rocField
                            width: parent.width
                            visible: surfaceKind.currentValue !== "flat"
                            numericDisplayValue: window.editorFields.roc_mm
                            inputMethodHints: Qt.ImhFormattedNumbersOnly
                            onTextEdited: bridge.setField("roc_mm", text)
                        }
                        ThemeControls.ThemeLabel { text: "圆锥常数 k"; visible: surfaceKind.currentValue === "asphere" }
                        ThemeControls.ThemeField {
                            id: conicField
                            width: parent.width
                            visible: surfaceKind.currentValue === "asphere"
                            numericDisplayValue: window.editorFields.conic_k
                            inputMethodHints: Qt.ImhFormattedNumbersOnly
                            onTextEdited: bridge.setField("conic_k", text)
                        }
                        ThemeControls.ThemeLabel { text: "A4 / mm⁻³"; visible: surfaceKind.currentValue === "asphere" }
                        ThemeControls.ThemeField {
                            id: a4Field
                            width: parent.width
                            visible: surfaceKind.currentValue === "asphere"
                            numericDisplayValue: window.editorFields.a4
                            inputMethodHints: Qt.ImhFormattedNumbersOnly
                            onTextEdited: bridge.setField("a4", text)
                        }
                        ThemeControls.ThemeLabel { text: "A6 / mm⁻⁵"; visible: surfaceKind.currentValue === "asphere" }
                        ThemeControls.ThemeField {
                            id: a6Field
                            width: parent.width
                            visible: surfaceKind.currentValue === "asphere"
                            numericDisplayValue: window.editorFields.a6
                            inputMethodHints: Qt.ImhFormattedNumbersOnly
                            onTextEdited: bridge.setField("a6", text)
                        }
                        ThemeControls.ThemeLabel { text: "A8 / mm⁻⁷"; visible: surfaceKind.currentValue === "asphere" }
                        ThemeControls.ThemeField {
                            id: a8Field
                            width: parent.width
                            visible: surfaceKind.currentValue === "asphere"
                            numericDisplayValue: window.editorFields.a8
                            inputMethodHints: Qt.ImhFormattedNumbersOnly
                            onTextEdited: bridge.setField("a8", text)
                        }
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: 8
                color: window.themeColors.colorSurface
                border.color: window.themeColors.colorBorder

                ThemeControls.ThemeLabel {
                    id: previewTitle
                    anchors.left: parent.left
                    anchors.top: parent.top
                    anchors.margins: 18
                    text: "俯视轮廓与径向截面 · 局部 mm"
                            heading: true
                }
                Canvas {
                    id: preview
                    objectName: "substratePreview"
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: previewTitle.bottom
                    anchors.bottom: parent.bottom
                    anchors.margins: 18
                    onWidthChanged: requestPaint()
                    onHeightChanged: requestPaint()
                    onPaint: {
                        const c = getContext("2d")
                        c.clearRect(0, 0, width, height)
                        if (width < 120 || height < 120) return
                        const f = bridge.fields
                        const radius = bridge.previewRadius
                        const top = height * 0.64
                        const scale = Math.max(1e-9, Math.min((width - 75) / (2.2 * radius),
                                                             (top - 70) / (2.2 * radius)))
                        const cx = width / 2, cy = top / 2 + 5
                        const darkCanvas = window.themePaletteMode === "dark"
                        const gridColor = window.themeColors ? window.themeColors.colorBorder : (darkCanvas ? "#506475" : "#d4dde6")
                        const axisColor = window.themeColors ? window.themeColors.colorBorderStrong : (darkCanvas ? "#8395a1" : "#8a9ba5")
                        const guideColor = window.themeColors ? window.themeColors.colorBorder : (darkCanvas ? "#506475" : "#d4dde6")
                        const secondaryLabelColor = window.themeColors ? window.themeColors.colorTextMuted : (darkCanvas ? "#c0ccd4" : "#506475")
                        function px(x) { return cx + x * scale }
                        function py(y) { return cy - y * scale }
                        c.strokeStyle = gridColor
                        c.globalAlpha = 0.55
                        c.lineWidth = 1
                        c.beginPath()
                        for (let i = 1; i < 4; i += 2) {
                            const offsetX = (width - 36) * (i - 2) / 4
                            const offsetY = (top - 36) * (i - 2) / 4
                            c.moveTo(cx + offsetX, 18); c.lineTo(cx + offsetX, top - 18)
                            c.moveTo(18, cy + offsetY); c.lineTo(width - 18, cy + offsetY)
                        }
                        c.stroke()
                        c.globalAlpha = 1
                        c.strokeStyle = axisColor
                        c.lineWidth = 1
                        c.beginPath(); c.moveTo(18, cy); c.lineTo(width - 18, cy)
                        c.moveTo(cx, 18); c.lineTo(cx, top - 18); c.stroke()
                        c.fillStyle = secondaryLabelColor
                        c.font = window.themeColors ? window.themeColors.fontSizeBody + "pt sans-serif" : "14px sans-serif"
                        c.fillText("+X", width - 45, cy - 8)
                        c.fillText("+Y", cx + 8, 26)
                        c.strokeStyle = bridge.valid ? (darkCanvas ? "#69baff" : "#246da7")
                                                     : (darkCanvas ? "#ff907f" : "#bc4d42")
                        c.fillStyle = darkCanvas ? "#365775" : "#d6e8f7"
                        c.lineWidth = 2
                        if (f.outline_kind === "circle") {
                            const r = Number(f.radius_mm) * scale
                            if (Number.isFinite(r) && r > 0) {
                                c.beginPath(); c.arc(cx, cy, r, 0, Math.PI * 2)
                                c.fill(); c.stroke()
                            }
                        } else {
                            let points = []
                            if (f.outline_kind === "rectangle") {
                                const hw = Number(f.width_mm) / 2, hh = Number(f.height_mm) / 2
                                points = [{x:-hw,y:-hh},{x:hw,y:-hh},{x:hw,y:hh},{x:-hw,y:hh}]
                            } else {
                                points = bridge.outlinePoints
                            }
                            if (points.length > 0) {
                                c.beginPath(); c.moveTo(px(points[0].x), py(points[0].y))
                                for (let i = 1; i < points.length; i++) c.lineTo(px(points[i].x), py(points[i].y))
                                if (f.outline_kind === "rectangle" || bridge.closed) c.closePath()
                                if (f.outline_kind !== "curve" && (f.outline_kind === "rectangle" || bridge.closed)) c.fill()
                                c.stroke()
                            }
                            if (f.outline_kind === "polygon" || f.outline_kind === "curve") {
                                const controls = bridge.points
                                c.strokeStyle = guideColor
                                c.setLineDash([4, 4])
                                if (controls.length > 1) {
                                    c.beginPath(); c.moveTo(px(controls[0].x), py(controls[0].y))
                                    for (let j = 1; j < controls.length; j++) c.lineTo(px(controls[j].x), py(controls[j].y))
                                    if (bridge.closed) c.closePath()
                                    c.stroke()
                                }
                                c.setLineDash([])
                                for (let k = 0; k < controls.length; k++) {
                                    c.fillStyle = k === bridge.selectedIndex ? "#d45435" : "#f0a451"
                                    c.beginPath(); c.arc(px(controls[k].x), py(controls[k].y), 6, 0, Math.PI * 2)
                                    c.fill()
                                    c.fillStyle = secondaryLabelColor
                                    c.fillText(String(k + 1), px(controls[k].x) + 8, py(controls[k].y) - 8)
                                }
                            }
                        }
                        c.fillStyle = darkCanvas ? "#ff9979" : "#bd503d"
                        c.beginPath(); c.arc(cx, cy, 3, 0, Math.PI * 2); c.fill()
                        c.strokeStyle = axisColor
                        c.beginPath(); c.moveTo(10, top); c.lineTo(width - 10, top); c.stroke()
                        c.fillStyle = secondaryLabelColor
                        c.fillText("径向截面 h(x) / mm", 16, top + 22)
                        const section = bridge.sectionPoints
                        if (section.length > 1) {
                            let minH = Infinity, maxH = -Infinity
                            for (const p of section) { minH = Math.min(minH, p.y); maxH = Math.max(maxH, p.y) }
                            const span = Math.max(maxH - minH, 1e-8)
                            const y0 = top + 36, y1 = height - 25
                            c.strokeStyle = darkCanvas ? "#ff9979" : "#c35a3d"; c.lineWidth = 2
                            c.beginPath()
                            for (let i = 0; i < section.length; i++) {
                                const p = section[i]
                                const sx = width / 2 + p.x * (width - 50) / (2 * radius)
                                const sy = y1 - (p.y - minH) * (y1 - y0) / span
                                if (i === 0) c.moveTo(sx, sy); else c.lineTo(sx, sy)
                            }
                            c.stroke()
                            c.fillStyle = secondaryLabelColor
                            c.fillText(NumericFormat.format(minH, Qt.locale(), 3) + " ~ " + NumericFormat.format(maxH, Qt.locale(), 3),
                                       width - 190, top + 22)
                        }
                    }
                    MouseArea {
                        anchors.fill: parent
                        enabled: outlineKind.currentValue === "polygon" || outlineKind.currentValue === "curve"
                        onPressed: bridge.canvasPress(mouse.x, mouse.y, preview.width, preview.height,
                                                      toolKind.currentValue)
                        onPositionChanged: {
                            if (pressed && toolKind.currentValue === "select")
                                bridge.canvasDrag(mouse.x, mouse.y, preview.width, preview.height)
                        }
                        onReleased: bridge.canvasRelease()
                    }
                }
            }
        }

    }

}
