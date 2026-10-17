import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Window {
    id: bubble
    title: "看板娘"
    readonly property var themeColors: typeof Theme !== "undefined" ? Theme.snapshot : null
    readonly property var themeAssets: typeof Theme !== "undefined" ? Theme.assets : ({})
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
    palette.disabled.windowText: themeColors ? themeColors.colorDisabledText : palette.disabled.windowText
    palette.disabled.text: themeColors ? themeColors.colorDisabledText : palette.disabled.text
    palette.disabled.buttonText: themeColors ? themeColors.colorDisabledText : palette.disabled.buttonText
    palette.disabled.placeholderText: themeColors ? themeColors.colorDisabledText : palette.disabled.placeholderText
    palette.disabled.base: themeColors ? themeColors.colorDisabledSurface : palette.disabled.base
    palette.disabled.alternateBase: themeColors ? themeColors.colorDisabledSurface : palette.disabled.alternateBase
    palette.disabled.button: themeColors ? themeColors.colorDisabledSurface : palette.disabled.button
    palette.disabled.window: themeColors ? themeColors.colorDisabledSurface : palette.disabled.window
    Accessible.name: String(themeAssets.floatingIconAccessibleName || title)
    width: assistant.iconSize
    height: width
    visible: false
    color: "transparent"
    transientParent: null
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus

    function openContextMenu() {
        if (!bubble.visible)
            return
        const point = desktop.menuPosition(Math.round(bubble.x), Math.round(bubble.y),
            Math.ceil(bubble.width), Math.ceil(bubble.height),
            Math.ceil(Math.max(contextMenu.width, contextMenu.implicitWidth)),
            Math.ceil(Math.max(contextMenu.height, contextMenu.implicitHeight)))
        contextMenu.popup(bubbleMouseArea, Qt.point(point.x, point.y))
    }

    Rectangle {
        anchors.fill: parent
        radius: width / 2
        color: bubble.themeColors ? bubble.themeColors.colorSurface : bubble.palette.base
        border.color: bubble.themeColors ? bubble.themeColors.colorAccent : bubble.palette.highlight
        border.width: bubble.themeColors ? bubble.themeColors.borderWidth : 2
    }

    Image {
        anchors.fill: parent
        anchors.margins: 4
        source: String(bubble.themeAssets.floatingIconUrl || assistant.portraitUrl)
        Accessible.ignored: true
        fillMode: Image.PreserveAspectFit
        smooth: true
        mipmap: true
    }

    MouseArea {
        id: bubbleMouseArea
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        cursorShape: Qt.PointingHandCursor
        property real pressX: 0
        property real pressY: 0
        property bool moved: false

        onPressed: function(mouse) {
            if (mouse.button !== Qt.LeftButton)
                return
            pressX = mouse.x
            pressY = mouse.y
            moved = false
        }
        onPositionChanged: function(mouse) {
            if ((pressedButtons & Qt.LeftButton) && !moved &&
                    Math.abs(mouse.x - pressX) + Math.abs(mouse.y - pressY) > 8) {
                moved = true
                bubble.startSystemMove()
            }
        }
        onClicked: function(mouse) {
            // Finish the originating right-button release before a new popup
            // window takes the mouse grab/focus.
            if (mouse.button === Qt.RightButton) {
                Qt.callLater(bubble.openContextMenu)
                return
            }
            if (mouse.button === Qt.LeftButton && !moved)
                assistant.toggleChat()
        }
    }

    Menu {
        id: contextMenu
        objectName: "assistantContextMenu"
        // A custom background has no implicit width; Fusion can otherwise
        // create a two-pixel popup despite opened/visible both being true.
        width: Math.max(230, implicitWidth)
        popupType: Popup.Window
        focus: true
        palette.window: bubble.themeColors ? bubble.themeColors.colorWindow : palette.window
        palette.windowText: bubble.themeColors ? bubble.themeColors.colorText : palette.windowText
        palette.base: bubble.themeColors ? bubble.themeColors.colorSurface : palette.base
        palette.alternateBase: bubble.themeColors ? bubble.themeColors.colorSurfaceRaised : palette.alternateBase
        palette.text: bubble.themeColors ? bubble.themeColors.colorText : palette.text
        palette.button: bubble.themeColors ? bubble.themeColors.colorSurfaceRaised : palette.button
        palette.buttonText: bubble.themeColors ? bubble.themeColors.colorText : palette.buttonText
        palette.highlight: bubble.themeColors ? bubble.themeColors.colorSelection : palette.highlight
        palette.highlightedText: bubble.themeColors ? bubble.themeColors.colorText : palette.highlightedText
        palette.placeholderText: bubble.themeColors ? bubble.themeColors.colorTextMuted : palette.placeholderText
        palette.mid: bubble.themeColors ? bubble.themeColors.colorBorder : palette.mid
        palette.dark: bubble.themeColors ? bubble.themeColors.colorBorderStrong : palette.dark
        background: Rectangle {
            objectName: "assistantContextMenu_background"
            color: bubble.themeColors ? bubble.themeColors.colorSurface : bubble.palette.base
            border.width: bubble.themeColors ? bubble.themeColors.borderWidth : 1
            border.color: bubble.themeColors ? bubble.themeColors.colorBorderStrong : bubble.palette.mid
        }
        onAboutToShow: desktop.startWindowListTracking()
        onAboutToHide: desktop.stopWindowListTracking()
        onOpened: {
            const popupWindow = contentItem.Window.window
            if (!popupWindow || popupWindow === bubble)
                return
            Qt.callLater(function() {
                if (!contextMenu.visible || !contextMenu.opened || !bubble.visible)
                    return
                const currentWindow = contextMenu.contentItem.Window.window
                if (currentWindow === popupWindow && currentWindow !== bubble
                        && currentWindow.visible && contextMenu.visible
                        && contextMenu.opened && bubble.visible)
                    currentWindow.requestActivate()
            })
        }
        Menu {
            id: windowListMenu
            objectName: "assistantWindowListMenu"
            title: desktop && desktop.openWindows.length > 0 ? "窗口列表" : "窗口列表（暂无窗口）"
            enabled: desktop && desktop.openWindows.length > 0
            width: Math.max(260, implicitWidth)
            palette.window: bubble.themeColors ? bubble.themeColors.colorWindow : palette.window
            palette.windowText: bubble.themeColors ? bubble.themeColors.colorText : palette.windowText
            palette.base: bubble.themeColors ? bubble.themeColors.colorSurface : palette.base
            palette.alternateBase: bubble.themeColors ? bubble.themeColors.colorSurfaceRaised : palette.alternateBase
            palette.text: bubble.themeColors ? bubble.themeColors.colorText : palette.text
            palette.button: bubble.themeColors ? bubble.themeColors.colorSurfaceRaised : palette.button
            palette.buttonText: bubble.themeColors ? bubble.themeColors.colorText : palette.buttonText
            palette.highlight: bubble.themeColors ? bubble.themeColors.colorSelection : palette.highlight
            palette.highlightedText: bubble.themeColors ? bubble.themeColors.colorText : palette.highlightedText
            palette.placeholderText: bubble.themeColors ? bubble.themeColors.colorTextMuted : palette.placeholderText
            palette.mid: bubble.themeColors ? bubble.themeColors.colorBorder : palette.mid
            palette.dark: bubble.themeColors ? bubble.themeColors.colorBorderStrong : palette.dark
            background: Rectangle {
                objectName: "assistantWindowListMenu_background"
                color: bubble.themeColors ? bubble.themeColors.colorSurface : bubble.palette.base
                border.width: bubble.themeColors ? bubble.themeColors.borderWidth : 1
                border.color: bubble.themeColors ? bubble.themeColors.colorBorderStrong : bubble.palette.mid
            }
            Instantiator {
                model: desktop.openWindows
                delegate: MenuItem {
                    id: windowListItem
                    objectName: "assistantWindowListItem_" + modelData.id
                    required property var modelData
                    property bool closeDispatchPending: false
                    property bool closeButtonPressed: false
                    readonly property var themeColors: bubble.themeColors
                    text: modelData.title
                    font.family: themeColors ? themeColors.fontFamily : font.family
                    font.pointSize: themeColors ? themeColors.fontSizeBody : font.pointSize
                    padding: themeColors ? themeColors.spaceSm : 8
                    function requestWindowClose() {
                        if (closeDispatchPending)
                            return
                        closeDispatchPending = true
                        closeButtonPressed = false
                        desktop.closeWindow(modelData.id)
                        Qt.callLater(function() { windowListItem.closeDispatchPending = false })
                    }
                    onTriggered: {
                        // On Windows, MenuItem receives the release even when the
                        // pointer pressed its child ToolButton. Remember that press and
                        // route this trigger before the popup dismisses.
                        if (closeButtonPressed || closeButton.hovered || closeDispatchPending)
                            windowListItem.requestWindowClose()
                        else
                            desktop.activateWindow(modelData.id)
                    }
                    contentItem: RowLayout {
                        spacing: 8
                        ToolButton {
                            id: closeButton
                            objectName: "assistantCloseWindowButton"
                            text: "×"
                            implicitWidth: 28
                            implicitHeight: 28
                            padding: 2
                            font.family: windowListItem.themeColors
                                        ? windowListItem.themeColors.fontFamily : font.family
                            font.pointSize: windowListItem.themeColors
                                            ? windowListItem.themeColors.fontSizeHeading : font.pointSize
                            palette.button: windowListItem.themeColors
                                            ? windowListItem.themeColors.colorSurfaceRaised : palette.button
                            palette.buttonText: windowListItem.themeColors
                                                ? windowListItem.themeColors.colorText : palette.buttonText
                            palette.text: windowListItem.themeColors
                                          ? windowListItem.themeColors.colorText : palette.text
                            palette.highlight: windowListItem.themeColors
                                               ? windowListItem.themeColors.colorSelection : palette.highlight
                            palette.highlightedText: windowListItem.themeColors
                                                     ? windowListItem.themeColors.colorText : palette.highlightedText
                            Accessible.name: "关闭 " + windowListItem.text
                            onDownChanged: if (down) windowListItem.closeButtonPressed = true
                            onReleased: if (hovered) windowListItem.requestWindowClose()
                            onClicked: windowListItem.requestWindowClose()
                            contentItem: Text {
                                objectName: "assistantCloseWindowButtonLabel"
                                text: closeButton.text
                                font: closeButton.font
                                color: windowListItem.themeColors
                                       ? (!closeButton.enabled
                                          ? windowListItem.themeColors.colorDisabledText
                                          : closeButton.down || closePointerArea.pressed
                                            || (windowListItem.highlighted
                                                && !closeButton.hovered
                                                && !closePointerArea.containsMouse)
                                          ? windowListItem.themeColors.colorTextOnAccent
                                          : windowListItem.themeColors.colorText)
                                       : closeButton.palette.buttonText
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                            background: Rectangle {
                                objectName: "assistantCloseWindowButton_background"
                                color: !closeButton.enabled
                                       ? (windowListItem.themeColors
                                          ? windowListItem.themeColors.colorDisabledSurface : closeButton.palette.button)
                                       : closeButton.down || closePointerArea.pressed
                                         ? (windowListItem.themeColors
                                            ? windowListItem.themeColors.colorAccentPressed : closeButton.palette.button)
                                         : closeButton.hovered || closePointerArea.containsMouse
                                           ? (windowListItem.themeColors
                                              ? windowListItem.themeColors.colorSurfaceRaised : closeButton.palette.button)
                                           : "transparent"
                                border.width: closeButton.activeFocus && windowListItem.themeColors
                                              ? windowListItem.themeColors.focusRingWidth : 0
                                border.color: windowListItem.themeColors
                                             ? windowListItem.themeColors.colorFocusRing : "transparent"
                                radius: windowListItem.themeColors ? windowListItem.themeColors.radiusSm : 3
                            }
                            MouseArea {
                                id: closePointerArea
                                objectName: "assistantCloseWindowPointerArea"
                                anchors.fill: parent
                                z: 1
                                hoverEnabled: true
                                acceptedButtons: Qt.LeftButton
                                preventStealing: true
                                onPressed: function(mouse) {
                                    windowListItem.closeButtonPressed = true
                                    mouse.accepted = true
                                }
                                onReleased: function(mouse) {
                                    const inside = containsMouse
                                    mouse.accepted = true
                                    if (inside)
                                        windowListItem.requestWindowClose()
                                }
                            }
                        }
                        Label {
                            objectName: "assistantWindowListItemLabel"
                            Layout.fillWidth: true
                            text: windowListItem.text
                            font: windowListItem.font
                            color: windowListItem.themeColors
                                   ? (!windowListItem.enabled
                                      ? windowListItem.themeColors.colorDisabledText
                                      : windowListItem.highlighted
                                      ? windowListItem.themeColors.colorTextOnAccent
                                      : windowListItem.themeColors.colorText)
                                   : windowListItem.palette.text
                            elide: Text.ElideRight
                        }
                    }
                    background: Rectangle {
                        objectName: "assistantWindowListItem_background_" + windowListItem.modelData.id
                        color: !windowListItem.enabled
                               ? (windowListItem.themeColors
                                  ? windowListItem.themeColors.colorDisabledSurface : windowListItem.palette.base)
                               : windowListItem.highlighted
                                 ? (windowListItem.themeColors
                                    ? windowListItem.themeColors.colorAccent : windowListItem.palette.highlight)
                                 : windowListItem.hovered
                                   ? (windowListItem.themeColors
                                      ? windowListItem.themeColors.colorSurfaceRaised : windowListItem.palette.base)
                                   : (windowListItem.themeColors
                                      ? windowListItem.themeColors.colorSurface : windowListItem.palette.base)
                        border.width: windowListItem.activeFocus && windowListItem.themeColors
                                      ? windowListItem.themeColors.focusRingWidth : 0
                        border.color: windowListItem.themeColors
                                     ? windowListItem.themeColors.colorFocusRing : "transparent"
                    }
                }
                onObjectAdded: function(index, object) { windowListMenu.insertItem(index, object) }
                onObjectRemoved: function(index, object) { windowListMenu.removeItem(object) }
            }
        }
        MenuItem {
            objectName: "assistantQueueMenuItem"
            text: "查看计算队列"
            onTriggered: desktop.showQueue()
        }
        MenuSeparator { }
        MenuItem {
            objectName: "assistantSoundMenuItem"
            text: "气泡提示音"
            checkable: true
            checked: desktop ? desktop.soundEnabled : false
            onTriggered: desktop.setSoundEnabled(checked)
        }
        MenuSeparator { }
        MenuItem {
            objectName: "assistantCancelExitWaitingItem"
            text: "停止等待退出，保留助手"
            visible: desktop && desktop.applicationExitWaiting
            height: visible ? implicitHeight : 0
            enabled: visible
            onTriggered: desktop.cancelExitWaiting()
        }

        MenuItem {
            objectName: "assistantExitApplicationItem"
            text: "退出应用"
            enabled: desktop && desktop.applicationExitAvailable
            onTriggered: desktop.exitApplication()
        }

        MenuItem {
            objectName: "assistantHideMenuItem"
            text: "隐藏看板娘"
            onTriggered: desktop.hideAssistant()
        }
    }
}
