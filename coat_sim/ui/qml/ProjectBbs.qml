pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "theme_controls" as ThemeControls

Item {
    id: root
    objectName: "projectBbsPage"
    required property var projectBridge
    required property var themeTokens
    readonly property var service: projectBridge && projectBridge.projectBbs ? projectBridge.projectBbs : null
    property string projectId: ""
    property string generation: ""
    property string requestToken: ""
    property string pendingOperation: ""
    property string errorMessage: ""
    property string statusMessage: ""
    property var posts: []
    property int total: 0
    property int offset: 0
    property string parentId: ""
    property var currentPost: ({})
    property var revisions: []
    property int historyOffset: 0
    property int historyTotal: 0
    property bool historyVisible: false
    property bool readingFocus: false
    property bool richPreview: true
    property bool editorPreview: false
    property var selectedAttachment: ({})
    property var attachmentResult: ({})
    property string attachmentError: ""
    property var exportAttachment: ({})
    property var attachmentImportTarget: ({})
    property var attachmentExportTarget: ({})
    readonly property var shownAttachments: mode ? editAttachments : (currentPost.attachments || [])
    property string mode: ""
    property string editPostId: ""
    property int editRevision: 0
    property string editType: "journal"
    property string editFormat: "markdown"
    property string originalFormat: "markdown"
    property var editAttachments: []
    property var originalAttachments: []
    property string originalTitle: ""
    property string originalBody: ""
    property int editSerial: 0
    readonly property var snapshotState: ({
        selected_post_id: String(currentPost.post_id || ""),
        selected_revision: Number(currentPost.revision || 0),
        selected_title: String(currentPost.title || "").slice(0, 200),
        post_type_filter: filter.currentIndex > 0 ? typeKeys[filter.currentIndex - 1] : "",
        query: searchField.text.slice(0, 200), parent_id: parentId, offset: offset, total: total,
        editor_mode: mode, editor_post_id: editPostId, editor_revision: editRevision,
        unsaved: dirty, editor_title: titleField.text.slice(0, 200),
        editor_body_length: bodyField.text.length, edit_serial: editSerial,
        body_format: editFormat, attachment_count: shownAttachments.length,
        loading: busy, history_visible: historyVisible
    })
    property bool restoring: false
    readonly property bool busy: !!service && service.busy
    readonly property bool dirty: mode.length > 0 && (titleField.text !== originalTitle || bodyField.text !== originalBody
        || editFormat !== originalFormat || JSON.stringify(editAttachments) !== JSON.stringify(originalAttachments))
    readonly property var typeKeys: ["journal", "progress", "research_report", "work_report", "comment"]
    readonly property var typeLabels: ["工程日志", "进度", "研究报告", "工作报告", "评论"]

    function installPreviewProvider() {
        if (service && service.attachmentPreview) service.attachmentPreview.install(root)
    }
    Component.onCompleted: installPreviewProvider()
    onServiceChanged: installPreviewProvider()
    function previewAttachment(attachment, page) {
        selectedAttachment = attachment
        attachmentResult = ({})
        attachmentError = ""
        attachmentDialog.open()
        return request("attachment_preview", {attachment: attachment, page: page})
    }
    function openAttachmentImport() {
        attachmentImportTarget = {projectId: projectId, generation: generation, mode: mode,
            postId: editPostId, revision: editRevision}
        attachmentImportDialog.open()
    }
    function acceptAttachmentImport(url) {
        const target = attachmentImportTarget
        attachmentImportTarget = ({})
        if (target.projectId !== projectId || target.generation !== generation
                || target.mode !== mode || target.postId !== editPostId || target.revision !== editRevision) {
            errorMessage = "原附件选择目标已变化，请重新选择。"
            return false
        }
        return importAttachment(url)
    }
    function openAttachmentExport(attachment) {
        exportAttachment = attachment
        attachmentExportTarget = {projectId: projectId, generation: generation, attachment: attachment}
        attachmentExportDialog.open()
    }
    function acceptAttachmentExport(url) {
        const target = attachmentExportTarget
        attachmentExportTarget = ({})
        if (target.projectId !== projectId || target.generation !== generation) {
            errorMessage = "原附件导出目标已变化，请重新选择。"
            return false
        }
        return request("attachment_export", {attachment: target.attachment, destination_url: String(url)})
    }
    function importAttachment(url) {
        if (!mode || editAttachments.length >= 8) return false
        return request("attachment_import", {source_url: String(url)})
    }
    function removeAttachment(identity) {
        editAttachments = editAttachments.filter(function(item) { return item.asset_id !== identity })
        retain()
    }
    function actorName(value) { return value === "assistant" ? "助手" : "用户" }
    function typeName(value) { const i = typeKeys.indexOf(value); return i < 0 ? "工程记录" : typeLabels[i] }
    function retain() {
        if (restoring || !service || !projectId || !generation) return
        editSerial += 1
        service.stageDraft(projectId, generation, dirty ? {
            mode: mode, post_id: editPostId, revision: editRevision, post_type: editType, body_format: editFormat,
            attachments: editAttachments, originalFormat: originalFormat, originalAttachments: originalAttachments,
            title: titleField.text, body: bodyField.text,
            originalTitle: originalTitle, originalBody: originalBody
        } : {})
    }
    function openFor(identity, sessionGeneration) {
        retain()
        if (projectId === identity && generation === sessionGeneration) return
        dismissDialogs()
        restoring = true
        projectId = identity
        generation = sessionGeneration
        requestToken = ""
        pendingOperation = ""
        posts = []
        total = 0
        offset = 0
        parentId = ""
        currentPost = ({})
        readingFocus = false
        selectedAttachment = ({})
        attachmentResult = ({})
        attachmentError = ""
        revisions = []
        historyVisible = false
        errorMessage = ""
        statusMessage = ""
        const saved = service ? service.draft(projectId, generation) : ({})
        mode = String(saved.mode || "")
        editPostId = String(saved.post_id || "")
        editRevision = Number(saved.revision || 0)
        editType = String(saved.post_type || "journal")
        editFormat = String(saved.body_format || "markdown")
        originalFormat = String(saved.originalFormat || editFormat)
        editAttachments = saved.attachments || []
        originalAttachments = saved.originalAttachments || []
        originalTitle = String(saved.originalTitle || "")
        originalBody = String(saved.originalBody || "")
        titleField.text = String(saved.title || "")
        bodyField.text = String(saved.body || "")
        typeField.currentIndex = Math.max(0, typeKeys.indexOf(editType))
        restoring = false
    }
    function request(operation, payload) {
        if (!service || busy || !generation) return false
        errorMessage = ""
        statusMessage = ""
        const token = service.request(projectId, generation, operation, payload)
        if (!token) { errorMessage = service.error; return false }
        requestToken = token
        pendingOperation = operation
        return true
    }
    function refresh(pageOffset) {
        return request("list", {post_type: filter.currentIndex > 0 ? typeKeys[filter.currentIndex - 1] : "",
            parent_id: parentId, include_archived: archivedFilter.checked,
            query: searchField.text, offset: Math.max(0, pageOffset)})
    }
    function readPost(identity) { return request("read", {post_id: identity}) }
    function beginEdit(kind) {
        if (dirty) { errorMessage = "请先发布或放弃当前未发布内容。"; return false }
        if (kind !== "create" && !currentPost.post_id) return false
        restoring = true
        mode = kind
        editorPreview = false
        editPostId = kind === "create" ? "" : String(currentPost.post_id)
        editRevision = kind === "create" ? 0 : Number(currentPost.revision)
        editType = kind === "create" ? "journal" : String(currentPost.post_type)
        editFormat = kind === "update" ? String(currentPost.body_format || "plain") : "markdown"
        originalFormat = editFormat
        editAttachments = kind === "update" ? (currentPost.attachments || []) : []
        originalAttachments = editAttachments.slice()
        originalTitle = kind === "update" ? String(currentPost.title) : ""
        originalBody = kind === "update" ? String(currentPost.body) : ""
        titleField.text = originalTitle
        bodyField.text = originalBody
        typeField.currentIndex = Math.max(0, typeKeys.indexOf(editType))
        restoring = false
        errorMessage = ""
        statusMessage = ""
        titleField.forceActiveFocus()
        return true
    }
    function discardEdit() {
        restoring = true
        mode = ""
        titleField.text = ""
        bodyField.text = ""
        originalTitle = ""
        originalBody = ""
        restoring = false
        retain()
    }
    function publish() {
        if (!titleField.text.trim() || !bodyField.text.trim()) {
            errorMessage = "请填写标题和正文。"
            return false
        }
        return request(mode, {post_id: editPostId, expected_post_revision: editRevision,
            post_type: editType, title: titleField.text, body: bodyField.text,
            body_format: editFormat, attachments: editAttachments})
    }
    function loadHistory(pageOffset) {
        return request("history", {post_id: currentPost.post_id, offset: Math.max(0, pageOffset)})
    }
    function showReplies() {
        parentId = String(currentPost.post_id)
        currentPost = ({})
        historyVisible = false
        return refresh(0)
    }
    onVisibleChanged: if (visible && service && generation && !mode && !requestToken) refresh(offset)

    Connections {
        target: root.service
        function onCompleted(token, identity, sessionGeneration, result, error) {
            if (token !== root.requestToken || identity !== root.projectId || sessionGeneration !== root.generation) return
            const operation = root.pendingOperation
            root.requestToken = ""
            root.pendingOperation = ""
            if (error) {
                root.errorMessage = error + (root.mode ? " 未发布内容仍保留。" : "")
                if (operation === "attachment_preview" || operation === "attachment_export") root.attachmentError = error
                return
            }
            if (operation === "attachment_import") {
                root.editAttachments = root.editAttachments.concat([result.attachment])
                root.retain()
                root.statusMessage = "附件已复制到工程，发布后关联到本条记录。"
                return
            }
            if (operation === "attachment_preview") { root.attachmentResult = result; return }
            if (operation === "attachment_export") { root.statusMessage = "附件已导出。"; return }
            if (operation === "list") {
                root.posts = result.posts
                root.total = Number(result.total)
                root.offset = Number(result.offset)
            } else if (operation === "history") {
                root.revisions = result.revisions
                root.historyTotal = Number(result.total)
                root.historyOffset = Number(result.offset)
                root.historyVisible = true
            } else {
                root.currentPost = result
                root.historyVisible = false
                if (operation === "create" || operation === "reply" || operation === "update") {
                    root.discardEdit()
                    root.refresh(root.offset)
                    root.statusMessage = "已发布。"
                } else if (operation === "archive") root.statusMessage = result.archived ? "已归档，可恢复。" : "已恢复。"
            }
        }
    }

    ScrollView {
        id: pageScroll
        objectName: "projectBbsScroll"
        contentHeight: pageContent.height
        contentWidth: availableWidth
        anchors.fill: parent
        clip: true
        ScrollBar.vertical.policy: ScrollBar.AsNeeded
        ColumnLayout {
        id: pageContent
        width: pageScroll.availableWidth
        height: Math.max(root.height, root.themeTokens.controlHeightMd * 13
            + root.shownAttachments.length * (root.themeTokens.controlHeightMd + root.themeTokens.spaceSm))
        spacing: root.themeTokens.spaceSm
        ThemeControls.ThemeLabel {
            Layout.fillWidth: true
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            muted: true
            visible: !!root.mode || !root.readingFocus
            text: root.mode ? "未发布内容保留在本次工程会话；可保留并关闭窗口，稍后继续编辑。"
                            : "用户与助手共用工程记录；发布内容随工程保存，原文与修订历史可追溯。"
        }
        ThemeControls.ThemeLabel {
            objectName: "projectBbsMessage"
            Layout.fillWidth: true
            visible: root.busy || !!root.errorMessage || !!root.statusMessage
            text: root.busy ? "正在处理…" : root.errorMessage || root.statusMessage
            textFormat: Text.PlainText
            color: root.errorMessage ? root.themeTokens.colorError : root.themeTokens.colorTextMuted
            wrapMode: Text.Wrap
        }
        RowLayout {
            visible: !root.mode && !root.readingFocus
            Layout.fillWidth: true
            ThemeControls.ThemeComboBox {
                id: filter
                objectName: "projectBbsFilter"
                accessibleLabel: "记录类型筛选"
                model: ["全部类型", "工程日志", "进度", "研究报告", "工作报告", "评论"]
                enabled: !root.busy
                onActivated: root.refresh(0)
            }
            ThemeControls.ThemeField {
                id: searchField
                objectName: "projectBbsSearch"
                Layout.fillWidth: true
                Layout.minimumWidth: 30
                accessibleLabel: "搜索工程记录"
                placeholderText: "搜索标题或正文"
                maximumLength: 200
                enabled: !root.busy
                onAccepted: root.refresh(0)
            }
            ThemeControls.ThemeButton { text: "刷新"; enabled: !root.busy; onClicked: root.refresh(0) }
            CheckBox {
                id: archivedFilter
                text: "含归档"
                enabled: !root.busy
                palette.windowText: root.themeTokens.colorText
                onClicked: root.refresh(0)
            }
        }
        ListView {
            id: postList
            objectName: "projectBbsList"
            visible: !root.mode && !root.readingFocus
            Layout.fillWidth: true
            Layout.preferredHeight: root.themeTokens.controlHeightMd * 2.2
            clip: true
            model: root.posts
            ScrollBar.vertical: ScrollBar { }
            delegate: ItemDelegate {
                id: row
                required property var modelData
                width: postList.width
                enabled: !root.busy
                highlighted: root.currentPost.post_id === modelData.post_id
                contentItem: Text {
                    text: (row.modelData.archived ? "[归档] " : "") + root.typeName(row.modelData.post_type)
                          + " · " + row.modelData.title + " · " + root.actorName(row.modelData.author)
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: root.themeTokens.colorText
                }
                onClicked: root.readPost(modelData.post_id)
            }
            ThemeControls.ThemeLabel {
                anchors.centerIn: parent
                visible: !root.busy && root.posts.length === 0
                text: "暂无符合条件的记录"
                muted: true
            }
        }
        RowLayout {
            visible: !root.mode && !root.readingFocus
            Layout.fillWidth: true
            ThemeControls.ThemeButton {
                visible: !!root.parentId
                text: "返回主题"
                enabled: !root.busy
                onClicked: { root.parentId = ""; root.currentPost = ({}); root.refresh(0) }
            }
            ThemeControls.ThemeButton {
                objectName: "projectBbsNew"
                text: "写记录"
                primary: true
                enabled: !root.busy
                onClicked: root.beginEdit("create")
            }
            Item { Layout.fillWidth: true }
            ThemeControls.ThemeLabel { text: root.total + " 条"; muted: true }
            ThemeControls.ThemeButton { text: "上一页"; enabled: !root.busy && root.offset > 0; onClicked: root.refresh(root.offset - 20) }
            ThemeControls.ThemeButton { text: "下一页"; enabled: !root.busy && root.offset + 20 < root.total; onClicked: root.refresh(root.offset + 20) }
        }
        ThemeControls.ThemeLabel {
            visible: !root.mode && !!root.currentPost.post_id
            Layout.fillWidth: true
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            text: root.currentPost.title + " · " + root.actorName(root.currentPost.author) + " · "
                  + String(root.currentPost.created_at || "").slice(0, 16).replace("T", " ") + " UTC · 版本 " + root.currentPost.revision
                  + " · 最近编辑：" + root.actorName(root.currentPost.editor)
        }
        ScrollView {
            id: bodyViewScroll
            contentWidth: availableWidth
            visible: !root.mode && !root.historyVisible
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            TextArea {
                objectName: "projectBbsBodyView"
                implicitWidth: 0
                width: bodyViewScroll.availableWidth
                text: root.currentPost.body ? (root.richPreview && root.currentPost.body_format === "markdown" && root.service
                    ? root.service.renderMarkdown(String(root.currentPost.body)) : String(root.currentPost.body))
                    : "请选择记录阅读，或点击“写记录”。"
                textFormat: root.richPreview && root.currentPost.body && root.currentPost.body_format === "markdown" ? TextEdit.RichText : TextEdit.PlainText
                wrapMode: TextEdit.Wrap
                readOnly: true
                selectByMouse: true
                color: root.themeTokens.colorText
                selectionColor: root.themeTokens.colorSelection
                background: Rectangle { color: root.themeTokens.colorSurface; border.color: root.themeTokens.colorBorder }
            }
        }
        ListView {
            id: historyList
            objectName: "projectBbsHistoryList"
            visible: !root.mode && root.historyVisible
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            model: root.revisions
            ScrollBar.vertical: ScrollBar { }
            delegate: TextArea {
                required property var modelData
                width: historyList.width
                text: "版本 " + modelData.revision + " · " + root.actorName(modelData.editor) + " · " + modelData.edited_at
                      + (modelData.archived ? " · 已归档" : "") + "\n" + modelData.title + "\n" + modelData.body
                textFormat: TextEdit.PlainText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.Wrap
                color: root.themeTokens.colorText
                background: Rectangle { color: root.themeTokens.colorSurface; border.color: root.themeTokens.colorBorder }
            }
        }
        RowLayout {
            visible: !root.mode && root.historyVisible
            Layout.fillWidth: true
            ThemeControls.ThemeLabel { text: "历史 " + (root.historyOffset + 1) + "–" + Math.min(root.historyOffset + 10, root.historyTotal) + " / " + root.historyTotal; muted: true }
            Item { Layout.fillWidth: true }
            ThemeControls.ThemeButton { text: "较早"; enabled: !root.busy && root.historyOffset > 0; onClicked: root.loadHistory(root.historyOffset - 10) }
            ThemeControls.ThemeButton { text: "较新"; enabled: !root.busy && root.historyOffset + 10 < root.historyTotal; onClicked: root.loadHistory(root.historyOffset + 10) }
        }
        Flow {
            visible: !root.mode && !!root.currentPost.post_id
            Layout.fillWidth: true
            spacing: root.themeTokens.spaceSm
            ThemeControls.ThemeButton { objectName: "projectBbsReadingFocus"; text: root.readingFocus ? "返回列表" : "展开阅读"; onClicked: { root.readingFocus = !root.readingFocus; const flickable = pageScroll.contentItem as Flickable; if (flickable) flickable.contentY = 0 } }
            ThemeControls.ThemeButton { objectName: "projectBbsRichToggle"; text: root.richPreview ? "查看原文" : "富文本预览"; onClicked: root.richPreview = !root.richPreview }
            ThemeControls.ThemeButton { objectName: "projectBbsEdit"; text: "编辑"; enabled: !root.busy && !root.currentPost.archived; onClicked: root.beginEdit("update") }
            ThemeControls.ThemeButton { objectName: "projectBbsReply"; text: "回复"; enabled: !root.busy && !root.currentPost.parent_id && !root.currentPost.archived; onClicked: root.beginEdit("reply") }
            ThemeControls.ThemeButton { text: "查看回复 " + Number(root.currentPost.reply_count || 0); enabled: !root.busy && !root.currentPost.parent_id; onClicked: root.showReplies() }
            ThemeControls.ThemeButton { objectName: "projectBbsHistory"; text: root.historyVisible ? "当前正文" : "编辑历史"; enabled: !root.busy; onClicked: root.historyVisible ? root.historyVisible = false : root.loadHistory(0) }
            ThemeControls.ThemeButton { objectName: "projectBbsArchive"; text: root.currentPost.archived ? "恢复" : "归档"; enabled: !root.busy; onClicked: archiveDialog.open() }
        }
        RowLayout {
            visible: !!root.mode
            Layout.fillWidth: true
            ThemeControls.ThemeButton { objectName: "projectBbsEditorPreview"; text: root.editorPreview ? "编辑 Markdown" : "预览"; onClicked: root.editorPreview = !root.editorPreview }
            ThemeControls.ThemeLabel { text: root.mode === "create" ? "新记录" : root.mode === "reply" ? "回复记录" : "编辑 · 版本 " + root.editRevision }
            ThemeControls.ThemeComboBox {
                objectName: "projectBbsFormat"
                accessibleLabel: "正文格式"
                model: ["Markdown", "纯文本"]
                currentIndex: root.editFormat === "markdown" ? 0 : 1
                enabled: !root.busy
                onActivated: { root.editFormat = currentIndex === 0 ? "markdown" : "plain"; root.retain() }
            }
            ThemeControls.ThemeComboBox {
                id: typeField
                objectName: "projectBbsType"
                accessibleLabel: "记录类型"
                model: root.typeLabels
                enabled: root.mode === "create" && !root.busy
                onActivated: { root.editType = root.typeKeys[currentIndex]; root.retain() }
            }
        }
        ThemeControls.ThemeField {
            id: titleField
            objectName: "projectBbsTitle"
            visible: !!root.mode
            Layout.fillWidth: true
            accessibleLabel: "记录标题"
            placeholderText: "标题（最多 200 字）"
            maximumLength: 200
            enabled: !root.busy
            onTextChanged: root.retain()
        }
        ScrollView {
            id: bodyEditScroll
            contentWidth: availableWidth
            visible: !!root.mode && !root.editorPreview
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            TextArea {
                id: bodyField
                implicitWidth: 0
                width: bodyEditScroll.availableWidth
                objectName: "projectBbsBody"
                Accessible.name: "记录正文"
                placeholderText: "正文（支持 Markdown 标题、列表、表格、引用和代码；最多 64 KiB）"
                textFormat: TextEdit.PlainText
                wrapMode: TextEdit.Wrap
                selectByMouse: true
                enabled: !root.busy
                color: root.themeTokens.colorText
                selectionColor: root.themeTokens.colorSelection
                onTextChanged: root.retain()
                background: Rectangle { color: root.themeTokens.colorSurface; border.color: bodyField.activeFocus ? root.themeTokens.colorFocusRing : root.themeTokens.colorBorder }
            }
        }
        ScrollView {
            id: editorPreviewScroll
            contentWidth: availableWidth
            visible: !!root.mode && root.editorPreview
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            TextArea {
                objectName: "projectBbsEditorPreviewView"
                implicitWidth: 0
                width: editorPreviewScroll.availableWidth
                text: !visible ? "" : root.editFormat === "markdown" && root.service ? root.service.renderMarkdown(bodyField.text) : bodyField.text
                textFormat: root.editFormat === "markdown" ? TextEdit.RichText : TextEdit.PlainText
                wrapMode: TextEdit.Wrap
                readOnly: true
                selectByMouse: true
                color: root.themeTokens.colorText
                selectionColor: root.themeTokens.colorSelection
                background: Rectangle { color: root.themeTokens.colorSurface; border.color: root.themeTokens.colorBorder }
            }
        }
        ColumnLayout {
            visible: root.shownAttachments.length > 0
            Layout.fillWidth: true
            spacing: root.themeTokens.spaceSm
            Repeater {
                model: root.shownAttachments
                delegate: RowLayout {
                    id: attachmentRow
                    required property var modelData
                    Layout.fillWidth: true
                    ThemeControls.ThemeLabel {
                        Layout.fillWidth: true
                        Layout.minimumWidth: 20
                        text: attachmentRow.modelData.name + " · " + Math.ceil(Number(attachmentRow.modelData.size) / 1024) + " KiB"
                        textFormat: Text.PlainText
                        elide: Text.ElideMiddle
                    }
                    ThemeControls.ThemeButton {
                        objectName: "projectBbsPreview_" + attachmentRow.modelData.asset_id
                        text: "预览附件"
                        enabled: !root.busy
                        onClicked: root.previewAttachment(attachmentRow.modelData, 0)
                    }
                    ThemeControls.ThemeButton {
                        visible: !!root.mode
                        text: "移除"
                        enabled: !root.busy
                        onClicked: root.removeAttachment(attachmentRow.modelData.asset_id)
                    }
                }
            }
        }
        RowLayout {
            visible: !!root.mode
            Layout.fillWidth: true
            ThemeControls.ThemeButton { objectName: "projectBbsAddAttachment"; text: "添加附件"; enabled: !root.busy && root.editAttachments.length < 8; onClicked: root.openAttachmentImport() }
            Item { Layout.fillWidth: true }
            ThemeControls.ThemeButton { objectName: "projectBbsDiscard"; text: "取消编辑"; enabled: !root.busy; onClicked: root.dirty ? discardDialog.open() : root.discardEdit() }
            ThemeControls.ThemeButton { objectName: "projectBbsPublish"; text: root.mode === "update" ? "发布修改" : "发布"; primary: true; enabled: !root.busy; onClicked: root.publish() }
        }
    }
    }
    ThemeControls.ThemeDialogBase {
        id: discardDialog
        objectName: "projectBbsDiscardDialog"
        parent: Overlay.overlay
        anchors.centerIn: parent
        modal: true
        title: "放弃未发布内容？"
        standardButtons: Dialog.Discard | Dialog.Cancel
        onDiscarded: root.discardEdit()
    }
    ThemeControls.ThemeDialogBase {
        id: archiveDialog
        objectName: "projectBbsArchiveDialog"
        parent: Overlay.overlay
        anchors.centerIn: parent
        modal: true
        title: root.currentPost.archived ? "恢复这条记录？" : "归档这条记录？"
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: root.request("archive", {post_id: root.currentPost.post_id, archived: !root.currentPost.archived,
            expected_post_revision: root.currentPost.revision})
    }
    FileDialog {
        id: attachmentImportDialog
        objectName: "projectBbsImportAttachmentDialog"
        title: "选择工程附件（每个最多 32 MiB）"
        fileMode: FileDialog.OpenFile
        onAccepted: root.acceptAttachmentImport(selectedFile.toString())
        onRejected: root.attachmentImportTarget = ({})
    }
    FileDialog {
        id: attachmentExportDialog
        objectName: "projectBbsExportAttachmentDialog"
        title: "导出工程附件"
        fileMode: FileDialog.SaveFile
        onAccepted: root.acceptAttachmentExport(selectedFile.toString())
        onRejected: root.attachmentExportTarget = ({})
    }
    ThemeControls.ThemeDialogBase {
        id: attachmentDialog
        objectName: "projectBbsAttachmentDialog"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(780, parent ? parent.width - 32 : 780)
        height: Math.min(540, parent ? parent.height - 32 : 540)
        title: "工程附件预览"
        modal: true
        standardButtons: Dialog.Close
        contentItem: ProjectBbsAttachmentPreview {
            attachment: root.selectedAttachment
            preview: root.attachmentResult
            themeTokens: root.themeTokens
            busy: root.busy
            errorMessage: root.attachmentError
            onPreviewRequested: function(attachment, page) { root.previewAttachment(attachment, page) }
            onExportRequested: function(attachment) { root.openAttachmentExport(attachment) }
        }
    }
    function dismissDialogs() {
        attachmentImportTarget = ({})
        attachmentExportTarget = ({})
        discardDialog.close()
        archiveDialog.close()
        attachmentDialog.close()
        attachmentImportDialog.close()
        attachmentExportDialog.close()
        retain()
    }
}
