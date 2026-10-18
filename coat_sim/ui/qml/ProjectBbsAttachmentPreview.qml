pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

Frame {
    id: root
    objectName: "projectBbsAttachmentPreview"
    required property var themeTokens
    property var attachment: ({})
    property var preview: ({})
    property bool busy: false
    property string errorMessage: ""
    signal previewRequested(var attachment, int page)
    signal exportRequested(var attachment)
    readonly property bool hasAttachment: !!attachment.asset_id
    readonly property bool hasPreview: hasAttachment && preview.asset_id === attachment.asset_id
    readonly property bool hasImage: hasPreview && preview.kind === "image"
    readonly property bool hasText: hasPreview && (preview.kind === "text" || preview.kind === "pdf_text")
    readonly property string safeImageSource: hasImage && /^image:\/\/projectbbs\/[0-9a-f]{32}$/.test(String(preview.image_source || "")) ? String(preview.image_source) : ""
    padding: root.themeTokens.spaceSm
    background: Rectangle { color: root.themeTokens.colorSurface; border.color: root.themeTokens.colorBorder; radius: root.themeTokens.radiusSm }
    visible: hasAttachment
    implicitHeight: visible ? content.implicitHeight + topPadding + bottomPadding : 0

    ColumnLayout {
        id: content
        anchors.fill: parent
        spacing: root.themeTokens.spaceSm
        RowLayout {
            Layout.fillWidth: true
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                text: String(root.attachment.name || "附件")
                textFormat: Text.PlainText
                elide: Text.ElideMiddle
            }
            ThemeControls.ThemeButton {
                objectName: "projectBbsAttachmentPreviewReload"
                text: "预览"
                enabled: root.hasAttachment && !root.busy
                onClicked: root.previewRequested(root.attachment, 0)
            }
            ThemeControls.ThemeButton {
                objectName: "projectBbsAttachmentExport"
                text: "导出"
                enabled: root.hasAttachment && !root.busy
                onClicked: root.exportRequested(root.attachment)
            }
        }
        ThemeControls.ThemeLabel {
            Layout.fillWidth: true
            visible: root.busy || root.errorMessage.length > 0 || !root.hasPreview
            text: root.busy ? "正在读取工程附件…" : root.errorMessage || "选择预览以读取工程内的附件。"
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }
        ThemeControls.ThemeLabel {
            Layout.fillWidth: true
            visible: root.hasPreview && !!root.preview.message
            text: String(root.preview.message || "")
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }
        Image {
            objectName: "projectBbsAttachmentImage"
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 320 : 0
            visible: root.hasImage && root.safeImageSource.length > 0
            source: root.safeImageSource
            sourceSize: Qt.size(1200, 1200)
            fillMode: Image.PreserveAspectFit
            cache: false
            asynchronous: false
        }
        ScrollView {
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 240 : 0
            visible: root.hasText
            clip: true
            TextArea {
                objectName: "projectBbsAttachmentText"
                text: root.hasText ? String(root.preview.text || "") : ""
                textFormat: TextEdit.PlainText
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.Wrap
                color: root.themeTokens.colorText
                font.family: root.themeTokens.fontFamily
                font.pointSize: root.themeTokens.fontSizeBody
                background: Rectangle { color: root.themeTokens.colorSurfaceRaised }
            }
        }
        ThemeControls.ThemeLabel {
            Layout.fillWidth: true
            visible: root.hasPreview && !!root.preview.truncated
            text: "仅显示前 64 KiB；导出可查看完整内容。"
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
        }
        RowLayout {
            visible: root.hasPreview && root.preview.kind === "pdf_text"
            ThemeControls.ThemeButton {
                objectName: "projectBbsAttachmentPreviousPage"
                text: "上一页"
                enabled: !root.busy && Number(root.preview.page || 0) > 0
                onClicked: root.previewRequested(root.attachment, Number(root.preview.page) - 1)
            }
            ThemeControls.ThemeLabel {
                text: "第 " + (Number(root.preview.page || 0) + 1) + " / " + Number(root.preview.page_count || 0) + " 页"
                textFormat: Text.PlainText
            }
            ThemeControls.ThemeButton {
                objectName: "projectBbsAttachmentNextPage"
                text: "下一页"
                enabled: !root.busy && Number(root.preview.page || 0) + 1 < Number(root.preview.page_count || 0)
                onClicked: root.previewRequested(root.attachment, Number(root.preview.page) + 1)
            }
        }
    }
}
