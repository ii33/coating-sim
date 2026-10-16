import QtQuick
import QtQuick.Controls

Window {
    id: notice
    title: "看板娘提示"
    readonly property var themeColors: typeof Theme !== "undefined" ? Theme.snapshot : null
    palette.window: themeColors ? themeColors.colorWindow : palette.window
    palette.windowText: themeColors ? themeColors.colorText : palette.windowText
    palette.base: themeColors ? themeColors.colorSurface : palette.base
    palette.alternateBase: themeColors ? themeColors.colorSurfaceRaised : palette.alternateBase
    palette.text: themeColors ? themeColors.colorText : palette.text
    palette.button: themeColors ? themeColors.colorSurfaceRaised : palette.button
    palette.buttonText: themeColors ? themeColors.colorText : palette.buttonText
    palette.highlight: themeColors ? themeColors.colorSelection : palette.highlight
    palette.highlightedText: themeColors ? themeColors.colorText : palette.highlightedText
    palette.placeholderText: themeColors ? themeColors.colorTextMuted : palette.placeholderText
    palette.mid: themeColors ? themeColors.colorBorder : palette.mid
    palette.dark: themeColors ? themeColors.colorBorderStrong : palette.dark
    width: 300
    height: 108
    visible: false
    color: "transparent"
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
    property int notificationSerial: 0
    onNotificationSerialChanged: dismiss.restart()
    Rectangle {
        anchors.fill: parent
        anchors.margins: 2
        radius: notice.themeColors ? notice.themeColors.radiusLg : 14
        border.width: notice.themeColors ? notice.themeColors.borderWidth : 1
        color: notice.themeColors ? notice.themeColors.colorSurface : notice.palette.window
        border.color: notice.themeColors ? notice.themeColors.colorBorder : notice.palette.mid
        Column {
            anchors.fill: parent
            anchors.margins: 12
            spacing: 6
            Label { text: "看板娘"; font.bold: true; color: notice.themeColors ? notice.themeColors.colorAccent : notice.palette.highlight }
            Label {
                width: parent.width
                text: desktop ? desktop.notificationText : ""
                wrapMode: Text.WordWrap
                maximumLineCount: 3
                elide: Text.ElideRight
                color: notice.themeColors ? notice.themeColors.colorText : notice.palette.windowText
            }
        }
        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: { desktop.openNotification(); notice.hide() }
        }
    }
    Timer { id: dismiss; interval: 6500; onTriggered: notice.hide() }
}
