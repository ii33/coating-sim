pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Window
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "theme_controls" as ThemeControls

Item {
    id: root

    required property var projectBridge
    required property var themeTokens
    readonly property var hostWindow: Window.window
    signal globalActionRequested(string moduleKey)

    // The desktop entry injects `launcher` on the root QQmlContext.
    // qmllint disable unqualified
    readonly property var launcherBridge: typeof launcher === "undefined" ? null : launcher
    // qmllint enable unqualified

    readonly property bool recentLoading: !!projectBridge
        && typeof projectBridge.recentBusy !== "undefined" && projectBridge.recentBusy
    property string validationError: ""
    property string selectedDescriptorFile: ""
    property var focusReturnItem: null
    property var actionEntry: null
    property string fileAction: ""
    property bool recentDetailsDismissed: false
    property string fullDetailsText: ""

    readonly property real contentSpacing: Math.min(
        themeTokens.spaceMd, Math.max(7, root.height * 0.024))

    // Recent-project details are visual guidance only. Keep the overlay from
    // participating in pointer hit testing so controls below it remain usable.
    TapHandler {
        acceptedButtons: Qt.AllButtons
        onPressedChanged: if (pressed) root.recentDetailsDismissed = true
    }

    Connections {
        target: root.hostWindow
        function onActiveChanged() {
            if (!root.hostWindow.active) root.recentDetailsDismissed = true
        }
    }
    Connections {
        target: root.projectBridge
        function onProjectsChanged() {
            root.recentDetailsDismissed = true
            fullDetailsDialog.close()
        }
    }

    ThemeControls.ThemeDialogBase {
        id: fullDetailsDialog
        objectName: "launcherFullProjectDetails"
        title: "工程详情"
        parent: Overlay.overlay
        modal: true
        width: Math.max(1, Math.min(600, parent.width - 24))
        height: Math.max(1, Math.min(520, parent.height - 24))
        x: Math.round((parent.width - width) / 2)
        y: Math.round((parent.height - height) / 2)
        onClosed: root.restoreDialogFocus()
        contentItem: ScrollView {
            id: fullDetailsScroll
            objectName: "launcherFullProjectDetailsScroll"
            Accessible.role: Accessible.Dialog
            Accessible.name: fullDetailsDialog.title
            contentWidth: availableWidth
            clip: true
            Flickable {
                id: fullDetailsFlickable
                contentWidth: width
                contentHeight: fullDetailsBodyText.implicitHeight
                boundsBehavior: Flickable.StopAtBounds
                Text {
                    id: fullDetailsBodyText
                    objectName: "launcherFullProjectDetailsText"
                    width: Math.max(1, fullDetailsFlickable.width - fullDetailsScroll.effectiveScrollBarWidth)
                    text: root.fullDetailsText
                    textFormat: Text.PlainText
                    font.family: root.themeTokens.fontFamily
                    font.pointSize: root.themeTokens.fontSizeBody
                    color: root.themeTokens.colorText
                    wrapMode: Text.WrapAnywhere
                }
            }
        }
        footer: DialogButtonBox {
            ThemeControls.ThemeButton {
                objectName: "launcherCloseFullProjectDetails"
                text: "关闭"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
            onRejected: fullDetailsDialog.close()
        }
    }

    // Scoped to the three recent-card actions; shared ThemeButton is unchanged.
    component RecentActionButton: ThemeControls.ThemeButton {
        id: actionControl
        flat: true
        hoverEnabled: true
        background: Rectangle {
            objectName: actionControl.objectName + "_background"
            radius: actionControl.themeTokens.radiusMd
            color: "transparent"
            border.width: 0
            Rectangle {
                objectName: actionControl.objectName + "_interaction"
                anchors.fill: parent
                radius: parent.radius
                color: actionControl.themeTokens.colorAccent
                opacity: !actionControl.enabled ? 0
                    : actionControl.down ? actionControl.themeTokens.opacityPressed
                    : actionControl.hovered ? actionControl.themeTokens.opacityHover : 0
                Behavior on opacity {
                    NumberAnimation { duration: 120; easing.type: Easing.OutCubic }
                }
            }
            Rectangle {
                objectName: actionControl.objectName + "_keyboardFocus"
                anchors.fill: parent
                radius: parent.radius
                color: "transparent"
                border.color: actionControl.themeTokens.colorFocusRing
                border.width: actionControl.themeTokens.focusRingWidth
                opacity: actionControl.visualFocus ? 1 : 0
                Behavior on opacity {
                    NumberAnimation { duration: 120; easing.type: Easing.OutCubic }
                }
            }
        }
    }

    function localPathFromUrl(url) {
        if (!root.launcherBridge || !url)
            return ""
        return root.launcherBridge.localFilePath(url)
    }

    function openProjectAt(path) {
        const descriptorPath = String(path || "").trim()
        if (!projectBridge || !descriptorPath || projectBridge.projectBusy)
            return false
        validationError = ""
        selectedDescriptorFile = descriptorPath
        return projectBridge.openProject(descriptorPath)
    }

    function openRecentProject(entry) {
        if (!entry)
            return false
        selectedDescriptorFile = String(entry.descriptorFile || "")
        if (entry.available !== true) {
            validationError = "工程文件当前不可用，可能已移动或删除：" + selectedDescriptorFile
            return false
        }
        return openProjectAt(selectedDescriptorFile)
    }

    function rowDetails(entry) {
        return "名称：" + String(entry.name || "未命名工程")
                + "\n摘要：" + String(entry.summary || "未填写摘要")
                + "\n备注：" + String(entry.notes || "未填写")
                + "\n工程文件：" + String(entry.descriptorFile || "")
                + "\n最近打开：" + String(entry.lastOpened || "时间未知")
                + (entry.available === true ? "" : "\n工程文件当前不可用")
    }

    function descriptorDirectory(entry) {
        const path = String(entry && entry.descriptorFile || "")
        return path.substring(0, Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\")))
    }

    function requestRemove(entry, button) {
        if (!projectBridge || !entry || projectBridge.projectBusy)
            return
        recentDetailsDismissed = true
        actionEntry = entry
        focusReturnItem = button
        deleteFilesChoice.checked = false
        removeDialog.open()
    }

    function confirmRemove() {
        if (!projectBridge || !actionEntry || projectBridge.projectBusy)
            return
        const descriptor = String(actionEntry.descriptorFile || "")
        if (deleteFilesChoice.checked) {
            if (!projectBridge.trashRecentProject(descriptor, String(actionEntry.projectId || "")))
                return
        } else {
            projectBridge.removeRecentProject(descriptor)
            if (selectedDescriptorFile === descriptor)
                selectedDescriptorFile = ""
        }
        validationError = ""
        removeDialog.close()
    }

    function requestFileAction(entry, action, button) {
        if (!projectBridge || !entry || projectBridge.projectBusy)
            return
        recentDetailsDismissed = true
        if (!actionEntry || String(actionEntry.descriptorFile || "") !== String(entry.descriptorFile || "")
                || fileAction !== action)
            fileDestinationField.text = ""
        actionEntry = entry
        fileAction = action
        focusReturnItem = button
        fileActionDialog.open()
    }

    function submitFileAction() {
        if (!projectBridge || !actionEntry || !fileDestinationField.text.trim())
            return
        const descriptor = String(actionEntry.descriptorFile || "")
        const accepted = fileAction === "copy"
            ? projectBridge.copyRecentProject(descriptor, String(actionEntry.projectId || ""), fileDestinationField.text.trim())
            : projectBridge.exportRecentProject(descriptor, String(actionEntry.projectId || ""), fileDestinationField.text.trim())
        if (accepted)
            fileActionDialog.close()
    }

    function createDestination(parentPath, projectName) {
        const parent = String(parentPath || "").trim()
        const name = String(projectName || "").trim()
        if (!parent || !name) {
            validationError = "请填写工程名称并选择存放位置。"
            return ""
        }
        if (name === "." || name === ".." || /[\\/]/.test(name)) {
            validationError = "工程名称只能是一个文件夹名称。"
            return ""
        }
        const separator = parent.indexOf("\\") >= 0 && parent.indexOf("/") < 0 ? "\\" : "/"
        const base = parent.replace(/[\\/]+$/, "")
        validationError = ""
        return (base || separator) + (base ? separator : "") + name
    }

    function submitCreate() {
        if (!projectBridge || projectBridge.projectBusy)
            return false
        const name = projectNameField.text.trim()
        const rootPath = createDestination(projectParentField.text, name)
        if (!rootPath)
            return false
        const accepted = typeof projectBridge.createProjectWithInformation === "function"
              ? projectBridge.createProjectWithInformation(name, rootPath,
                    projectSummaryField.text, projectNotesField.text)
              : projectBridge.createProject(name, rootPath)
        if (accepted)
            createDialog.close()
        return accepted
    }

    function restoreDialogFocus() {
        Qt.callLater(function() {
            if (root.focusReturnItem)
                root.focusReturnItem.forceActiveFocus(Qt.OtherFocusReason)
        })
    }

    function lastOpenedText(value) {
        const parsed = new Date(String(value || ""))
        if (isNaN(parsed.getTime()))
            return "时间未知"
        return Qt.formatDateTime(parsed, parsed.getFullYear() === new Date().getFullYear()
                                 ? "MM/dd hh:mm" : "yy/MM/dd")
    }

    function rowIsSelected(entry) {
        return !!entry && (entry.active === true
                           || String(entry.descriptorFile || "") === selectedDescriptorFile)
    }

    FolderDialog {
        id: createLocationDialog
        objectName: "launcherCreateLocationDialog"
        title: "选择项目存放位置"
        onAccepted: {
            projectParentField.text = root.localPathFromUrl(selectedFolder)
            root.restoreDialogFocus()
        }
        onRejected: root.restoreDialogFocus()
    }

    FileDialog {
        id: openProjectDialog
        objectName: "launcherOpenProjectFileDialog"
        title: "选择工程文件"
        fileMode: FileDialog.OpenFile
        nameFilters: ["工程文件 (*.coatproj *.COATPROJ)"]
        onAccepted: {
            const descriptorPath = root.localPathFromUrl(selectedFile)
            projectPathField.text = descriptorPath
            root.restoreDialogFocus()
            if (descriptorPath)
                root.openProjectAt(descriptorPath)
        }
        onRejected: root.restoreDialogFocus()
    }

    FolderDialog {
        id: copyParentDialog
        objectName: "launcherCopyParentDialog"
        title: "选择副本存放位置"
        onAccepted: {
            const parent = root.localPathFromUrl(selectedFolder)
            const name = String(root.actionEntry && root.actionEntry.name || "工程").replace(/[\\/]/g, "_") + " 副本"
            fileDestinationField.text = root.createDestination(parent, name)
            fileDestinationField.forceActiveFocus()
        }
    }

    FileDialog {
        id: exportDestinationDialog
        objectName: "launcherExportDestinationDialog"
        title: "选择新工程包文件"
        fileMode: FileDialog.SaveFile
        nameFilters: ["工程包 (*.cproject)"]
        defaultSuffix: "cproject"
        onAccepted: {
            fileDestinationField.text = root.localPathFromUrl(selectedFile)
            fileDestinationField.forceActiveFocus()
        }
    }

    ThemeControls.ThemeDialogBase {
        id: fileActionDialog
        objectName: "launcherProjectFileActionDialog"
        title: root.fileAction === "copy" ? "复制工程" : "导出工程"
        parent: Overlay.overlay
        modal: true
        width: Math.min(540, parent.width - 24)
        x: Math.round((parent.width - width) / 2)
        y: Math.max(0, Math.round((parent.height - height) / 2))
        onClosed: root.restoreDialogFocus()
        height: Math.min(implicitHeight, parent.height - 24)
        contentItem: ScrollView {
            id: fileActionDialogScroll
            contentWidth: availableWidth
            clip: true
            implicitHeight: fileActionContent.implicitHeight
            ColumnLayout {
                id: fileActionContent
                width: fileActionDialogScroll.availableWidth
                spacing: root.themeTokens.spaceSm
                ThemeControls.ThemeLabel {
                    textFormat: Text.PlainText
                    text: root.fileAction === "copy"
                        ? "复制已保存的工程输入，生成独立工程；不复制运行结果。请选择尚不存在的完整目标目录。"
                        : "导出已保存的工程输入为 .cproject 工程包；不包含运行结果。请选择新文件，不会覆盖已有文件。"
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeLabel {
                    textFormat: Text.PlainText
                    text: "工程：" + String(root.actionEntry && root.actionEntry.name || "")
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeField {
                    id: fileDestinationField
                    objectName: "launcherProjectFileDestination"
                    accessibleLabel: root.fileAction === "copy" ? "副本完整目标目录" : "新工程包文件"
                    placeholderText: root.fileAction === "copy" ? "新的完整工程目录" : "新的 .cproject 文件"
                    Layout.fillWidth: true
                    onAccepted: root.submitFileAction()
                }
                ThemeControls.ThemeButton {
                    objectName: "launcherBrowseProjectFileDestination"
                    text: "选择位置…"
                    onClicked: root.fileAction === "copy" ? copyParentDialog.open() : exportDestinationDialog.open()
                }
                ThemeControls.ThemeLabel {
                    textFormat: Text.PlainText
                    text: "如果工程正在使用，请先保存当前修改并关闭该工程后重试。"
                    muted: true
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
            }
        }
        footer: DialogButtonBox {
            ThemeControls.ThemeButton {
                objectName: "launcherConfirmProjectFileAction"
                text: root.fileAction === "copy" ? "创建副本" : "导出"
                primary: true
                enabled: fileDestinationField.text.trim().length > 0 && !!root.projectBridge && !root.projectBridge.projectBusy
                DialogButtonBox.buttonRole: DialogButtonBox.ActionRole
                onClicked: root.submitFileAction()
            }
            ThemeControls.ThemeButton {
                text: "取消"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
            onRejected: fileActionDialog.close()
        }
    }

    ThemeControls.ThemeDialogBase {
        id: removeDialog
        objectName: "launcherRemoveProjectDialog"
        title: "移除工程"
        parent: Overlay.overlay
        modal: true
        width: Math.min(540, parent.width - 24)
        x: Math.round((parent.width - width) / 2)
        y: Math.max(0, Math.round((parent.height - height) / 2))
        onOpened: removeConfirmButton.forceActiveFocus()
        onClosed: root.restoreDialogFocus()
        height: Math.min(implicitHeight, parent.height - 24)
        contentItem: ScrollView {
            id: removeDialogScroll
            contentWidth: availableWidth
            clip: true
            implicitHeight: removeContent.implicitHeight
            ColumnLayout {
                id: removeContent
                width: removeDialogScroll.availableWidth
                spacing: root.themeTokens.spaceSm
                ThemeControls.ThemeLabel {
                    textFormat: Text.PlainText
                    text: "是否从最近列表移除“" + String(root.actionEntry && root.actionEntry.name || "未命名工程") + "”？"
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                CheckBox {
                    id: deleteFilesChoice
                    objectName: "launcherDeleteProjectFilesChoice"
                    text: "同时将整个工程目录移入回收站"
                    checked: false
                    enabled: !!root.actionEntry && root.actionEntry.available === true
                        && root.actionEntry.informationAvailable === true
                    font.family: root.themeTokens.fontFamily
                    font.pointSize: root.themeTokens.fontSizeBody
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeLabel {
                    textFormat: Text.PlainText
                    objectName: "launcherRemoveProjectScope"
                    text: deleteFilesChoice.checked
                        ? "将移入回收站的完整目录：\n" + root.descriptorDirectory(root.actionEntry)
                          + "\n包括此工程文件、工程资源、导入导出文件和运行结果。仅明确归属的标准工程目录可回收；含其他工程、链接或不明内容时会保留原文件和记录。回收失败不会永久删除。"
                        : "仅移出最近列表，保留所有工程文件。"
                    wrapMode: Text.Wrap
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeLabel {
                    textFormat: Text.PlainText
                    objectName: "launcherRemoveProjectDescriptor"
                    text: "工程文件：" + String(root.actionEntry && root.actionEntry.descriptorFile || "")
                    wrapMode: Text.WrapAnywhere
                    muted: true
                    Layout.fillWidth: true
                }
            }
        }
        footer: DialogButtonBox {
            ThemeControls.ThemeButton {
                id: removeConfirmButton
                objectName: "launcherConfirmRemoveProject"
                text: deleteFilesChoice.checked ? "移入回收站并移除" : "仅移出最近列表"
                primary: !deleteFilesChoice.checked
                enabled: !!root.projectBridge && !root.projectBridge.projectBusy
                DialogButtonBox.buttonRole: DialogButtonBox.ActionRole
                onClicked: root.confirmRemove()
            }
            ThemeControls.ThemeButton {
                objectName: "launcherCancelRemoveProject"
                text: "取消"
                DialogButtonBox.buttonRole: DialogButtonBox.RejectRole
            }
            onRejected: removeDialog.close()
        }
    }

    ThemeControls.ThemeDialogBase {
        id: createDialog
        objectName: "launcherCreateProjectDialog"
        title: "新建工程"
        modal: true
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(560, parent ? parent.width - 36 : 560)
        height: Math.min(580, parent ? parent.height - 36 : 580)
        onClosed: root.restoreDialogFocus()

        ColumnLayout {
            anchors.fill: parent
            spacing: root.themeTokens.spaceSm
            ScrollView {
                id: createFieldsScroll
                Layout.fillWidth: true
                Layout.fillHeight: true
                contentWidth: availableWidth
                clip: true
                ColumnLayout {
                    width: createFieldsScroll.availableWidth
                    spacing: root.themeTokens.spaceSm
                    ThemeControls.ThemeLabel { text: "工程名称"; Layout.fillWidth: true }
                    ThemeControls.ThemeField {
                        id: projectNameField
                        objectName: "launcherProjectNameField"
                        accessibleLabel: "工程名称"
                        placeholderText: "例如：镀膜方案 A"
                        maximumLength: 100
                        Layout.fillWidth: true
                    }
                    ThemeControls.ThemeLabel { text: "摘要"; Layout.fillWidth: true }
                    ThemeControls.ThemeField {
                        id: projectSummaryField
                        objectName: "launcherProjectSummaryField"
                        accessibleLabel: "工程摘要"
                        placeholderText: "简短说明工程目标（可选）"
                        maximumLength: 20000
                        Layout.fillWidth: true
                    }
                    ThemeControls.ThemeLabel { text: "备注"; Layout.fillWidth: true }
                    ScrollView {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 110
                        clip: true
                        TextArea {
                            id: projectNotesField
                            objectName: "launcherProjectNotesField"
                            Accessible.name: "工程备注"
                            placeholderText: "补充背景、约束或待办（可选，最多 20000 字）"
                            wrapMode: TextEdit.Wrap
                            color: root.themeTokens.colorText
                            placeholderTextColor: root.themeTokens.colorTextMuted
                            selectionColor: root.themeTokens.colorSelection
                            selectedTextColor: root.themeTokens.colorText
                            font.family: root.themeTokens.fontFamily
                            font.pointSize: root.themeTokens.fontSizeBody
                            background: Rectangle {
                                color: root.themeTokens.colorSurface
                                radius: root.themeTokens.radiusSm
                                border.color: projectNotesField.activeFocus
                                    ? root.themeTokens.colorFocusRing : root.themeTokens.colorBorder
                            }
                        }
                    }
                    ThemeControls.ThemeLabel { text: "存放位置"; Layout.fillWidth: true }
                    RowLayout {
                        Layout.fillWidth: true
                        ThemeControls.ThemeField {
                            id: projectParentField
                            objectName: "launcherProjectParentPathField"
                            accessibleLabel: "项目存放位置"
                            placeholderText: "选择父文件夹"
                            Layout.fillWidth: true
                        }
                        ThemeControls.ThemeButton {
                            objectName: "launcherChooseProjectLocationButton"
                            text: "浏览…"
                            enabled: !root.projectBridge || !root.projectBridge.projectBusy
                            onClicked: {
                                root.focusReturnItem = this
                                createLocationDialog.open()
                            }
                        }
                    }
                    ThemeControls.ThemeLabel {
                        textFormat: Text.PlainText
                        visible: root.validationError.length > 0
                        text: root.validationError
                        color: root.themeTokens.colorError
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ThemeControls.ThemeButton {
                    objectName: "launcherCancelCreateProjectButton"
                    text: "取消"
                    enabled: !root.projectBridge || !root.projectBridge.projectBusy
                    onClicked: createDialog.close()
                }
                ThemeControls.ThemeButton {
                    objectName: "launcherConfirmCreateProjectButton"
                    text: "创建"
                    primary: true
                    enabled: !!root.projectBridge && !root.projectBridge.projectBusy
                    onClicked: root.submitCreate()
                }
            }
        }
    }

    ScrollView {
        id: managerScroll
        objectName: "launcherProjectManagerScroll"
        anchors.fill: parent
        anchors.topMargin: Math.max(root.themeTokens.spaceSm,
                                    Math.min(root.themeTokens.spaceMd, root.height * 0.03))
        anchors.bottomMargin: globalActions.implicitHeight + root.themeTokens.spaceSm * 2
        clip: true
        contentWidth: availableWidth
        ScrollBar.vertical.policy: ScrollBar.AsNeeded

        ColumnLayout {
            id: managerColumn
            width: managerScroll.availableWidth
            height: implicitHeight
            spacing: root.contentSpacing

            GridLayout {
                id: workspaceHeader
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                Layout.maximumWidth: managerScroll.availableWidth
                columns: managerScroll.availableWidth
                         >= refreshAction.implicitWidth + 100 + columnSpacing
                         ? 2 : 1
                columnSpacing: root.themeTokens.spaceMd
                rowSpacing: root.themeTokens.spaceXs
                ColumnLayout {
                    Layout.minimumWidth: 0
                    Layout.fillWidth: true
                    spacing: root.themeTokens.spaceXs
                    ThemeControls.ThemeLabel {
                        textFormat: Text.PlainText
                        id: projectTitle
                        objectName: "launcherProjectManagerTitle"
                        text: "最近工程"
                        heading: true
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                    }
                    ThemeControls.ThemeLabel {
                        textFormat: Text.PlainText
                        objectName: "launcherProjectManagerDescription"
                        text: "打开最近使用的工程，或新建、浏览打开工程文件。"
                        muted: true
                        wrapMode: Text.Wrap
                        visible: root.width >= 360
                        Layout.fillWidth: true
                        Layout.minimumWidth: 0
                    }
                }
                ThemeControls.ThemeButton {
                    id: refreshAction
                    objectName: "launcherRefreshProjectsButton"
                    text: "刷新"
                    enabled: !!root.projectBridge && !root.projectBridge.projectBusy
                    Layout.minimumWidth: 0
                    Layout.maximumWidth: implicitWidth
                    Layout.alignment: Qt.AlignRight
                    onClicked: root.projectBridge.refreshProjects()
                }
            }

            Rectangle {
                id: projectCard
                objectName: "launcherProjectCard"
                Layout.fillWidth: true
                readonly property real requiredContentHeight: Math.max(
                    132, projectCardContent.implicitHeight + 2 * root.contentSpacing)
                Layout.minimumHeight: requiredContentHeight
                Layout.preferredHeight: Math.max(
                    requiredContentHeight, Math.min(310, root.height - 310))
                radius: root.themeTokens.radiusMd
                color: root.themeTokens.colorSurface
                border.color: root.themeTokens.colorBorder
                border.width: root.themeTokens.borderWidth

                ColumnLayout {
                    id: projectCardContent
                    anchors.fill: parent
                    anchors.margins: root.contentSpacing
                    spacing: root.themeTokens.spaceSm

                    ThemeControls.ThemeLabel {
                        textFormat: Text.PlainText
                        visible: !!root.projectBridge && (root.projectBridge.projectBusy || root.recentLoading)
                        objectName: "launcherProjectBusyStatus"
                        text: root.projectBridge && root.projectBridge.projectBusy
                              ? "正在处理项目请求…" : "正在读取最近工程…"
                        muted: true
                        Layout.fillWidth: true
                    }

                    ThemeControls.ThemeLabel {
                        textFormat: Text.PlainText
                        visible: !root.projectBridge
                                 || root.projectBridge.projectEntries.length === 0
                                    && !root.projectBridge.projectBusy && !root.recentLoading
                        objectName: "launcherProjectEmptyStatus"
                        text: root.projectBridge
                              ? "还没有最近工程。新建工程或打开工程文件后，会显示在这里。"
                              : "项目管理尚未连接。"
                        muted: true
                        wrapMode: Text.Wrap
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        verticalAlignment: Text.AlignVCenter
                    }

                    ListView {
                        id: projectList
                        objectName: "launcherProjectList"
                        visible: !!root.projectBridge && root.projectBridge.projectEntries.length > 0
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        spacing: root.themeTokens.spaceSm
                        onMovementStarted: root.recentDetailsDismissed = true
                        onContentYChanged: root.recentDetailsDismissed = true
                        ScrollBar.vertical: ScrollBar {
                            id: recentScroll
                            policy: projectList.contentHeight > projectList.height
                                    ? ScrollBar.AlwaysOn : ScrollBar.AsNeeded
                        }
                        model: root.projectBridge ? root.projectBridge.projectEntries : []
                        currentIndex: {
                            if (!root.projectBridge)
                                return -1
                            const entries = root.projectBridge.projectEntries
                            for (let i = 0; i < entries.length; ++i) {
                                if (root.rowIsSelected(entries[i]))
                                    return i
                            }
                            return -1
                        }
                        delegate: Rectangle {
                            id: projectRow
                            required property int index
                            required property var modelData
                            objectName: "launcherProjectEntry_" + index
                            width: projectList.width - (recentScroll.visible ? recentScroll.width + root.themeTokens.spaceXs : 0)
                            height: rowContent.implicitHeight + 2 * root.themeTokens.spaceXs
                            radius: root.themeTokens.radiusMd
                            color: root.rowIsSelected(projectRow.modelData)
                                   ? root.themeTokens.colorSelection
                                   : root.themeTokens.colorSurfaceRaised
                            border.color: root.rowIsSelected(projectRow.modelData)
                                          ? root.themeTokens.colorAccent
                                          : root.themeTokens.colorBorder
                            border.width: root.rowIsSelected(projectRow.modelData)
                                          ? Math.max(root.themeTokens.borderWidth,
                                                     root.themeTokens.focusRingWidth)
                                          : root.themeTokens.borderWidth

                            HoverHandler {
                                id: rowHover
                                onHoveredChanged: {
                                    if (hovered) root.recentDetailsDismissed = false
                                    else root.recentDetailsDismissed = true
                                }
                            }
                            ToolTip {
                                id: rowDetailsTip
                                objectName: "launcherRecentProjectDetails_" + projectRow.index
                                parent: Overlay.overlay
                                enabled: false
                                visible: rowHover.hovered && !root.recentDetailsDismissed
                                    && sidePlacement && !fullDetailsDialog.visible
                                delay: 500
                                text: root.rowDetails(projectRow.modelData)
                                font.family: root.themeTokens.fontFamily
                                font.pointSize: root.themeTokens.fontSizeBody
                                readonly property real windowLeft: root.hostWindow ? root.hostWindow.x : 0
                                readonly property real windowTop: root.hostWindow ? root.hostWindow.y : 0
                                readonly property real boundsLeft: Math.max(12, root.Screen.virtualX - windowLeft + 12)
                                readonly property real boundsTop: Math.max(12, root.Screen.virtualY - windowTop + 12)
                                readonly property real boundsRight: Math.min(parent.width - 12,
                                    root.Screen.virtualX + root.Screen.desktopAvailableWidth - windowLeft - 12)
                                readonly property real boundsBottom: Math.min(parent.height - 12,
                                    root.Screen.virtualY + root.Screen.desktopAvailableHeight - windowTop - 12)
                                // Protect the entire manager, including every recent row
                                // and the open/create controls, rather than just this row.
                                // Include the content parent's position explicitly:
                                // mapToItem alone does not track ancestor movement.
                                readonly property point managerOrigin: root.parent.parent.mapToItem(parent,
                                    root.parent.x + root.x, root.parent.y + root.y)
                                readonly property real rowLeft: managerOrigin.x
                                readonly property real rowRight: rowLeft + root.width
                                readonly property real sideGap: 24
                                readonly property real leftSpace: Math.max(0, rowLeft - boundsLeft - sideGap)
                                readonly property real rightSpace: Math.max(0, boundsRight - rowRight - sideGap)
                                readonly property bool showOnRight: rightSpace >= leftSpace
                                readonly property real sideSpace: showOnRight ? rightSpace : leftSpace
                                readonly property bool sidePlacement: sideSpace >= 220
                                readonly property real rowTop: rowHover.point.scenePosition.y
                                width: Math.max(1, Math.min(360, sideSpace))
                                height: Math.max(1, Math.min(360, boundsBottom - boundsTop,
                                    detailsText.implicitHeight + topPadding + bottomPadding))
                                x: showOnRight ? rowRight + sideGap : rowLeft - width - sideGap
                                y: Math.max(boundsTop, Math.min(boundsBottom - height, rowTop))
                                contentItem: ScrollView {
                                    id: detailsScroll
                                    objectName: "launcherRecentProjectDetailsScroll_" + projectRow.index
                                    contentWidth: availableWidth
                                    clip: true
                                    Text {
                                        id: detailsText
                                        objectName: "launcherRecentProjectDetailsText_" + projectRow.index
                                        width: Math.max(1, detailsScroll.availableWidth - detailsScroll.effectiveScrollBarWidth)
                                        text: rowDetailsTip.text
                                        textFormat: Text.PlainText
                                        font: rowDetailsTip.font
                                        color: root.themeTokens.colorText
                                        wrapMode: Text.WrapAnywhere
                                    }
                                }
                            }

                            ColumnLayout {
                                id: rowContent
                                anchors.fill: parent
                                anchors.margins: root.themeTokens.spaceXs
                                spacing: 0

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: Math.max(1, root.themeTokens.spaceXs / 2)
                                    ThemeControls.ThemeButton {
                                        id: projectOpenButton
                                        objectName: "launcherOpenRecentProject_" + projectRow.index
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 0
                                        flat: true
                                        leftPadding: root.themeTokens.spaceXs
                                        rightPadding: root.themeTokens.spaceXs
                                        topPadding: root.themeTokens.spaceXs
                                        bottomPadding: root.themeTokens.spaceXs
                                        implicitHeight: Math.max(root.themeTokens.controlHeightSm,
                                            contentItem.implicitHeight + topPadding + bottomPadding)
                                        text: String(projectRow.modelData.name || "未命名工程").replace(/[\r\n]+/g, " ")
                                        enabled: !!root.projectBridge && !root.projectBridge.projectBusy
                                        onClicked: root.openRecentProject(projectRow.modelData)
                                        contentItem: ThemeControls.ThemeLabel {
                                            textFormat: Text.PlainText
                                            objectName: "launcherRecentProjectName_" + projectRow.index
                                            text: projectOpenButton.text
                                            font.pointSize: root.themeTokens.fontSizeBody
                                            font.weight: root.themeTokens.fontWeightEmphasis
                                            wrapMode: Text.NoWrap
                                            maximumLineCount: 1
                                            elide: Text.ElideRight
                                            verticalAlignment: Text.AlignVCenter
                                            horizontalAlignment: Text.AlignLeft
                                        }
                                    }
                                    RecentActionButton {
                                        objectName: "launcherCopyRecentProject_" + projectRow.index
                                        text: "复制"
                                        font.pointSize: root.themeTokens.fontSizeSmall
                                        leftPadding: Math.max(1, root.themeTokens.spaceXs / 2)
                                        rightPadding: Math.max(1, root.themeTokens.spaceXs / 2)
                                        topPadding: root.themeTokens.spaceXs
                                        bottomPadding: root.themeTokens.spaceXs
                                        implicitHeight: Math.max(root.themeTokens.controlHeightSm,
                                            contentItem.implicitHeight + topPadding + bottomPadding)
                                        enabled: !!root.projectBridge && !root.projectBridge.projectBusy
                                            && projectRow.modelData.available === true
                                            && projectRow.modelData.informationAvailable === true
                                        onClicked: root.requestFileAction(projectRow.modelData, "copy", this)
                                    }
                                    RecentActionButton {
                                        objectName: "launcherExportRecentProject_" + projectRow.index
                                        text: "导出"
                                        font.pointSize: root.themeTokens.fontSizeSmall
                                        leftPadding: Math.max(1, root.themeTokens.spaceXs / 2)
                                        rightPadding: Math.max(1, root.themeTokens.spaceXs / 2)
                                        topPadding: root.themeTokens.spaceXs
                                        bottomPadding: root.themeTokens.spaceXs
                                        implicitHeight: Math.max(root.themeTokens.controlHeightSm,
                                            contentItem.implicitHeight + topPadding + bottomPadding)
                                        enabled: !!root.projectBridge && !root.projectBridge.projectBusy
                                            && projectRow.modelData.available === true
                                            && projectRow.modelData.informationAvailable === true
                                        onClicked: root.requestFileAction(projectRow.modelData, "export", this)
                                    }
                                    RecentActionButton {
                                        objectName: "launcherRemoveRecentProject_" + projectRow.index
                                        text: "移除"
                                        font.pointSize: root.themeTokens.fontSizeSmall
                                        leftPadding: Math.max(1, root.themeTokens.spaceXs / 2)
                                        rightPadding: Math.max(1, root.themeTokens.spaceXs / 2)
                                        topPadding: root.themeTokens.spaceXs
                                        bottomPadding: root.themeTokens.spaceXs
                                        implicitHeight: Math.max(root.themeTokens.controlHeightSm,
                                            contentItem.implicitHeight + topPadding + bottomPadding)
                                        enabled: !!root.projectBridge && !root.projectBridge.projectBusy
                                        onClicked: root.requestRemove(projectRow.modelData, this)
                                    }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: root.themeTokens.spaceXs
                                    ThemeControls.ThemeLabel {
                                        textFormat: Text.PlainText
                                        objectName: "launcherRecentProjectSummary_" + projectRow.index
                                        text: String(projectRow.modelData.summary || "未填写摘要").replace(/[\r\n]+/g, " ")
                                        muted: true
                                        wrapMode: Text.NoWrap
                                        elide: Text.ElideRight
                                        maximumLineCount: 1
                                        Layout.fillWidth: true
                                        Layout.minimumWidth: 0
                                        Layout.leftMargin: root.themeTokens.spaceXs
                                        Layout.rightMargin: root.themeTokens.spaceXs
                                        horizontalAlignment: Text.AlignLeft
                                    }
                                    RecentActionButton {
                                        objectName: "launcherShowRecentProjectDetails_" + projectRow.index
                                        text: "详情"
                                        font.pointSize: root.themeTokens.fontSizeSmall
                                        topPadding: 0
                                        bottomPadding: 0
                                        implicitHeight: contentItem.implicitHeight
                                        onClicked: {
                                            root.recentDetailsDismissed = true
                                            root.fullDetailsText = root.rowDetails(projectRow.modelData)
                                            root.focusReturnItem = this
                                            fullDetailsFlickable.contentY = 0
                                            fullDetailsDialog.open()
                                        }
                                    }
                                }
                            }
                        }
                    }
                    ThemeControls.ThemeLabel {
                        textFormat: Text.PlainText
                        objectName: "launcherRecentScrollHint"
                        visible: root.width >= 360
                                 && projectList.contentHeight > projectList.height
                                 && projectList.count > 1
                        text: projectList.count + " 个最近工程 · 滚动查看"
                        muted: true
                        font.pointSize: root.themeTokens.fontSizeSmall
                        Layout.fillWidth: true
                    }
                }
            }

            ThemeControls.ThemeLabel {
                textFormat: Text.PlainText
                objectName: "launcherProjectFileMessage"
                visible: !!root.projectBridge
                    && typeof root.projectBridge.projectFileMessage !== "undefined"
                    && root.projectBridge.projectFileMessage.length > 0
                text: visible ? root.projectBridge.projectFileMessage : ""
                wrapMode: Text.WrapAnywhere
                Layout.fillWidth: true
            }

            ThemeControls.ThemeLabel {
                textFormat: Text.PlainText
                objectName: "launcherProjectError"
                visible: !!root.projectBridge && root.projectBridge.projectError.length > 0
                text: root.projectBridge ? root.projectBridge.projectError : ""
                color: root.themeTokens.colorError
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }

            ThemeControls.ThemeLabel {
                textFormat: Text.PlainText
                objectName: "launcherProjectValidationError"
                visible: root.validationError.length > 0
                text: root.validationError
                color: root.themeTokens.colorError
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }

            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeField {
                    id: projectPathField
                    objectName: "launcherOpenProjectPathField"
                    accessibleLabel: "已有工程文件"
                    accessibleDescription: "输入或浏览选择 .coatproj 工程文件。"
                    placeholderText: "工程文件路径"
                    Layout.fillWidth: true
                }
                ThemeControls.ThemeButton {
                    objectName: "launcherBrowseOpenProjectButton"
                    text: "浏览…"
                    enabled: !!root.projectBridge && !root.projectBridge.projectBusy
                    onClicked: {
                        root.focusReturnItem = this
                        openProjectDialog.open()
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeButton {
                    objectName: "launcherNewProjectButton"
                    text: "新建工程"
                    primary: true
                    enabled: !!root.projectBridge && !root.projectBridge.projectBusy
                    Layout.fillWidth: true
                    onClicked: {
                        root.validationError = ""
                        root.focusReturnItem = this
                        createDialog.open()
                        projectNameField.forceActiveFocus(Qt.TabFocusReason)
                    }
                }
                ThemeControls.ThemeButton {
                    objectName: "launcherOpenProjectButton"
                    text: "打开工程文件"
                    enabled: !!root.projectBridge && !root.projectBridge.projectBusy
                    Layout.fillWidth: true
                    onClicked: {
                        if (projectPathField.text.trim().length > 0) {
                            root.openProjectAt(projectPathField.text)
                        } else {
                            root.focusReturnItem = this
                            openProjectDialog.open()
                        }
                    }
                }
            }

            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: root.themeTokens.spaceSm
            }
        }
    }

    // Keep global actions above the window's separate attribution footer.
    // The project list and picker workflow remain in the scrollable area above.
    TextMetrics { id: fullGlobalResourcesText; text: "全局资产库"; font: resourcesAction.font }
    TextMetrics { id: fullGlobalSettingsText; text: "设置"; font: settingsAction.font }
    TextMetrics { id: fullGlobalAboutText; text: "关于帮助"; font: aboutAction.font }
    GridLayout {
        id: globalActions
        readonly property bool compactActions: width < fullGlobalResourcesText.width
            + fullGlobalSettingsText.width + fullGlobalAboutText.width
            + 6 * root.themeTokens.spaceMd + 2 * columnSpacing
        objectName: "launcherGlobalActions"
        anchors.left: root.left
        anchors.right: root.right
        anchors.bottom: root.bottom
        anchors.leftMargin: root.themeTokens.spaceXs
        anchors.rightMargin: root.themeTokens.spaceXs
        anchors.bottomMargin: root.themeTokens.spaceSm
        columns: globalActions.width
                 >= settingsAction.implicitWidth + aboutAction.implicitWidth + resourcesAction.implicitWidth + 2 * columnSpacing
                 ? 3 : globalActions.width >= resourcesAction.implicitWidth + settingsAction.implicitWidth + columnSpacing
                   ? 2 : 1
        columnSpacing: root.themeTokens.spaceSm
        rowSpacing: root.themeTokens.spaceSm

        ThemeControls.ThemeButton {
            id: resourcesAction
            objectName: "launcherGlobalResourcesButton"
            text: globalActions.compactActions ? "资产库" : "全局资产库"
            Accessible.name: "全局资产库"
            leftPadding: globalActions.compactActions ? root.themeTokens.spaceXs : root.themeTokens.spaceMd
            rightPadding: leftPadding
            ToolTip.visible: hovered
            ToolTip.text: "全局资产库"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            onClicked: root.globalActionRequested("global_resources")
        }
        ThemeControls.ThemeButton {
            id: settingsAction
            objectName: "launcherSettingsButton"
            text: "设置"
            leftPadding: globalActions.compactActions ? root.themeTokens.spaceXs : root.themeTokens.spaceMd
            rightPadding: leftPadding
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            onClicked: root.globalActionRequested("settings")
        }
        ThemeControls.ThemeButton {
            id: aboutAction
            objectName: "launcherAboutHelpButton"
            text: globalActions.compactActions ? "帮助" : "关于帮助"
            Accessible.name: "关于与帮助"
            leftPadding: globalActions.compactActions ? root.themeTokens.spaceXs : root.themeTokens.spaceMd
            rightPadding: leftPadding
            ToolTip.visible: hovered
            ToolTip.text: "关于与帮助"
            Layout.columnSpan: globalActions.columns === 2 ? 2 : 1
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            onClicked: root.globalActionRequested("about")
        }
    }

}
