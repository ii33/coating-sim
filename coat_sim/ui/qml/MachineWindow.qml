pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "." as LocalComponents
import "theme_controls" as ThemeControls

LocalComponents.AppWindow {
    id: win
    readonly property var editorBridge: bridge
    // The launcher is injected into this window's QQmlContext by the host.
    // qmllint disable unqualified
    readonly property var moduleLauncher: typeof launcher !== "undefined" ? launcher : null
    // qmllint enable unqualified
    property var projectNavigation: null
    property var projectResources: null
    property bool projectPendingTextEdited: false
    property bool projectCloseAuthorized: false
    LocalComponents.ProjectPendingEdits { editorWindow: win }
    titleBase: "机器结构"
    captionBase: titleBase + " — " + (win.projectResources !== null
             ? win.projectResources.displayName
             : (win.editorBridge.informationController.resourceInformation.name || "未命名机器"))
             + (editorBridge.dirty ? " *" : "")
    projectNameForTitle: projectNavigation ? projectNavigation.projectName : ""
    statusText: !editorBridge.canSave && editorBridge.validationMessage.length > 0
                ? editorBridge.validationMessage : editorBridge.status
    statusSeverity: !editorBridge.canSave && editorBridge.validationMessage.length > 0
                    ? "error" : "info"
    statusDetail: {
        const status = String(editorBridge.status || "")
        const validation = String(editorBridge.validationMessage || "")
        return !editorBridge.canSave && validation.length > 0
            ? (status.length > 0 ? status + "\n" : "") + validation
            : status
    }
    minimumWidth: 920
    minimumHeight: 620
    onClosing: close => {
        if (win.projectCloseAuthorized) {
            win.projectCloseAuthorized = false
            close.accepted = true
            return
        }
        if (win.projectResources !== null) {
            close.accepted = win.projectResources.requestClose()
        }
    }
    function commitPendingEdits() {
        if (contentItem) contentItem.forceActiveFocus()
    }

    property var doc: ({})
    property var maskPlanes: []
    property bool previewRequested: false
    property bool previewScheduled: false
    readonly property var previewPlan: ({
        carrier_x_mm: doc.carrier_x_mm, carrier_y_mm: doc.carrier_y_mm,
        planet_offset_mm: doc.planet_offset_mm, sources: doc.sources || [],
        monitors: doc.monitors || [], masks: maskPlanes
    })

    Connections {
        target: typeof Theme !== "undefined" ? Theme : null
        function onSnapshotChanged() {
            if (win.previewRequested) {
                topView.requestPaint()
                sideView.requestPaint()
            }
        }
    }
    Connections {
        target: win
        function onFrameSwapped() {
            if (!win.visible || win.previewScheduled) return
            win.previewScheduled = true
            Qt.callLater(function() {
                if (!win.visible) {
                    win.previewScheduled = false
                    return
                }
                win.previewRequested = true
                editorBridge.preparePreview()
            })
        }
    }

    function readPlanes() {
        try { maskPlanes = JSON.parse(editorBridge.maskPlanesJson) }
        catch (e) { maskPlanes = [] }
        if (win.previewRequested) {
            topView.requestPaint()
            sideView.requestPaint()
        }
    }
    function documentDisplayName(path) {
        const value = String(path || "")
        return value.slice(Math.max(value.lastIndexOf("/"), value.lastIndexOf("\\")) + 1)
    }
    property string selectedFilePointer: ""
    property string filePurpose: ""

    function readDocument() {
        try { doc = JSON.parse(editorBridge.documentJson) }
        catch (e) { doc = ({}) }
        if (win.previewRequested) {
            topView.requestPaint()
            sideView.requestPaint()
        }
    }
    function putNumber(pointer, value) {
        let number = Number(value)
        if (isFinite(number)) editorBridge.setValue(pointer, JSON.stringify(number))
    }
    function ratioRadius(teeth) {
        return Number(teeth || 0) * Number(doc.gear_module_mm || 0) / 2
    }
    function chooseDependency(pointer) {
        selectedFilePointer = pointer
        filePurpose = "dependency"
        openDialog.open()
    }
    function newMachineDocument(initial) {
        const content = JSON.stringify({
            schema_version: 5, carrier_x_mm: 0, carrier_y_mm: 0,
            planet_offset_mm: 312, planet_tilt_deg: 0, substrate_z_mm: 580,
            sun_teeth: 120, planet_teeth: 36, gear_module_mm: 4,
            sources: [], masks: [], monitors: []
        })
        if (initial) win.editorBridge.initializeDocument(content)
        else win.editorBridge.newDocument(content)
    }

    Component.onCompleted: {
        if (!editorBridge.documentPath && editorBridge.documentJson === "{}")
            newMachineDocument(true)
        readDocument()
    }
    Connections {
        target: editorBridge
        function onDocumentChanged() { win.readDocument() }
        function onPlanesChanged() { win.readPlanes() }
    }


    Connections {
        target: editorBridge
        function onOverwriteRequested(url) { overwriteDialog.targetPath = url.toString(); overwriteDialog.open() }
        function onReloadRequested() { reloadDialog.open() }
        function onDocumentReplaceRequested() { documentReplaceDialog.open() }
    }
    ThemeControls.ThemeDialogBase {
        palette: win.palette
        id: documentReplaceDialog
        title: "替换未保存的设计？"
        modal: true
        anchors.centerIn: parent
        width: Math.min(480, win.width - 40)
        standardButtons: Dialog.Yes | Dialog.Cancel
        onAccepted: win.editorBridge.confirmDocumentReplace(true)
        onRejected: win.editorBridge.confirmDocumentReplace(false)
        contentItem: Label { text: "当前机器设计有未保存修改。继续会放弃这些修改并打开或新建设计。"; wrapMode: Text.Wrap }
    }
    ThemeControls.ThemeDialogBase {
        palette: win.palette
        id: reloadDialog
        objectName: "reloadDialog"
        title: "放弃修改并刷新？"
        modal: true
        anchors.centerIn: parent
        width: Math.min(480, win.width - 40)
        standardButtons: Dialog.Yes | Dialog.No
        onAccepted: editorBridge.confirmReload(true)
        onRejected: editorBridge.confirmReload(false)
        contentItem: Label { text: "刷新会重新读取已打开的文件及依赖，并放弃当前未保存修改。不会写入文件。"; wrapMode: Text.Wrap }
    }
    ThemeControls.ThemeDialogBase {
        palette: win.palette
        id: overwriteDialog
        objectName: "machineOverwriteDialog"
        property string targetPath: ""
        title: "覆盖已有设计？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.No
        onAccepted: editorBridge.confirmSaveAs(true)
        onRejected: editorBridge.confirmSaveAs(false)
        ThemeControls.ThemeLabel { text: "文件已存在，确认覆盖？\n" + overwriteDialog.targetPath; wrapMode: Text.WrapAnywhere; width: 440 }
    }

    FileDialog {
        id: openDialog
        currentFolder: filePurpose === "open" ? editorBridge.defaultDirectory : editorBridge.dependencyDirectory(selectedFilePointer)
        title: filePurpose === "open" ? "打开机器" : "选择依赖文件"
        fileMode: FileDialog.OpenFile
        nameFilters: filePurpose === "open" ? ["机器设计 (*.cmac)"] : selectedFilePointer.indexOf("sources") >= 0 ? ["蒸发源设计 (*.csrc)"] : ["修正板设计 (*.cmsk)"]
        onAccepted: {
            if (filePurpose === "open") editorBridge.loadDocument(selectedFile.toString())
            else editorBridge.setDependencyFile(selectedFilePointer, selectedFile.toString())
        }
    }
    FileDialog {
        id: saveAsDialog
        options: FileDialog.DontConfirmOverwrite
        onRejected: editorBridge.confirmSaveAs(false)
        currentFolder: editorBridge.defaultDirectory
        title: "机器另存为"
        fileMode: FileDialog.SaveFile
        nameFilters: ["机器设计 (*.cmac)"]
        defaultSuffix: "cmac"
        onAccepted: editorBridge.saveDocumentAs(selectedFile.toString())
    }

    Pane {
        anchors.fill: parent
        padding: 0
        background: null
        palette: win.palette
        font.family: win.themeColors.fontFamily
        font.pointSize: win.themeColors.fontSizeBody
        font.weight: win.themeColors.fontWeightNormal

        LocalComponents.ResponsiveOperationBar {
            id: actions
            objectName: "machineActions"
            Accessible.name: "操作栏"
            height: implicitHeight
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.topMargin: win.contentTopMargin
            property real leftMargin: 16
            property real rightMargin: 16
            anchors.leftMargin: leftMargin
            anchors.rightMargin: rightMargin
            spacing: 8
            LocalComponents.ProjectModuleSelector {
                navigation: win.projectNavigation
                currentModuleKey: "machine"
            }
            LocalComponents.ProjectResourceControls {
                projectResources: win.projectResources
                informationProvider: win.projectResources !== null ? win.projectResources : win.editorBridge.informationController
            }
            ThemeControls.ThemeButton {
                text: "新建"
                visible: win.projectResources === null
                onClicked: newMachineDocument()
            }
            ThemeControls.ThemeButton {
                text: "打开"
                visible: win.projectResources === null
                onClicked: { win.filePurpose = "open"; openDialog.open() }
            }
            ThemeControls.ThemeButton {
                objectName: "reloadButton"
                Accessible.name: "刷新并重新载入文件"
                text: "刷新"
                enabled: editorBridge.canReload
                onClicked: editorBridge.reloadCurrent()
                ToolTip.visible: hovered
                ToolTip.text: "重新读取当前文件和依赖"
            }
            ThemeControls.ThemeButton {
                text: "保存"
                visible: win.projectResources === null
                enabled: editorBridge.canSave
                onClicked: {
                    if (editorBridge.documentPath) editorBridge.saveDocument()
                    else saveAsDialog.open()
                }
            }
            ThemeControls.ThemeButton {
                text: "另存为"
                visible: win.projectResources === null
                enabled: editorBridge.canSave
                onClicked: saveAsDialog.open()
            }
            ThemeControls.ThemeLabel {
                objectName: "machineResourceDisplayName"
                property bool operationBarFlexible: true
                visible: win.projectResources === null
                width: Math.min(420, Math.max(180, actions.width * 0.34))
                text: (win.editorBridge.dirty ? "● " : "")
                      + (win.projectResources !== null ? win.projectResources.displayName
                         : (win.editorBridge.informationController.resourceInformation.name || "未命名机器"))
                textFormat: Text.PlainText
                elide: Text.ElideMiddle
                wrapMode: Text.WrapAnywhere
                color: win.themeColors.colorText
                HoverHandler { id: machineNameHover }
                ToolTip.visible: machineNameHover.hovered && win.editorBridge.documentPath.length > 0
                ToolTip.text: win.editorBridge.documentPath
            }
        }

        SplitView {
            objectName: "machineWorkspace"
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: actions.bottom
            anchors.bottom: parent.bottom
            anchors.margins: 12
            orientation: Qt.Horizontal

            ScrollView {
                objectName: "machineSettingsScroll"
                palette: win.palette
                SplitView.preferredWidth: 420
                SplitView.minimumWidth: 300
                clip: true
                contentWidth: availableWidth

                ColumnLayout {
                    width: parent.width - 14
                    spacing: 8

                    GroupBox {
                        title: "机器几何 · mm"
                        Layout.fillWidth: true
                        padding: 6
                        topPadding: 25
                        bottomPadding: 6
                        GridLayout {
                            id: geometryFields
                            anchors.fill: parent
                            columns: win.width < 1120 ? 1 : 2
                            columnSpacing: 12
                            rowSpacing: 4
                            Repeater {
                                model: [
                                    {key:"carrier_x_mm", label:"盘心 X", unit:"mm"},
                                    {key:"carrier_y_mm", label:"盘心 Y", unit:"mm"},
                                    {key:"planet_offset_mm", label:"偏心距", unit:"mm"},
                                    {key:"planet_tilt_deg", label:"径向倾角", unit:"°"},
                                    {key:"substrate_z_mm", label:"基片高度", unit:"mm"},
                                    {key:"sun_teeth", label:"太阳齿数", unit:"齿"},
                                    {key:"planet_teeth", label:"行星齿数", unit:"齿"},
                                    {key:"gear_module_mm", label:"齿轮模数", unit:"mm"}
                                ]
                                delegate: RowLayout {
                                    id: geometrySlot
                                    required property var modelData
                                    Layout.fillWidth: true
                                    spacing: 4
                                    ThemeControls.ThemeLabel {
                                        text: geometrySlot.modelData.label
                                        Layout.preferredWidth: 72
                                        Layout.maximumWidth: 72
                                        elide: Text.ElideRight
                                    }
                                    ThemeControls.ThemeField {
                                        objectName: "geometryField_" + geometrySlot.modelData.key
                                        accessibleLabel: geometrySlot.modelData.label
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 82
                                        Layout.preferredWidth: 88
                                        Layout.maximumWidth: 96
                                        numericDisplayValue: win.doc[geometrySlot.modelData.key] === undefined ? "" : win.doc[geometrySlot.modelData.key]
                                        selectByMouse: true
                                        onEditingFinished: if (numericValueEdited) win.putNumber("/" + geometrySlot.modelData.key, text)
                                    }
                                    ThemeControls.ThemeLabel {
                                        text: geometrySlot.modelData.unit
                                        Layout.preferredWidth: 24
                                        Layout.maximumWidth: 24
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel {
                                Layout.columnSpan: geometryFields.columns
                                Layout.fillWidth: true
                                text: "倾角为正：朝大盘中心的一侧抬高；倾斜方向随公转旋转。"
                                wrapMode: Text.WordWrap
                                color: win.themeColors.colorTextMuted
                            }
                            RowLayout {
                                Layout.columnSpan: geometryFields.columns
                                Layout.fillWidth: true
                                spacing: 4
                                ThemeControls.ThemeLabel { text: "太阳轮节圆半径"; Layout.preferredWidth: 128; Layout.maximumWidth: 128 }
                                ThemeControls.ThemeField {
                                    objectName: "solarRadiusField"
                                    accessibleLabel: "太阳轮节圆半径 mm"
                                    Layout.preferredWidth: 100
                                    Layout.maximumWidth: 112
                                    numericDisplayValue: win.ratioRadius(win.doc.sun_teeth)
                                    selectByMouse: true
                                    onEditingFinished: if (numericValueEdited) {
                                        let teeth = Number(win.doc.sun_teeth)
                                        if (teeth > 0) win.putNumber("/gear_module_mm", 2 * Number(text) / teeth)
                                    }
                                }
                                ThemeControls.ThemeLabel { text: "mm"; Layout.preferredWidth: 24; Layout.maximumWidth: 24 }
                                Item { Layout.fillWidth: true }
                            }
                            RowLayout {
                                Layout.columnSpan: geometryFields.columns
                                Layout.fillWidth: true
                                spacing: 4
                                ThemeControls.ThemeLabel { text: "行星轮节圆半径"; Layout.preferredWidth: 128; Layout.maximumWidth: 128 }
                                ThemeControls.ThemeField {
                                    objectName: "planetRadiusField"
                                    accessibleLabel: "行星轮节圆半径 mm"
                                    Layout.preferredWidth: 100
                                    Layout.maximumWidth: 112
                                    numericDisplayValue: win.ratioRadius(win.doc.planet_teeth)
                                    selectByMouse: true
                                    onEditingFinished: if (numericValueEdited) {
                                        let teeth = Number(win.doc.planet_teeth)
                                        if (teeth > 0) win.putNumber("/gear_module_mm", 2 * Number(text) / teeth)
                                    }
                                }
                                ThemeControls.ThemeLabel { text: "mm"; Layout.preferredWidth: 24; Layout.maximumWidth: 24 }
                                Item { Layout.fillWidth: true }
                            }
                        }
                    }

                    GroupBox {
                        title: "蒸发源 · 机器内位置"
                        Layout.fillWidth: true
                        padding: 6
                        topPadding: 25
                        bottomPadding: 6
                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 4
                            Repeater {
                                model: win.doc.sources || []
                                delegate: Frame {
                                    id: sourceSlot
                                    required property var modelData
                                    required property int index
                                    property int slotIndex: sourceSlot.index
                                    Layout.fillWidth: true
                                    padding: 5
                                    ColumnLayout {
                                        anchors.fill: parent
                                        spacing: 3
                                        RowLayout {
                                            ThemeControls.ThemeLabel { text: "源 " + (sourceSlot.slotIndex + 1); font.bold: true }
                                            Item { Layout.fillWidth: true }
                                            ThemeControls.ThemeButton {
                                                objectName: "deleteSourceButton"
                                                text: "×"
                                                Accessible.name: "删除蒸发源 " + (sourceSlot.slotIndex + 1)
                                                ToolTip.visible: hovered
                                                ToolTip.text: "删除蒸发源 " + (sourceSlot.slotIndex + 1)
                                                onClicked: win.editorBridge.removeItem("/sources", sourceSlot.slotIndex)
                                            }
                                        }
                                        RowLayout {
                                            ThemeControls.ThemeLabel { text: "源文件" }
                                            ThemeControls.ThemeLabel {
                                                objectName: "sourceDependencyLabel" + sourceSlot.slotIndex
                                                visible: win.projectResources === null
                                                Layout.fillWidth: true
                                                Layout.minimumWidth: 80
                                                elide: Text.ElideMiddle
                                                text: win.documentDisplayName(win.editorBridge.dependencyPath(
                                                    "/sources/" + sourceSlot.slotIndex + "/file", sourceSlot.modelData.file || ""))
                                            }
                                            LocalComponents.ProjectResourceDependencyPicker {
                                                objectName: "machineSourceDependencyPicker"
                                                projectResources: win.projectResources
                                                kind: "source"
                                                pointer: "/sources/" + sourceSlot.slotIndex + "/file"
                                                currentReference: sourceSlot.modelData.file || ""
                                                visible: win.projectResources !== null
                                                Layout.fillWidth: true
                                                Layout.minimumWidth: 90
                                            }
                                            ThemeControls.ThemeButton {
                                                objectName: "importSourceButton"
                                                text: "导入"
                                                ToolTip.visible: hovered
                                                ToolTip.text: win.projectResources !== null
                                                    ? "导入文件；项目模式会复制到当前项目"
                                                    : "导入并引用外部蒸发源文件"
                                                onClicked: win.chooseDependency("/sources/" + sourceSlot.slotIndex + "/file")
                                            }
                                            ThemeControls.ThemeButton {
                                                objectName: "sourceEditorActionButton"
                                                property string sourceEditorPath: win.editorBridge.dependencyPath("/sources/" + sourceSlot.slotIndex + "/file", sourceSlot.modelData.file || "")
                                                text: sourceEditorPath.length > 0 ? "编辑蒸发源" : "新建"
                                                onClicked: {
                                                    if (win.projectResources !== null)
                                                        win.projectResources.openDependencyEditor("/sources/" + sourceSlot.slotIndex + "/file")
                                                    else
                                                        win.moduleLauncher.openWindow("source", sourceEditorPath)
                                                }
                                            }
                                        }
                                        RowLayout {
                                            ThemeControls.ThemeLabel { text: "X / mm" }
                                            ThemeControls.ThemeField {
                                                objectName: "sourceXField" + sourceSlot.slotIndex
                                                accessibleLabel: "蒸发源 " + (sourceSlot.slotIndex + 1) + " X mm"
                                                Layout.preferredWidth: 94
                                                Layout.maximumWidth: 112
                                                Layout.fillWidth: true
                                                numericDisplayValue: sourceSlot.modelData.x_mm
                                                onEditingFinished: if (numericValueEdited) win.putNumber("/sources/" + sourceSlot.slotIndex + "/x_mm", text)
                                            }
                                            ThemeControls.ThemeLabel { text: "Y / mm" }
                                            ThemeControls.ThemeField {
                                                objectName: "sourceYField" + sourceSlot.slotIndex
                                                accessibleLabel: "蒸发源 " + (sourceSlot.slotIndex + 1) + " Y mm"
                                                Layout.preferredWidth: 94
                                                Layout.maximumWidth: 112
                                                Layout.fillWidth: true
                                                numericDisplayValue: sourceSlot.modelData.y_mm
                                                onEditingFinished: if (numericValueEdited) win.putNumber("/sources/" + sourceSlot.slotIndex + "/y_mm", text)
                                            }
                                        }
                                        RowLayout {
                                            ThemeControls.ThemeLabel { text: "Z / mm" }
                                            ThemeControls.ThemeField {
                                                objectName: "sourceZField" + sourceSlot.slotIndex
                                                accessibleLabel: "蒸发源 " + (sourceSlot.slotIndex + 1) + " Z mm"
                                                Layout.preferredWidth: 108
                                                Layout.maximumWidth: 128
                                                Layout.fillWidth: true
                                                numericDisplayValue: sourceSlot.modelData.z_mm === undefined ? 0 : sourceSlot.modelData.z_mm
                                                onEditingFinished: if (numericValueEdited) win.putNumber("/sources/" + sourceSlot.slotIndex + "/z_mm", text)
                                            }
                                        }
                                    }
                                }
                            }
                            ThemeControls.ThemeButton {
                                text: "＋ 添加蒸发源"
                                enabled: (win.doc.sources || []).length < 64
                                onClicked: editorBridge.appendItem("/sources", JSON.stringify({file:"",x_mm:0,y_mm:0,z_mm:0}))
                            }
                        }
                    }

                    GroupBox {
                        title: "修正板"
                        Layout.fillWidth: true
                        padding: 6
                        topPadding: 25
                        bottomPadding: 6
                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 4
                            Repeater {
                                model: win.doc.masks || []
                                delegate: RowLayout {
                                    id: maskSlot
                                    required property var modelData
                                    required property int index
                                    property int slotIndex: maskSlot.index
                                    Layout.fillWidth: true
                                    ThemeControls.ThemeLabel { text: "板 " + (maskSlot.slotIndex + 1) }
                                    ThemeControls.ThemeLabel {
                                        objectName: "maskDependencyLabel" + maskSlot.slotIndex
                                        visible: win.projectResources === null
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 80
                                        elide: Text.ElideMiddle
                                        text: win.documentDisplayName(win.editorBridge.dependencyPath(
                                            "/masks/" + maskSlot.slotIndex + "/file", maskSlot.modelData.file || ""))
                                    }
                                    LocalComponents.ProjectResourceDependencyPicker {
                                        objectName: "machineMaskDependencyPicker"
                                        projectResources: win.projectResources
                                        kind: "mask"
                                        pointer: "/masks/" + maskSlot.slotIndex + "/file"
                                        currentReference: maskSlot.modelData.file || ""
                                        visible: win.projectResources !== null
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 90
                                    }
                                    ThemeControls.ThemeButton {
                                        objectName: "importMaskButton"
                                        text: "导入"
                                        ToolTip.visible: hovered
                                        ToolTip.text: win.projectResources !== null
                                            ? "导入文件；项目模式会复制到当前项目"
                                            : "导入并引用外部修正板文件"
                                        onClicked: win.chooseDependency("/masks/" + maskSlot.slotIndex + "/file")
                                    }
                                    ThemeControls.ThemeButton {
                                        objectName: "maskEditorActionButton"
                                        property string maskEditorPath: win.editorBridge.dependencyPath("/masks/" + maskSlot.slotIndex + "/file", maskSlot.modelData.file || "")
                                        text: maskEditorPath.length > 0 ? "编辑修正板" : "新建"
                                        onClicked: {
                                            if (win.projectResources !== null)
                                                win.projectResources.openDependencyEditor("/masks/" + maskSlot.slotIndex + "/file")
                                            else
                                                win.moduleLauncher.openWindow("mask", maskEditorPath)
                                        }
                                    }
                                    ThemeControls.ThemeButton {
                                        objectName: "deleteMaskButton"
                                        text: "×"
                                        Accessible.name: "删除修正板 " + (maskSlot.slotIndex + 1)
                                        ToolTip.visible: hovered
                                        ToolTip.text: "删除修正板 " + (maskSlot.slotIndex + 1)
                                        onClicked: win.editorBridge.removeItem("/masks", maskSlot.slotIndex)
                                    }
                                }
                            }
                            ThemeControls.ThemeButton {
                                text: "＋ 添加修正板"
                                onClicked: editorBridge.appendItem("/masks", JSON.stringify({file:""}))
                            }
                        }
                    }

                    GroupBox {
                        title: "监测点 · 固定机架 XYZ"
                        Layout.fillWidth: true
                        padding: 6
                        topPadding: 25
                        bottomPadding: 6
                        ColumnLayout {
                            anchors.fill: parent
                            spacing: 4
                            Repeater {
                                model: win.doc.monitors || []
                                delegate: Frame {
                                    id: monitorSlot
                                    required property var modelData
                                    required property int index
                                    property int slotIndex: monitorSlot.index
                                    Layout.fillWidth: true
                                    padding: 5
                                    ColumnLayout {
                                        anchors.fill: parent
                                        spacing: 3
                                        RowLayout {
                                            ThemeControls.ThemeLabel { text: "监测点 " + (monitorSlot.slotIndex + 1); font.bold: true }
                                            Item { Layout.fillWidth: true }
                                            ThemeControls.ThemeButton { text: "删除"; onClicked: win.editorBridge.removeItem("/monitors", monitorSlot.slotIndex) }
                                        }
                                        RowLayout {
                                            ThemeControls.ThemeComboBox {
                                                model: ["晶控", "光控"]
                                                currentIndex: monitorSlot.modelData.kind === "optical" ? 1 : 0
                                                onActivated: win.editorBridge.setValue("/monitors/" + monitorSlot.slotIndex + "/kind", JSON.stringify(currentIndex ? "optical" : "crystal"))
                                            }
                                            ThemeControls.ThemeField {
                                                objectName: "monitorNameField" + monitorSlot.slotIndex
                                                property int slotIndex: monitorSlot.slotIndex
                                                Layout.fillWidth: true
                                                placeholderText: "名称"
                                                text: monitorSlot.modelData.name || ""
                                                onEditingFinished: win.editorBridge.setValue("/monitors/" + monitorSlot.slotIndex + "/name", JSON.stringify(text))
                                            }
                                        }
                                        RowLayout {
                                            Repeater {
                                                model: ["x_mm", "y_mm", "z_mm"]
                                                delegate: RowLayout {
                                                    id: monitorAxis
                                                    required property var modelData
                                                    Layout.fillWidth: true
                                                    ThemeControls.ThemeLabel { text: monitorAxis.modelData.charAt(0).toUpperCase() }
                                                    ThemeControls.ThemeField {
                                                        objectName: "monitorField" + monitorSlot.slotIndex + monitorAxis.modelData
                                                        accessibleLabel: "监测点 " + (monitorSlot.slotIndex + 1) + " " + monitorAxis.modelData
                                                        Layout.preferredWidth: 76
                                                        Layout.maximumWidth: 88
                                                        Layout.fillWidth: true
                                                        numericDisplayValue: (win.doc.monitors || [])[monitorSlot.slotIndex][monitorAxis.modelData]
                                                        onEditingFinished: if (numericValueEdited) win.putNumber("/monitors/" + monitorSlot.slotIndex + "/" + monitorAxis.modelData, text)
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            ThemeControls.ThemeButton {
                                objectName: "addMonitorButton"
                                text: "＋ 添加监测点"
                                enabled: (win.doc.monitors || []).length < 64
                                onClicked: editorBridge.appendItem("/monitors", JSON.stringify({kind:"crystal",name:"监测点",x_mm:0,y_mm:0,z_mm:0}))
                            }
                        }
                    }
                }
            }

            ColumnLayout {
                SplitView.fillWidth: true
                SplitView.minimumWidth: 390
                spacing: 8

                GroupBox {
                    title: "俯视图 · 源、监测点与修正板"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    Layout.minimumHeight: 240
                    padding: 6
                    topPadding: 25
                    bottomPadding: 6
                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 4
                        Item {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            LocalComponents.MachinePlanView {
                                id: topView
                                objectName: "machinePlanPreview"
                                anchors.fill: parent
                                visible: win.previewRequested
                                plan: win.previewPlan
                                ink: win.themeColors.colorText
                                axisColor: win.themeColors.colorBorderStrong
                                orbitColor: win.themeColors.colorAccent
                                sourceColors: [win.themeColors.colorAccent,
                                               win.themeColors.colorInfo,
                                               win.themeColors.colorSuccess,
                                               win.themeColors.colorWarning]
                                maskColors: [win.themeColors.colorInfo,
                                             win.themeColors.colorAccent]
                            }
                            ColumnLayout {
                                anchors.centerIn: parent
                                visible: !win.previewRequested
                                ThemeControls.ThemeLabel {
                                    text: "几何预览将在首帧后自动载入；左侧编辑器可先操作。"
                                    wrapMode: Text.WordWrap
                                    horizontalAlignment: Text.AlignHCenter
                                    Layout.maximumWidth: 260
                                }
                            }
                        }
                        ThemeControls.ThemeLabel {
                            Layout.fillWidth: true
                            text: "固定机架 XY / mm · 圆：蒸发源 · 方：监测点 · 实线：板外轮廓 · 虚线：孔洞"
                            wrapMode: Text.WordWrap
                            color: win.themeColors.colorTextMuted
                        }
                        Repeater {
                            model: win.maskPlanes
                            delegate: Label {
                                required property var modelData
                                visible: Boolean(modelData.error)
                                Layout.fillWidth: true
                                text: "板 " + modelData.slot + "：" + (modelData.error || "")
                                wrapMode: Text.WrapAnywhere
                            color: win.themeColors.colorError
                            }
                        }
                    }
                }
                GroupBox {
                    title: "侧视图 · 机架高度"
                    Layout.fillWidth: true
                    Layout.fillHeight: false
                    Layout.preferredHeight: 190
                    Layout.minimumHeight: 140
                    Layout.maximumHeight: 230
                    padding: 6
                    topPadding: 25
                    bottomPadding: 6
                    Canvas {
                        id: sideView
                        objectName: "machineSidePreview"
                        anchors.fill: parent
                        visible: win.previewRequested
                        onWidthChanged: { if (win.previewRequested) requestPaint() }
                        onHeightChanged: { if (win.previewRequested) requestPaint() }
                        onPaint: {
                            let ctx=getContext("2d")
                            ctx.clearRect(0,0,width,height)
                            let zTop=Number(win.doc.substrate_z_mm || 1)
                            let sources=win.doc.sources || [], monitors=win.doc.monitors || []
                            let planes=win.maskPlanes
                            let values=[0,zTop]
                            for (let s of sources) values.push(Number(s.z_mm || 0))
                            for (let m of monitors) values.push(Number(m.z_mm || 0))
                            for (let plane of planes) if(plane.z_mm!==undefined) values.push(Number(plane.z_mm))
                            let zMin=Math.min(...values), zMax=Math.max(...values)
                            let pad=Math.max(1,(zMax-zMin)*0.08)
                            zMin-=pad; zMax+=pad
                            let y=z=>height-28-(z-zMin)/(zMax-zMin)*(height-56)
                            ctx.font=win.themeColors.fontSizeSmall + "pt \"" + win.themeColors.fontFamily + "\"";ctx.lineWidth=2
                            let tilt=Number(win.doc.planet_tilt_deg || 0)
                            let rise=Math.sin(tilt*Math.PI/180)*Math.min(25,(height-56)*0.10)
                            ctx.strokeStyle=win.themeColors.colorInfo;ctx.beginPath();ctx.moveTo(30,y(zTop)-rise);ctx.lineTo(width-125,y(zTop)+rise);ctx.stroke()
                            ctx.fillStyle=win.themeColors.colorInfo;ctx.fillText("盘面 z="+zTop+" mm / "+tilt+"°",width-122,y(zTop)-8)
                            ctx.strokeStyle=win.themeColors.colorBorderStrong;ctx.beginPath();ctx.moveTo(30,y(0));ctx.lineTo(width-125,y(0));ctx.stroke()
                            ctx.fillStyle=win.themeColors.colorTextMuted;ctx.fillText("Z=0 mm",width-122,y(0)-5)
                            let lastSourceLabelY=-100
                            for(let i=0;i<sources.length;i++) {
                                let sz=Number(sources[i].z_mm || 0), sy=y(sz)
                                ctx.strokeStyle=win.themeColors.colorWarning;ctx.beginPath();ctx.moveTo(32,sy);ctx.lineTo(width-140,sy);ctx.stroke()
                                let ly=Math.max(sy-5,lastSourceLabelY+16)
                                ctx.fillStyle=win.themeColors.colorWarning;ctx.fillText("源 "+(i+1)+" z="+sz+" mm",width-136,ly)
                                lastSourceLabelY=ly
                            }
                            let lastLabelY=-100
                            for(let i=0;i<planes.length;i++) {
                                let plane=planes[i]
                                if(plane.z_mm===undefined)continue
                                let py=y(Number(plane.z_mm))
                                ctx.strokeStyle=i===0?win.themeColors.colorSuccess:win.themeColors.colorAccent
                                ctx.beginPath();ctx.moveTo(45,py);ctx.lineTo(width-115,py);ctx.stroke()
                                let labelY=Math.max(py-4,lastLabelY+17)
                                ctx.fillStyle=ctx.strokeStyle
                                if(labelY>py+5) {
                                    ctx.beginPath();ctx.moveTo(width-115,py);ctx.lineTo(width-109,labelY-4);ctx.stroke()
                                }
                                ctx.fillText("板 "+plane.slot+" z="+Number(plane.z_mm).toFixed(1)+" mm",width-108,labelY)
                                lastLabelY=labelY
                            }
                            for(let i=0;i<monitors.length;i++) {
                                let m=monitors[i], py=y(Number(m.z_mm))
                                ctx.fillStyle=win.themeColors.colorFocusRing;ctx.fillRect(37+i*15,py-4,8,8)
                            }
                        }
                    }
                    ThemeControls.ThemeLabel {
                        anchors.centerIn: parent
                        visible: !win.previewRequested
                        text: "首帧后自动显示侧视几何。"
                        color: win.themeColors.colorTextMuted
                    }
                }
            }
        }

    }
}
