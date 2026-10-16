pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."
import "theme_controls" as ThemeControls

AppWindow {
    id: window
    title: "看板娘 · 待决事项"
    initialMaximized: false
    width: 640
    height: Math.max(400, Math.min(700, body.implicitHeight + 86))
    minimumWidth: 360
    minimumHeight: 350
    readonly property var currentDecision: decisionUi ? decisionUi.decision : ({})
    readonly property string decisionId: String(currentDecision.decision_id || "")
    statusText: String(currentDecision.error || "").length > 0 ? "提交失败" : ""
    statusSeverity: String(currentDecision.error || "").length > 0 ? "error" : "info"
    statusDetail: String(currentDecision.error || "").length > 0
        ? statusText + ": " + String(currentDecision.error || "") : ""
    property int selectedIndex: 0
    property double clockMs: Date.now()
    readonly property int remainingSeconds: currentDecision.deadline_ms
        ? Math.max(0, Math.ceil((Number(currentDecision.deadline_ms) - clockMs) / 1000))
        : Number(currentDecision.remaining_s || 0)

    onDecisionIdChanged: {
        selectedIndex = 0
        supplement.text = ""
        clockMs = Date.now()
    }
    onClosing: function(close) {
        close.accepted = false
        if (decisionId)
            decisionUi.deferDecision(decisionId)
        window.hide()
    }
    Timer {
        interval: 500
        running: window.visible && window.currentDecision.optional === true
        repeat: true
        onTriggered: window.clockMs = Date.now()
    }

    ColumnLayout {
        id: body
        objectName: "assistantDecisionContentLayout"
        anchors.fill: parent
        anchors.margins: 18
        anchors.topMargin: window.contentTopMargin + (window.themeColors ? window.themeColors.spaceSm : 8)
        spacing: 12
        Label {
            Layout.fillWidth: true
            text: window.currentDecision.question || "等待选择"
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            font.bold: true
            font.pointSize: Qt.application.font.pointSize + 1
        }
        ScrollView {
            id: choices
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 130
            contentWidth: availableWidth
            clip: true
            Column {
                width: choices.availableWidth
                spacing: 8
                Repeater {
                    model: window.currentDecision.options || []
                    delegate: Rectangle {
                        id: choice
                        required property int index
                        required property var modelData
                        width: parent.width
                        height: optionBody.implicitHeight + 20
                        radius: 8
                        color: window.selectedIndex === choice.index ? window.palette.alternateBase : window.palette.base
                        border.color: window.selectedIndex === choice.index ? window.palette.highlight : window.palette.mid
                        ColumnLayout {
                            id: optionBody
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 10
                            spacing: 2
                            RadioButton {
                                id: optionButton
                                Layout.fillWidth: true
                                checked: window.selectedIndex === choice.index
                                text: choice.modelData.label + (choice.index === 0 ? "（推荐）" : "")
                                enabled: window.currentDecision.submitting !== true
                                contentItem: Text {
                                    text: optionButton.text
                                    textFormat: Text.PlainText
                                    font: optionButton.font
                                    color: window.palette.windowText
                                    leftPadding: optionButton.indicator.width + optionButton.spacing
                                    verticalAlignment: Text.AlignVCenter
                                    wrapMode: Text.WordWrap
                                }
                                onClicked: {
                                    window.selectedIndex = choice.index
                                    decisionUi.pauseDecisionCountdown(window.decisionId)
                                }
                            }
                            Label {
                                Layout.fillWidth: true
                                Layout.leftMargin: 30
                                text: choice.modelData.description || ""
                                textFormat: Text.PlainText
                                color: window.palette.placeholderText
                                wrapMode: Text.WordWrap
                            }
                        }
                    }
                }
            }
        }
        TextArea {
            id: supplement
            Layout.fillWidth: true
            Layout.preferredHeight: 82
            placeholderText: "补充要求，或输入自己的选择"
            wrapMode: TextEdit.Wrap
            enabled: window.currentDecision.submitting !== true
            onTextChanged: {
                if (text.trim().length > 0 && window.decisionId)
                    decisionUi.pauseDecisionCountdown(window.decisionId)
            }
        }
        Label {
            Layout.fillWidth: true
            text: window.currentDecision.optional === true
                ? window.remainingSeconds + " 秒后采用第一项默认值；你也可以现在选择。"
                : "请明确选择或补充输入；关闭窗口会保留待决事项。"
            color: window.palette.placeholderText
            wrapMode: Text.WordWrap
        }
        Label {
            Layout.fillWidth: true
            visible: window.currentDecision.source === "compute"
            text: "补充计算要求会取消本次估算，并交给板娘重新估算。"
            color: window.palette.placeholderText
            wrapMode: Text.WordWrap
        }
        RowLayout {
            Layout.fillWidth: true
            ToolButton {
                text: "稍后"
                enabled: window.currentDecision.submitting !== true
                onClicked: {
                    decisionUi.deferDecision(window.decisionId)
                    window.hide()
                }
            }
            Item { Layout.fillWidth: true }
            ToolButton {
                visible: supplement.text.trim().length > 0
                text: "仅发送补充"
                enabled: window.currentDecision.submitting !== true
                onClicked: decisionUi.submitDecision(window.decisionId, -1, supplement.text.trim())
            }
            ThemeControls.ThemeButton {
                objectName: "decisionSubmitButton"
                primary: true
                text: window.currentDecision.submitting === true ? "正在确认" : "采用此项"
                enabled: window.decisionId.length > 0 && window.currentDecision.submitting !== true
                onClicked: decisionUi.submitDecision(window.decisionId, window.selectedIndex, supplement.text.trim())
            }
        }
    }
}
