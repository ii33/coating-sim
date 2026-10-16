pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Window {
    id: host
    objectName: "assistantTrayMenuHost"
    visible: false
    color: "transparent"
    flags: Qt.Tool | Qt.FramelessWindowHint
    width: trayMenu.width
    height: trayMenu.implicitHeight

    function openAtCursor() {
        if (!desktop)
            return
        const point = desktop.trayMenuPosition(Math.ceil(trayMenu.implicitWidth),
                                                Math.ceil(trayMenu.implicitHeight))
        x = point.x
        y = point.y
        show()
        trayMenu.open()
    }

    function closeMenu() {
        trayMenu.close()
        hide()
    }

    Menu {
        id: trayMenu
        objectName: "assistantTrayQuickMenu"
        x: 0
        y: 0
        width: Math.max(230, implicitWidth)
        modal: false
        focus: true
        onClosed: host.hide()

        MenuItem {
            objectName: "assistantTrayQueueItem"
            text: "查看计算队列"
            onTriggered: desktop.showQueue()
        }

        MenuItem {
            objectName: "assistantTraySoundItem"
            text: "气泡提示音"
            checkable: true
            checked: desktop ? desktop.soundEnabled : false
            onTriggered: desktop.setSoundEnabled(checked)
        }

        MenuItem {
            objectName: "assistantTrayVolumeItem"
            implicitHeight: 42
            contentItem: RowLayout {
                spacing: 8
                Label { text: "音量" }
                Slider {
                    id: soundVolume
                    objectName: "assistantTrayVolumeSlider"
                    Layout.fillWidth: true
                    from: 0
                    to: 100
                    stepSize: 1
                    value: desktop ? desktop.notificationSoundVolumePercent : 70
                    onMoved: desktop.setNotificationSoundVolumePercent(Math.round(value))
                }
                ToolButton {
                    objectName: "assistantTrayPreviewSoundButton"
                    text: "试听"
                    Accessible.name: "试听提示音"
                    onClicked: desktop.previewNotificationSound(Math.round(soundVolume.value))
                }
            }
        }

        MenuItem {
            objectName: "assistantTrayNotificationItem"
            text: "查看最新提醒"
            onTriggered: desktop.openNotification()
        }

        MenuItem {
            objectName: "assistantCancelExitWaitingItem"
            text: "停止等待退出，保留助手"
            // DesktopAssistantController injects the desktop context property.
            // qmllint disable unqualified
            visible: desktop && desktop.applicationExitWaiting
            enabled: visible
            onTriggered: desktop.cancelExitWaiting()
            // qmllint enable unqualified
        }

        MenuItem {
            objectName: "assistantExitApplicationItem"
            text: "退出应用"
            // DesktopAssistantController injects the desktop context property.
            // qmllint disable unqualified
            enabled: desktop && desktop.applicationExitAvailable
            onTriggered: desktop.exitApplication()
            // qmllint enable unqualified
        }

        MenuItem {
            objectName: "assistantTrayVisibilityItem"
            text: desktop && desktop.hidden ? "显示看板娘" : "隐藏看板娘"
            onTriggered: desktop.toggleVisibility()
        }
    }
}
