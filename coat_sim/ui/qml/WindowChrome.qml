import QtQuick
import QtQuick.Window
import QtQuick.Controls
import "theme_controls" as ThemeControls

Item {
    id: chrome
    property var hostWindow
    property string caption: hostWindow ? hostWindow.title : ""
    property string statusText: ""
    property string statusSeverity: "info"
    property string statusDetail: ""
    readonly property string fullStatus: statusDetail.length ? statusDetail : statusText
    readonly property string severityLabel: statusSeverity === "error" ? "错误"
        : statusSeverity === "warning" ? "注意" : statusSeverity === "success" ? "完成"
        : statusSeverity === "busy" ? "进行中" : "状态"
    readonly property real barHeight: hostWindow && typeof hostWindow.titleBarHeight !== "undefined"
        ? hostWindow.titleBarHeight : Math.max(36,
            (themeColors ? themeColors.controlHeightSm : 30) + 2 * (themeColors ? themeColors.spaceXs : 4))
    readonly property real leftInset: themeColors ? themeColors.spaceLg : 20
    readonly property real textGap: themeColors ? themeColors.spaceSm : 8
    readonly property real usableTextWidth: Math.max(0, controls.x - leftInset - textGap)
    readonly property real captionWidth: statusText.length
        ? Math.min(330, Math.max(120, usableTextWidth * 0.42)) : Math.min(500, usableTextWidth)
    readonly property var themeColors: typeof Theme !== "undefined" ? Theme.snapshot : null
    readonly property var buttonObjectNames: ["windowChromeMinimizeButton",
                                               "windowChromeMaximizeButton",
                                               "windowChromeCloseButton"]
    readonly property bool lightBackdrop: hostWindow && hostWindow.useCustomBackground
    readonly property color ink: themeColors ? themeColors.colorText
                                  : lightBackdrop ? "#183b53" : hostWindow
                                  ? (hostWindow.active ? hostWindow.palette.active.windowText
                                                       : hostWindow.palette.inactive.windowText)
                                  : "#202020"
    readonly property real controlsTopMargin: (themeColors ? themeColors.spaceXs : 5)
                                              + (hostWindow && hostWindow.visibility === Window.Maximized ? 4 : 0)
    anchors.fill: parent
    z: 100

    Text {
        id: captionLabel
        objectName: "windowChromeCaption"
        x: chrome.leftInset
        y: (chrome.barHeight - height) / 2
        width: Math.min(chrome.usableTextWidth, chrome.captionWidth)
        text: chrome.caption
        textFormat: Text.PlainText
        elide: Text.ElideRight
        color: chrome.ink
        font.pointSize: chrome.themeColors ? chrome.themeColors.fontSizeBody : 9
        font.family: chrome.themeColors ? chrome.themeColors.fontFamily : "sans-serif"
        font.weight: chrome.themeColors ? chrome.themeColors.fontWeightEmphasis : Font.Bold
        Accessible.role: Accessible.StaticText
        Accessible.name: text
    }
    MouseArea {
        id: titleDrag
        objectName: "windowChromeTitleDrag"
        x: 8
        y: 5
        width: Math.max(0, chrome.width - controls.width - 32)
        height: Math.max(0, chrome.barHeight - y)
        acceptedButtons: Qt.LeftButton
        hoverEnabled: true
        cursorShape: Qt.ArrowCursor
        property real pressedX: 0
        property real pressedY: 0
        property bool moving: false
        property bool fallbackMove: false
        property real moveStartGlobalX: 0
        property real moveStartGlobalY: 0
        property real moveStartWindowX: 0
        property real moveStartWindowY: 0
        onPressed: function(mouse) {
            pressedX = mouse.x
            pressedY = mouse.y
            moving = false
            fallbackMove = false
        }
        onPositionChanged: function(mouse) {
            if (!pressed) return
            const globalPoint = titleDrag.mapToGlobal(Qt.point(mouse.x, mouse.y))
            if (!moving && Math.abs(mouse.x - pressedX) + Math.abs(mouse.y - pressedY) > 5) {
                if (hostWindow.visibility === Window.Maximized ||
                        hostWindow.visibility === Window.FullScreen) {
                    hostWindow.showNormal()
                    hostWindow.x = Math.round(globalPoint.x - titleDrag.x - pressedX)
                    hostWindow.y = Math.round(globalPoint.y - titleDrag.y - pressedY)
                }
                moving = true
                fallbackMove = !hostWindow.startSystemMove()
                moveStartGlobalX = globalPoint.x
                moveStartGlobalY = globalPoint.y
                moveStartWindowX = hostWindow.x
                moveStartWindowY = hostWindow.y
            } else if (moving && fallbackMove) {
                hostWindow.x = Math.round(moveStartWindowX + globalPoint.x - moveStartGlobalX)
                hostWindow.y = Math.round(moveStartWindowY + globalPoint.y - moveStartGlobalY)
            }
        }
        onReleased: {
            // Let the native move loop own edge snapping whenever it is
            // supported. The manual top-edge behavior is only a fallback;
            // applying it after a native side snap would expand that snap.
            if (moving && fallbackMove && hostWindow.visibility === Window.Windowed) {
                const top = hostWindow.screen ? hostWindow.screen.availableGeometry.y : 0
                if (hostWindow.y <= top + 12) hostWindow.showMaximized()
            }
            moving = false
            fallbackMove = false
        }
        onDoubleClicked: {
            if (hostWindow.visibility === Window.FullScreen ||
                    hostWindow.visibility === Window.Maximized)
                hostWindow.showNormal()
            else
                hostWindow.showMaximized()
        }
        ToolTip {
            id: captionToolTip
            visible: titleDrag.containsMouse && !titleDrag.pressed
                && titleDrag.mouseX < captionLabel.x + captionLabel.width - titleDrag.x
            text: chrome.caption
            closePolicy: Popup.NoAutoClose
            width: Math.min(620, Math.max(240, chrome.width - 40))
            height: Math.min(captionTipText.implicitHeight + topPadding + bottomPadding,
                Math.max(80, (hostWindow ? hostWindow.height : 640) - chrome.barHeight - 12))
            x: chrome.leftInset - titleDrag.x
            y: titleDrag.height + 4
            contentItem: ScrollView {
                id: captionTipScroll
                clip: true
                contentWidth: availableWidth
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                Text {
                    id: captionTipText
                    width: captionTipScroll.availableWidth
                    text: captionToolTip.text
                    textFormat: Text.PlainText
                    color: chrome.themeColors ? chrome.themeColors.colorText : "#17212b"
                    font.pointSize: chrome.themeColors ? chrome.themeColors.fontSizeBody : 9
                    font.family: chrome.themeColors ? chrome.themeColors.fontFamily : "sans-serif"
                    wrapMode: Text.WrapAnywhere
                }
            }
            background: Rectangle {
                color: chrome.themeColors ? chrome.themeColors.colorSurfaceRaised : "#f8fafc"
                border.color: chrome.themeColors ? chrome.themeColors.colorBorderStrong : "#7c8b9a"
                radius: chrome.themeColors ? chrome.themeColors.radiusSm : 4
            }
        }
    }

    // Status is deliberately presentation-only. It accepts no input, so
    // pointer presses over the text fall through to the title drag handler.
    Item {
        id: statusControl
        objectName: "windowChromeStatus"
        visible: chrome.statusText.length > 0
        x: captionLabel.x + captionLabel.width + chrome.textGap
        y: chrome.controlsTopMargin
        width: Math.max(0, controls.x - x - chrome.textGap)
        height: Math.max(0, chrome.barHeight - y - (chrome.themeColors ? chrome.themeColors.spaceXs : 4))
        z: 1
        activeFocusOnTab: false
        focus: false
        Accessible.role: Accessible.StaticText
        Accessible.name: chrome.severityLabel + "：" + chrome.fullStatus
        Accessible.description: "窗口当前状态"
        Text {
            objectName: "windowChromeStatusText"
            anchors.fill: parent
            Accessible.ignored: true
            text: chrome.severityLabel + "：" + chrome.statusText
            elide: Text.ElideRight
            verticalAlignment: Text.AlignVCenter
            font.pointSize: chrome.themeColors ? chrome.themeColors.fontSizeBody : 9
            font.family: chrome.themeColors ? chrome.themeColors.fontFamily : "sans-serif"
            color: chrome.statusSeverity === "error" && chrome.themeColors ? chrome.themeColors.colorError
                : chrome.statusSeverity === "warning" && chrome.themeColors ? chrome.themeColors.colorWarning
                : chrome.ink
        }
    }

    Row {
        id: controls
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: chrome.controlsTopMargin
        anchors.rightMargin: chrome.themeColors ? chrome.themeColors.spaceLg : 16
        spacing: chrome.themeColors ? chrome.themeColors.spaceXs : 7
        z: 2
        Repeater {
            model: ["−", "□", "×"]
            delegate: ThemeControls.ThemeButton {
                required property int index
                required property string modelData
                objectName: chrome.buttonObjectNames[index]
                text: modelData
                width: chrome.themeColors ? chrome.themeColors.controlHeightSm : 30
                height: chrome.themeColors ? chrome.themeColors.controlHeightSm : 30
                flat: true
                topPadding: 0
                bottomPadding: 0
                leftPadding: 0
                rightPadding: 0
                font.pointSize: chrome.themeColors ? chrome.themeColors.fontSizeHeading : 15
                Accessible.name: index === 0 ? "最小化"
                                 : index === 1 ? "最大化或还原" : "关闭"
                enabled: hostWindow !== null
                onClicked: {
                    if (index === 0)
                        hostWindow.showMinimized()
                    else if (index === 1) {
                        if (hostWindow.visibility === Window.FullScreen ||
                                hostWindow.visibility === Window.Maximized)
                            hostWindow.showNormal()
                        else
                            hostWindow.showMaximized()
                    } else {
                        hostWindow.close()
                    }
                }
            }
        }
    }

    // Native system sizing keeps Windows snap behavior on a frameless window.
    Item {
        id: resizeEdges
        anchors.fill: parent
        z: 3
        visible: hostWindow.visibility === Window.Windowed
        readonly property int grip: 7
        MouseArea {
            x: resizeEdges.grip; y: 0
            width: Math.max(0, resizeEdges.width - 2 * resizeEdges.grip)
            height: resizeEdges.grip
            cursorShape: Qt.SizeVerCursor
            onPressed: hostWindow.startSystemResize(Qt.TopEdge)
        }
        MouseArea {
            x: resizeEdges.grip; y: resizeEdges.height - resizeEdges.grip
            width: Math.max(0, resizeEdges.width - 2 * resizeEdges.grip)
            height: resizeEdges.grip
            cursorShape: Qt.SizeVerCursor
            onPressed: hostWindow.startSystemResize(Qt.BottomEdge)
        }
        MouseArea {
            x: 0; y: resizeEdges.grip
            width: resizeEdges.grip
            height: Math.max(0, resizeEdges.height - 2 * resizeEdges.grip)
            cursorShape: Qt.SizeHorCursor
            onPressed: hostWindow.startSystemResize(Qt.LeftEdge)
        }
        MouseArea {
            x: resizeEdges.width - resizeEdges.grip; y: resizeEdges.grip
            width: resizeEdges.grip
            height: Math.max(0, resizeEdges.height - 2 * resizeEdges.grip)
            cursorShape: Qt.SizeHorCursor
            onPressed: hostWindow.startSystemResize(Qt.RightEdge)
        }
        MouseArea {
            x: 0; y: 0
            width: resizeEdges.grip; height: resizeEdges.grip
            cursorShape: Qt.SizeFDiagCursor
            onPressed: hostWindow.startSystemResize(Qt.LeftEdge | Qt.TopEdge)
        }
        MouseArea {
            x: resizeEdges.width - resizeEdges.grip; y: 0
            width: resizeEdges.grip; height: resizeEdges.grip
            cursorShape: Qt.SizeBDiagCursor
            onPressed: hostWindow.startSystemResize(Qt.RightEdge | Qt.TopEdge)
        }
        MouseArea {
            x: 0; y: resizeEdges.height - resizeEdges.grip
            width: resizeEdges.grip; height: resizeEdges.grip
            cursorShape: Qt.SizeBDiagCursor
            onPressed: hostWindow.startSystemResize(Qt.LeftEdge | Qt.BottomEdge)
        }
        MouseArea {
            x: resizeEdges.width - resizeEdges.grip
            y: resizeEdges.height - resizeEdges.grip
            width: resizeEdges.grip; height: resizeEdges.grip
            cursorShape: Qt.SizeFDiagCursor
            onPressed: hostWindow.startSystemResize(Qt.RightEdge | Qt.BottomEdge)
        }
    }
}
