pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ColumnLayout {
    id: panel
    objectName: "filmReductionLiveFeedback"
    property var controller
    property var themeTokens
    visible: controller.searchMode === "reduction"
             && (controller.busy || controller.hasReductionFeedback)
    spacing: panel.themeTokens.spaceXs

    ThemeControls.ThemeLabel {
        objectName: "filmReductionLiveSummary"
        Layout.fillWidth: true
        text: panel.controller.reductionFeedbackSummary
        wrapMode: Text.WordWrap
    }
    ThemeControls.ThemeLabel {
        Layout.fillWidth: true
        muted: true
        visible: panel.controller.hasReductionFeedback
        text: "背景条表示当前最佳完整核验候选的逐层物理厚度；只在完整原始与独立加密网格完成后更新，不增加光谱计算。"
        wrapMode: Text.WordWrap
    }
    ListView {
        objectName: "filmReductionLiveLayerScroll"
        Layout.fillWidth: true
        Layout.preferredHeight: 220
        Layout.minimumHeight: panel.controller.hasReductionFeedback ? 80 : 0
        visible: panel.controller.hasReductionFeedback
        clip: true
        model: panel.controller.reductionFeedbackLayers
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        delegate: Item {
            id: row
            required property var modelData
            objectName: "filmReductionLiveLayer_" + modelData.row_id
            width: ListView.view.width
            implicitHeight: details.implicitHeight + 2 * panel.themeTokens.spaceXs

            Rectangle {
                objectName: "filmReductionLiveBar_" + row.modelData.row_id
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: parent.width * row.modelData.best_ratio
                radius: panel.themeTokens.radiusSm
                color: panel.themeTokens.colorAccent
                opacity: row.modelData.best_deleted ? 0.04 : 0.12
            }
            ColumnLayout {
                id: details
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: panel.themeTokens.spaceXs
                ThemeControls.ThemeLabel {
                    Layout.fillWidth: true
                    text: row.modelData.label
                    wrapMode: Text.WordWrap
                }
                ThemeControls.ThemeLabel {
                    objectName: "filmReductionLiveValues_" + row.modelData.row_id
                    Layout.fillWidth: true
                    text: "原厚 " + row.modelData.original + " · 最佳 " + row.modelData.best
                          + " · 变化 " + row.modelData.delta
                    wrapMode: Text.WordWrap
                }
                    }
        }
    }
}
