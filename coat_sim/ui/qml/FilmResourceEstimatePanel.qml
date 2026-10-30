import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

ColumnLayout {
    id: panel
    objectName: "filmResourceEstimatePanel"
    required property var estimator
    required property var sourceController
    required property var themeTokens
    spacing: themeTokens.spaceSm
    signal startRequested()

    Label {
        Layout.fillWidth: true
        text: "资源评估 · 使用当前目标与候选材料的冻结快照；不会修改设计或优化约束。"
        wrapMode: Text.WordWrap
        color: panel.themeTokens.colorTextMuted
    }
    Label {
        objectName: "filmResourceEstimateSource"
        Layout.fillWidth: true
        text: panel.estimator.sourceSummary
        wrapMode: Text.WordWrap
        color: panel.themeTokens.colorText
    }
    RowLayout {
        Layout.fillWidth: true
        Label { text: "点频必要界枚举至层数"; color: panel.themeTokens.colorText }
        ThemeControls.ThemeField {
            objectName: "filmResourceEstimateLayerLimit"
            Layout.preferredWidth: 90
            enabled: !panel.estimator.busy
            text: String(panel.estimator.layerLimit)
            onEditingFinished: panel.estimator.setLayerLimit(text)
        }
    }
    Label {
        Layout.fillWidth: true
        text: "仅控制评估枚举范围，不是设计或优化限制；超出范围为 unknown。"
        wrapMode: Text.WordWrap
        color: panel.themeTokens.colorTextMuted
    }
    RowLayout {
        Layout.fillWidth: true
        ThemeControls.ThemeButton {
            objectName: "filmResourceEstimateStart"
            text: "开始评估"
            primary: true
            enabled: !panel.estimator.busy && panel.estimator.limitValid
            onClicked: panel.startRequested()
        }
        ThemeControls.ThemeButton {
            objectName: "filmResourceEstimateCancel"
            text: "取消"
            enabled: panel.estimator.busy
            onClicked: panel.estimator.cancel()
        }
        ThemeControls.ThemeButton {
            objectName: "filmResourceEstimateTargets"
            text: "目标与材料设置…"
            enabled: !panel.estimator.busy
            onClicked: panel.sourceController.openResourceEstimateSettings()
        }
    }
    Label {
        objectName: "filmResourceEstimateStatus"
        Layout.fillWidth: true
        text: panel.estimator.status
        color: panel.estimator.stale ? panel.themeTokens.colorError : panel.themeTokens.colorText
        wrapMode: Text.WordWrap
    }
    Label {
        objectName: "filmResourceEstimateResult"
        Layout.fillWidth: true
        text: panel.estimator.resultSummary
        color: panel.themeTokens.colorText
        wrapMode: Text.WordWrap
    }
}
