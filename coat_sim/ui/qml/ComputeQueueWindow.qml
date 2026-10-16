pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "theme_controls" as ThemeControls
import "theme_controls/NumericFormat.js" as NumericFormat

AppWindow {
    id: window
    property bool projectCloseAuthorized: false
    property var projectNavigation: null
    titleBase: "计算队列"
    projectNameForTitle: projectNavigation ? projectNavigation.projectName : ""
    initialMaximized: false
    width: 900
    height: 620
    minimumWidth: 360
    minimumHeight: 300
    readonly property string queueStatusText: computeQueue ? String(computeQueue.status || "") : ""
    statusText: queueStatusText.length > 0
        ? queueStatusText : (computeQueue && computeQueue.loading ? "正在加载…" : "")
    // The service-wide status is plain text, so its wording must not be used
    // to infer severity. Per-task failures remain in their typed card fields.
    statusSeverity: computeQueue && computeQueue.loading ? "busy" : "info"
    statusDetail: queueStatusText

    component QueueTabButton: TabButton {
        id: tab
        focusPolicy: Qt.StrongFocus
        font.family: Theme.snapshot.fontFamily
        font.pointSize: Theme.snapshot.fontSizeBody
        contentItem: ThemeControls.ThemeLabel {
            text: tab.text
            font: tab.font
            color: !tab.enabled ? Theme.snapshot.colorDisabledText : Theme.snapshot.colorText
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: Theme.snapshot.radiusSm
            color: !tab.enabled ? Theme.snapshot.colorDisabledSurface
                  : tab.checked ? Theme.snapshot.colorSelection
                  : tab.down ? Theme.snapshot.colorAccentPressed
                  : tab.hovered ? Theme.snapshot.colorSurfaceRaised
                                : Theme.snapshot.colorSurface
            border.width: tab.activeFocus ? Theme.snapshot.focusRingWidth : Theme.snapshot.borderWidth
            border.color: tab.activeFocus ? Theme.snapshot.colorFocusRing
                         : tab.hovered && tab.enabled ? Theme.snapshot.colorBorderStrong
                                                     : Theme.snapshot.colorBorder
        }
    }

    ColumnLayout {
        objectName: "computeQueueContentLayout"
        anchors.fill: parent
        anchors.margins: 18
        anchors.topMargin: window.contentTopMargin + (window.themeColors ? window.themeColors.spaceSm : 8)
        spacing: 12
        ProjectModuleSelector {
            navigation: window.projectNavigation
            currentModuleKey: "compute_queue"
            Layout.fillWidth: false
        }

        ThemeControls.ThemeLabel {
            Layout.fillWidth: true
            text: "任务进度、最佳评分与时间估算。运行任务显示预计完成时间；终态显示实际完成、失败或取消时间。"
            wrapMode: Text.WordWrap
            color: window.themeColors ? window.themeColors.colorTextMuted : window.palette.placeholderText
        }
        TabBar {
            id: queueTabs
            objectName: "queueTabs"
            Layout.fillWidth: true
            onCurrentIndexChanged: if (computeQueue) computeQueue.selectQueueTab(currentIndex)
            QueueTabButton { text: "最近任务" }
            QueueTabButton { text: "历史任务" }
        }
        ThemeControls.ThemeLabel {
            Layout.fillWidth: true
            text: queueTabs.currentIndex === 0
                  ? "最近任务包括全部排队中、运行中的任务，以及最近 24 小时内结束的任务。"
                  : "历史任务包括 24 小时前结束的任务，以及缺少有效结束时间的已结束任务；历史记录按页加载。"
            wrapMode: Text.WordWrap
            color: window.themeColors ? window.themeColors.colorTextMuted : window.palette.placeholderText
        }
        ScrollView {
            id: scroll
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            contentWidth: availableWidth
            Column {
                width: scroll.availableWidth
                spacing: 10
                Repeater {
                    id: queueJobs
                    objectName: "queueJobs"
                    model: computeQueue ? computeQueue.jobs : []
                    delegate: ThemeControls.ThemeSurface {
                        id: task
                        readonly property bool resultButtonEnabled: resultButton.enabled
                        readonly property bool resultWarningVisible: missingQueueResultWarning.visible
                        readonly property string resultWarningText: missingQueueResultWarning.text
                        required property var modelData
                        readonly property var progress: modelData.progress || ({})
                        function hasNumber(value) { return typeof value === "number" && isFinite(value) }
                        readonly property real searchDone: hasNumber(progress.completed_evaluations) ? progress.completed_evaluations : (progress.evaluations || 0)
                        readonly property real searchTotal: hasNumber(progress.total_evaluations) ? progress.total_evaluations : (progress.max_evaluations || 0)
                        readonly property bool isSimulation: modelData.kind === "process_recipe" || modelData.kind === "process"
                        readonly property string finishTimeText: String(modelData.finishTimeText || "完成时间未知")
                        readonly property real progressFraction: modelData.state === "succeeded" ? 1 : (hasNumber(progress.fraction) ? progress.fraction : Math.min(.99, searchDone / Math.max(1, searchTotal)))
                        readonly property var searchScore: hasNumber(progress.best_search_score) ? progress.best_search_score : progress.best_score
                        readonly property string scopeLabel: progress.score_scope === "source_contribution" ? ("源 " + (progress.source_slot || "") + " 贡献") : "全膜系"
                        readonly property bool cancelPending: !!modelData.cancel_requested ||
                            (computeQueue && computeQueue.pendingCancelJobIds.indexOf(modelData.job_id) >= 0)
                        readonly property string stageLabel: ({"queued": "排队等待", "integrate": "沉积积分", "baseline": "无选板基线", "current": "原板评分", "response": "初形扫描", "optimize": "轮廓搜索", "verify": "原网格候选验证", "group_complete": "本组验证完成", "verify_full": "全膜系最终验证", "full_verify": "全膜系最终验证", "materialize": "保存验证方案与结果", "complete": "最终验证完成"})[progress.stage] || progress.stage || "等待进度"
                        readonly property color stateInk: window.themeColors
                            ? (modelData.state === "succeeded" ? window.themeColors.colorSuccess
                               : ["failed", "cancelled"].indexOf(modelData.state) >= 0
                                 ? window.themeColors.colorError
                                 : modelData.state === "running" ? window.themeColors.colorInfo
                                                                   : window.themeColors.colorTextMuted)
                            : window.palette.placeholderText
                        function scoreLabel(score, cv) {
                            if (!hasNumber(score)) return "尚无有效评分"
                            if (progress.objective === "target_map") return "归一化 RMSE " + NumericFormat.format(score, Qt.locale(), 3)
                            return "CV " + NumericFormat.format(hasNumber(cv) ? cv : score * 100, Qt.locale(), 3) + "%"
                        }
                        width: parent.width
                        padding: Theme.snapshot.spaceMd
                        contentSpacing: Theme.snapshot.spaceSm
                        height: taskContent.implicitHeight + topPadding + bottomPadding
                        raised: taskHover.hovered
                        HoverHandler { id: taskHover }
                        contentItem: ColumnLayout {
                            id: taskContent
                            spacing: Theme.snapshot.spaceSm
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { Layout.fillWidth: true; text: task.modelData.summary || "计算任务"; wrapMode: Text.WordWrap; font.bold: true }
                                ThemeControls.ThemeLabel { text: task.modelData.stateLabel; color: task.stateInk }
                            }
                            Flow {
                                Layout.fillWidth: true
                                spacing: 18
                                ThemeControls.ThemeLabel { text: "预计用时：" + task.modelData.durationLabel }
                    ThemeControls.ThemeLabel { text: task.finishTimeText }
                            ThemeControls.ThemeLabel {
                                objectName: "queueTerminalReason"
                                Layout.fillWidth: true
                                visible: ["failed", "cancelled"].indexOf(task.modelData.state) >= 0 && !!task.modelData.error
                                text: task.modelData.error || ""
                                wrapMode: Text.WordWrap
                                color: window.themeColors ? window.themeColors.colorWarning
                                                          : window.palette.windowText
                            }
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                visible: !!task.modelData.memory_estimate_label
                                text: task.modelData.memory_estimate_label || ""
                                wrapMode: Text.WordWrap
                                color: window.themeColors ? window.themeColors.colorTextMuted : window.palette.placeholderText
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                visible: Object.keys(task.progress).length > 0
                                text: "阶段：" + task.stageLabel + (task.progress.group_count ? " · 第 " + task.progress.group_index + " / " + task.progress.group_count + " 组" : "")
                                wrapMode: Text.WordWrap
                            }
                            ProgressBar {
                                Layout.fillWidth: true
                                visible: task.isSimulation || task.searchTotal > 0
                                from: 0
                                to: 1
                                value: Math.max(0, Math.min(1, task.progressFraction))
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: task.isSimulation || task.searchTotal > 0 || task.modelData.state === "succeeded" ? (task.progressFraction * 100).toFixed(1) + "%" : "进度待更新"; Layout.fillWidth: true }
                                ThemeControls.ThemeButton {
                                    id: resultButton
                                    objectName: "viewQueueResult"
                                     text: task.modelData.state === "succeeded" ? "查看结果" : "查看最佳候选"
                                     visible: ["succeeded", "failed", "cancelled"].indexOf(task.modelData.state) >= 0
                                     enabled: task.modelData.resultAvailable === true
                                    onClicked: computeQueue.openQueueResult(task.modelData.job_id)
                                }
                                ThemeControls.ThemeButton {
                                    objectName: "cancelQueueJob"
                                    text: task.cancelPending ? "正在取消" : "取消"
                                    enabled: !task.cancelPending
                                    visible: task.modelData.state === "queued" || task.modelData.state === "running"
                                    onClicked: computeQueue.cancelQueueJob(task.modelData.job_id)
                                }
                            }
                            ThemeControls.ThemeLabel {
                                id: missingQueueResultWarning
                                objectName: "missingQueueResultWarning"
                                Layout.fillWidth: true
                                 visible: ["succeeded", "failed", "cancelled"].indexOf(task.modelData.state) >= 0 && task.modelData.resultAvailable === false
                                text: task.modelData.resultUnavailableReason || "结果文件已不存在；任务历史仍保留，不能仅凭摘要恢复计算结果。"
                                wrapMode: Text.WordWrap
                                color: window.themeColors ? window.themeColors.colorWarning
                                                          : window.palette.windowText
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                visible: !task.isSimulation && task.searchTotal > 0
                                text: "搜索评分：" + task.searchDone + " / " + task.searchTotal + " 次（搜索预算进度）" + (task.progress.verification_max_evaluations ? " · 原网格验证：" + (task.progress.verification_evaluations || 0) + " / " + task.progress.verification_max_evaluations + " 个候选" : "")
                                wrapMode: Text.WordWrap
                                color: window.themeColors ? window.themeColors.colorTextMuted : window.palette.placeholderText
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                visible: task.hasNumber(task.searchScore)
                                text: "搜索最佳：" + task.scoreLabel(task.searchScore, task.progress.best_search_cv_percent) + "（" + task.scopeLabel + "，" + (task.progress.search_grid_size || "?") + " 网格）"
                                wrapMode: Text.WordWrap
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                visible: task.hasNumber(task.progress.best_verified_score)
                                text: "原网格验证最佳：" + task.scoreLabel(task.progress.best_verified_score, task.progress.best_verified_cv_percent) + "（" + task.scopeLabel + "，" + (task.progress.verification_grid_size || "?") + " 网格）"
                                wrapMode: Text.WordWrap
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                visible: task.progress.score_scope === "source_contribution" && task.hasNumber(task.progress.full_recipe_cv_percent)
                                text: "全膜系最终 CV：" + (task.progress.full_recipe_cv_percent || 0).toFixed(4) + "%"
                                wrapMode: Text.WordWrap
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                visible: typeof task.progress.accepted === "boolean"
                                text: task.progress.accepted ? "最终验证已保留候选方案。" : "最终验证未发现可采纳改善，保留原方案。"
                                wrapMode: Text.WordWrap
                                color: window.themeColors ? window.themeColors.colorSuccess
                                                          : window.palette.windowText
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                visible: !!(task.modelData.result && task.modelData.result.warning)
                                text: task.modelData.result ? (task.modelData.result.warning || "") : ""
                                wrapMode: Text.WordWrap
                                color: window.themeColors ? window.themeColors.colorWarning
                                                          : window.palette.windowText
                            }
                            ThemeControls.ThemeLabel {
                                Layout.fillWidth: true
                                visible: !!task.modelData.error
                                text: task.modelData.error || ""
                                wrapMode: Text.WordWrap
                                color: window.themeColors ? window.themeColors.colorError
                                                          : window.palette.windowText
                            }
                        }
                    }
                }
                ThemeControls.ThemeButton {
                    objectName: "loadMoreQueueJobs"
                    width: parent.width
                    visible: computeQueue && computeQueue.hasMoreJobs
                    enabled: computeQueue && !computeQueue.loading
                    text: computeQueue && computeQueue.loading ? "正在加载" : "加载更多"
                    onClicked: computeQueue.loadMoreQueueJobs()
                }
                ThemeControls.ThemeLabel {
                    objectName: "queueEmptyState"
                    width: parent.width
                    visible: computeQueue && computeQueue.jobs.length === 0
                    text: computeQueue && computeQueue.loading ? "正在加载任务..." : (queueTabs.currentIndex === 0 ? "暂无最近任务" : "暂无历史任务")
                    color: window.themeColors ? window.themeColors.colorTextMuted : window.palette.placeholderText
                    horizontalAlignment: Text.AlignHCenter
                    padding: 30
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            ThemeControls.ThemeButton { text: "刷新"; onClicked: computeQueue.refreshQueue() }
            ThemeControls.ThemeButton { text: "关闭"; onClicked: window.close() }
        }
    }
}
