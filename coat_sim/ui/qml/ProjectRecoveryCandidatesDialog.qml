pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

Item {
    id: root
    objectName: "launcherRecoveryCandidatesController"

    required property var projectBridge
    required property var themeTokens
    property string projectId: ""
    property var candidates: []
    property string errorMessage: ""

    function sectionName(section) {
        if (section === "film") return "膜系"
        if (section === "resources") return "资源"
        if (section === "compute") return "计算配置"
        return String(section || "未知类别")
    }

    function openFor(projectId) {
        root.projectId = String(projectId || "")
        refresh()
        candidatesDialog.open()
    }

    // Close every recovery surface without accepting the current review. The
    // runtime owner remains responsible for cancelling its pending review.
    function dismiss() {
        archiveConfirm.close()
        candidatesDialog.close()
    }

    function refresh() {
        if (!root.projectBridge || !root.projectId)
            return
        root.candidates = root.projectBridge.recoveryCandidates(root.projectId)
        root.errorMessage = String(root.projectBridge.projectError || "")
    }

    Connections {
        target: root.projectBridge
        function onProjectsChanged() {
            if (candidatesDialog.visible)
                root.refresh()
        }
    }

    ThemeControls.ThemeDialogBase {
        id: candidatesDialog
        objectName: "launcherRecoveryCandidatesDialog"
        parent: Overlay.overlay
        modal: true
        anchors.centerIn: parent
        width: Math.min(720, parent ? parent.width - 32 : 720)
        height: Math.min(620, parent ? parent.height - 32 : 620)
        title: "异常恢复与已审阅归档"
        standardButtons: Dialog.NoButton

        contentItem: ColumnLayout {
            spacing: root.themeTokens.spaceSm
            implicitWidth: Math.min(680, candidatesDialog.width - 2 * candidatesDialog.padding)
            implicitHeight: Math.min(560, candidatesDialog.height - 2 * candidatesDialog.padding)

            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                text: "正常打开仍使用工程中配置的默认工艺。这里的异常退出恢复内容只会在你选择类别并确认后载入；归档副本会保留在列表中。"
                wrapMode: Text.WordWrap
                muted: true
            }

            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                visible: root.errorMessage.length > 0
                text: root.errorMessage
                wrapMode: Text.WordWrap
            }

            ScrollView {
                id: candidateScroll
                objectName: "launcherRecoveryCandidatesScroll"
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                ScrollBar.vertical.policy: ScrollBar.AsNeeded

                ColumnLayout {
                    width: candidateScroll.availableWidth
                    spacing: root.themeTokens.spaceSm

                    Repeater {
                        model: root.candidates

                        delegate: Rectangle {
                            id: candidateCard
                            required property var modelData
                            objectName: "launcherRecoveryCandidate_" + modelData.generation + "_" + (modelData.recordId || "pending")
                            Layout.fillWidth: true
                            implicitHeight: cardContent.implicitHeight + 2 * root.themeTokens.spaceSm
                            radius: root.themeTokens.radiusSm
                            color: root.themeTokens.colorSurfaceRaised
                            border.width: root.themeTokens.borderWidth
                            border.color: root.themeTokens.colorBorder

                            ColumnLayout {
                                id: cardContent
                                anchors.fill: parent
                                anchors.margins: root.themeTokens.spaceSm
                                spacing: root.themeTokens.spaceXs

                                ThemeControls.ThemeLabel {
                                    Layout.fillWidth: true
                                    heading: true
                                    wrapMode: Text.Wrap
                                    text: (candidateCard.modelData.reviewed ? "已审阅归档副本" : "未审阅异常会话")
                                          + " · " + String(candidateCard.modelData.generation)
                                          + " · 工程修订 " + candidateCard.modelData.revision
                                }

                                ThemeControls.ThemeLabel {
                                    Layout.fillWidth: true
                                    muted: true
                                    wrapMode: Text.Wrap
                                    text: (candidateCard.modelData.reviewed ? "归档时间：" : "最后检查点：")
                                          + (candidateCard.modelData.updatedAt || "时间未知")
                                          + (candidateCard.modelData.stale ? " · 对应的已保存工程已变化，不能直接恢复" : "")
                                }

                                ThemeControls.ThemeLabel {
                                    Layout.fillWidth: true
                                    wrapMode: Text.Wrap
                                    text: "保留类别：" + (candidateCard.modelData.sections || []).map(function(section) {
                                        return root.sectionName(section)
                                    }).join("、")
                                }

                                Flow {
                                    Layout.fillWidth: true
                                    spacing: root.themeTokens.spaceXs

                                    Repeater {
                                        model: candidateCard.modelData.sections || []
                                        delegate: ThemeControls.ThemeButton {
                                            required property var modelData
                                            objectName: "launcherRecoveryRestore_" + candidateCard.modelData.generation + "_" + modelData
                                            text: "恢复" + root.sectionName(modelData)
                                            enabled: !candidateCard.modelData.stale
                                                     && !root.projectBridge.projectBusy
                                            onClicked: root.projectBridge.reviewRecovery(
                                                           root.projectId,
                                                           candidateCard.modelData.generation,
                                                           String(modelData),
                                                           candidateCard.modelData.revision,
                                                           candidateCard.modelData.recordId || "")
                                        }
                                    }

                                    ThemeControls.ThemeButton {
                                        visible: !candidateCard.modelData.reviewed
                                        objectName: "launcherRecoveryArchive_" + candidateCard.modelData.generation
                                        text: "归档所选类别"
                                        enabled: !root.projectBridge.projectBusy
                                        onClicked: {
                                            archiveConfirm.generation = candidateCard.modelData.generation
                                            archiveConfirm.revision = candidateCard.modelData.revision
                                            archiveConfirm.section = candidateCard.modelData.sections.length === 1
                                                                    ? candidateCard.modelData.sections[0] : ""
                                            archiveConfirm.sectionChoices = candidateCard.modelData.sections || []
                                            root.errorMessage = ""
                                            archiveConfirm.open()
                                        }
                                    }
                                }
                            }
                        }
                    }

                    ThemeControls.ThemeLabel {
                        Layout.fillWidth: true
                        visible: root.candidates.length === 0
                        text: "没有保留的异常会话或已审阅副本。"
                        muted: true
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ThemeControls.ThemeButton {
                    objectName: "launcherRecoveryCandidatesClose"
                    text: "关闭"
                    onClicked: candidatesDialog.close()
                }
            }
        }
    }

    ThemeControls.ThemeDialogBase {
        id: archiveConfirm
        objectName: "launcherRecoveryArchiveConfirm"
        parent: Overlay.overlay
        modal: true
        anchors.centerIn: parent
        width: Math.min(520, parent ? parent.width - 32 : 520)
        title: "保留并归档恢复内容"
        property string generation: ""
        property int revision: -1
        property string section: ""
        property var sectionChoices: []
        contentItem: ColumnLayout {
            spacing: root.themeTokens.spaceMd
            implicitWidth: Math.min(480, archiveConfirm.width - 2 * archiveConfirm.padding)

            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                text: "选择要从会话 " + String(archiveConfirm.generation)
                      + " 移入可恢复的已审阅归档。未选择的类别会继续保留为异常恢复候选；归档副本可在此列表中再次恢复。\n所选类别："
                      + (archiveConfirm.section ? root.sectionName(archiveConfirm.section) : "尚未选择")
            }

            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                visible: root.errorMessage.length > 0
                text: root.errorMessage
                wrapMode: Text.WordWrap
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: root.themeTokens.spaceXs
                ThemeControls.ThemeButton {
                    visible: archiveConfirm.sectionChoices.indexOf("film") >= 0
                    objectName: "launcherRecoveryArchiveChoice_film"
                    text: root.sectionName("film")
                    Layout.fillWidth: true
                    primary: archiveConfirm.section === "film"
                    onClicked: archiveConfirm.section = "film"
                }
                ThemeControls.ThemeButton {
                    visible: archiveConfirm.sectionChoices.indexOf("resources") >= 0
                    objectName: "launcherRecoveryArchiveChoice_resources"
                    text: root.sectionName("resources")
                    Layout.fillWidth: true
                    primary: archiveConfirm.section === "resources"
                    onClicked: archiveConfirm.section = "resources"
                }
                ThemeControls.ThemeButton {
                    visible: archiveConfirm.sectionChoices.indexOf("compute") >= 0
                    objectName: "launcherRecoveryArchiveChoice_compute"
                    text: root.sectionName("compute")
                    Layout.fillWidth: true
                    primary: archiveConfirm.section === "compute"
                    onClicked: archiveConfirm.section = "compute"
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Item { Layout.fillWidth: true }
                ThemeControls.ThemeButton {
                    text: "取消"
                    onClicked: archiveConfirm.close()
                }
                ThemeControls.ThemeButton {
                    objectName: "launcherRecoveryArchiveConfirmButton"
                    text: "归档所选类别"
                    enabled: archiveConfirm.section !== ""
                    onClicked: {
                        const success = root.projectBridge.archiveReviewedRecovery(
                                           root.projectId, archiveConfirm.generation,
                                           archiveConfirm.section, archiveConfirm.revision)
                        if (success) {
                            archiveConfirm.close()
                            root.errorMessage = ""
                            root.refresh()
                        } else {
                            root.errorMessage = String(root.projectBridge.projectError || "归档失败；原候选仍保留。")
                        }
                    }
                }
            }
        }
    }
}
