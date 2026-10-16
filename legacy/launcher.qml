import QtQuick
import QtQuick.Window
import QtQuick.Effects

Window {
    id: window
    title: "行星盘沉积仿真"
    visible: false
    flags: Qt.Window | Qt.FramelessWindowHint
    color: "#e9f3f7"
    minimumWidth: 640
    minimumHeight: 480

    readonly property real unit: height / 720
    readonly property real coverScale: Math.max(
        width / launcher.backgroundWidth, height / launcher.backgroundHeight)
    readonly property real coverWidth: Math.max(
        width, Math.ceil(launcher.backgroundWidth * coverScale))
    readonly property real coverHeight: Math.max(
        height, Math.ceil(launcher.backgroundHeight * coverScale))
    readonly property real desiredPanelRight: 0.45 * width
    readonly property real handGap: Math.max(0.04 * width, 25 * unit)
    readonly property real cropX: Math.max(0, Math.min(
        coverWidth - width,
        0.77 * (coverWidth - width),
        0.515 * coverWidth - desiredPanelRight - handGap))
    readonly property real cropY: Math.max(0, Math.min(
        coverHeight - height, 0.39 * coverHeight - 0.39 * height))
    readonly property real fingertipX: 0.515 * coverWidth - cropX
    readonly property real panelX: Math.round(0.045 * width)
    readonly property real panelY: Math.round(0.08 * height)
    readonly property real panelRight: Math.min(
        width - 12, fingertipX - handGap,
        Math.max(panelX + 0.30 * width, desiredPanelRight))
    readonly property real panelWidth: panelRight - panelX
    readonly property real panelHeight: Math.round(0.85 * height)

    Component.onCompleted: {
        var sw = Screen.width
        var sh = Screen.height
        width = Math.min(1380, Math.max(640, Math.round(sw * 0.82)))
        height = Math.min(900, Math.max(480, Math.round(sh * 0.82)))
        x = Math.round((sw - width) / 2)
        y = Math.round((sh - height) / 2)
        showFullScreen()
    }

    function toggleFullscreen() {
        if (visibility === Window.FullScreen)
            showNormal()
        else
            showFullScreen()
    }

    function toggleMaximized() {
        if (visibility === Window.Maximized)
            showNormal()
        else
            showMaximized()
    }

    Shortcut {
        sequence: "F11"
        onActivated: window.toggleFullscreen()
    }
    Shortcut {
        sequence: "Escape"
        onActivated: {
            if (window.visibility === Window.FullScreen)
                window.showNormal()
        }
    }

    // The entire cover image is retained as one texture in the Qt Quick scene.
    Image {
        id: backdrop
        x: -window.cropX
        y: -window.cropY
        width: window.coverWidth
        height: window.coverHeight
        source: launcher.backgroundUrl
        fillMode: Image.Stretch
        asynchronous: true
        cache: true
        smooth: true
        mipmap: true
    }

    // Capture only the panel's part of the backdrop at reduced resolution.
    // MultiEffect blurs this texture in Qt Quick's GPU rendering pipeline.
    ShaderEffectSource {
        id: panelCapture
        x: window.panelX
        y: window.panelY
        width: window.panelWidth
        height: window.panelHeight
        sourceItem: backdrop
        sourceRect: Qt.rect(window.panelX - backdrop.x,
                            window.panelY - backdrop.y,
                            window.panelWidth, window.panelHeight)
        textureSize: Qt.size(Math.max(1, Math.round(width * 0.65)),
                             Math.max(1, Math.round(height * 0.65)))
        smooth: true
        visible: false
    }

    Rectangle {
        id: panelMask
        width: window.panelWidth
        height: window.panelHeight
        radius: Math.max(14, Math.round(19 * window.unit))
        color: "white"
        layer.enabled: true
        visible: false
    }

    MultiEffect {
        x: window.panelX
        y: window.panelY
        width: window.panelWidth
        height: window.panelHeight
        visible: !launcher.softwareMode
        source: panelCapture
        autoPaddingEnabled: false
        blurEnabled: !launcher.softwareMode
        blur: 0.68
        blurMax: 32
        maskEnabled: !launcher.softwareMode
        maskSource: panelMask
    }

    Rectangle {
        id: glass
        x: window.panelX
        y: window.panelY
        width: window.panelWidth
        height: window.panelHeight
        radius: panelMask.radius
        color: Qt.rgba(0.965, 0.986, 0.998, 0.70)
        border.color: Qt.rgba(1, 1, 1, 0.85)
        border.width: Math.max(1, Math.round(window.unit))
    }

    Item {
        id: content
        x: glass.x + Math.max(Math.round(22 * window.unit),
                              Math.round(0.027 * window.width))
        width: Math.max(1, glass.width - 2 * (x - glass.x))
        y: glass.y
        height: glass.height
        readonly property real rowHeight: 55 * window.unit
        readonly property real rowGap: 9 * window.unit

        Text {
            x: 0
            y: 54 * window.unit
            text: "行星盘沉积仿真"
            color: "#203b4e"
            font.family: "Microsoft YaHei UI"
            font.bold: true
            font.pixelSize: Math.min(37 * window.unit,
                                     Math.max(18, content.width / 9.5))
        }
        Text {
            x: 0
            y: 105 * window.unit
            text: "选择模块开始"
            color: "#567384"
            font.family: "Microsoft YaHei UI"
            font.pixelSize: Math.max(15, 16 * window.unit)
        }

        Column {
            id: moduleColumn
            x: 0
            y: 146 * window.unit
            width: content.width
            spacing: content.rowGap

            Repeater {
                model: launcher.modules
                delegate: Item {
                    id: moduleRow
                    required property int index
                    required property var modelData
                    width: moduleColumn.width
                    height: content.rowHeight

                    Rectangle {
                        id: halo
                        anchors.fill: parent
                        anchors.margins: -3 * window.unit
                        radius: 13 * window.unit
                        color: "transparent"
                        border.color: "#a1cfdb"
                        border.width: Math.max(1, window.unit)
                        opacity: hit.containsMouse ? 0.92 : 0
                        Behavior on opacity { NumberAnimation { duration: 170 } }
                    }
                    Rectangle {
                        id: outline
                        anchors.fill: parent
                        radius: 10 * window.unit
                        color: hit.containsMouse ? "#36ffffff" : "#12ffffff"
                        border.color: hit.containsMouse ? "#148db2" : "#708f9d"
                        border.width: Math.max(1, Math.round(
                            window.unit * (hit.containsMouse ? 2 : 1)))
                        scale: hit.pressed ? 0.985 : hit.containsMouse ? 1.012 : 1
                        Behavior on color { ColorAnimation { duration: 160 } }
                        Behavior on scale { NumberAnimation { duration: 120 } }
                    }
                    Text {
                        x: Math.round(19 * window.unit)
                        anchors.verticalCenter: parent.verticalCenter
                        text: moduleRow.modelData.title
                        color: hit.containsMouse ? "#086785" : "#244757"
                        font.family: "Microsoft YaHei UI"
                        font.bold: true
                        font.pixelSize: Math.max(18, 22 * window.unit)
                        Behavior on color { ColorAnimation { duration: 160 } }
                    }
                    Text {
                        x: Math.round(159 * window.unit)
                        anchors.verticalCenter: parent.verticalCenter
                        text: moduleRow.modelData.detail
                        visible: content.width >= 420 * window.unit
                        color: hit.containsMouse ? "#278ca8" : "#6a8795"
                        font.family: "Microsoft YaHei UI"
                        font.pixelSize: Math.max(13, 14 * window.unit)
                        Behavior on color { ColorAnimation { duration: 160 } }
                    }
                    MouseArea {
                        id: hit
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: launcher.launch(moduleRow.index)
                    }
                }
            }
        }

        Text {
            x: 0
            y: content.height - 34 * window.unit
            text: "F11 切换全屏"
            color: "#6c8a9a"
            font.family: "Microsoft YaHei UI"
            font.pixelSize: Math.max(13, 14 * window.unit)
        }
    }

    // Frameless controls remain visible both in full screen and normal mode.
    Row {
        id: chrome
        z: 3
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.rightMargin: Math.round(18 * window.unit)
        anchors.topMargin: Math.round(18 * window.unit)
        spacing: Math.round(8 * window.unit)
        Repeater {
            model: ["−", "□", "×"]
            delegate: Rectangle {
                id: control
                required property int index
                required property string modelData
                width: Math.max(30, 42 * window.unit)
                height: width
                radius: 9 * window.unit
                color: controlHit.containsMouse ? "#b8f6fbfd" : "#78f6fbfd"
                border.color: controlHit.containsMouse ? "#148db2" : "#6b8a99"
                border.width: Math.max(1, Math.round(window.unit))
                Behavior on color { ColorAnimation { duration: 130 } }
                Text {
                    anchors.centerIn: parent
                    text: control.modelData
                    color: "#315365"
                    font.family: "Microsoft YaHei UI"
                    font.pixelSize: Math.max(19, 24 * window.unit)
                }
                MouseArea {
                    id: controlHit
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        if (control.index === 0)
                            window.showMinimized()
                        else if (control.index === 1) {
                            if (window.visibility === Window.FullScreen)
                                window.showNormal()
                            else
                                window.toggleMaximized()
                        } else
                            window.close()
                    }
                }
            }
        }
    }

    // Drag the blank top edge in normal mode. Double-click toggles maximize.
    MouseArea {
        id: titleDrag
        z: 2
        x: 0
        y: 0
        width: Math.max(0, chrome.x - 8 * window.unit)
        height: Math.max(40 * window.unit, chrome.height + 12 * window.unit)
        acceptedButtons: Qt.LeftButton
        cursorShape: Qt.ArrowCursor
        property real pressX: 0
        property real pressY: 0
        property bool moveStarted: false
        onPressed: function(mouse) {
            pressX = mouse.x
            pressY = mouse.y
            moveStarted = false
        }
        onPositionChanged: function(mouse) {
            if (pressed && !moveStarted && window.visibility === Window.Windowed &&
                    (Math.abs(mouse.x - pressX) + Math.abs(mouse.y - pressY) > 5)) {
                moveStarted = true
                window.startSystemMove()
            }
        }
        onDoubleClicked: {
            if (window.visibility === Window.FullScreen)
                window.showNormal()
            else
                window.toggleMaximized()
        }
    }

    // A frameless window has no native sizing border. Delegate each edge and
    // corner to the window manager so ordinary snap and resize behavior works.
    Item {
        id: resizeHandles
        z: 5
        anchors.fill: parent
        visible: window.visibility === Window.Windowed
        readonly property int grip: Math.max(6, Math.round(7 * window.unit))

        MouseArea {
            x: resizeHandles.grip; y: 0
            width: resizeHandles.width - 2 * resizeHandles.grip
            height: resizeHandles.grip
            cursorShape: Qt.SizeVerCursor
            onPressed: window.startSystemResize(Qt.TopEdge)
        }
        MouseArea {
            x: resizeHandles.grip
            y: resizeHandles.height - resizeHandles.grip
            width: resizeHandles.width - 2 * resizeHandles.grip
            height: resizeHandles.grip
            cursorShape: Qt.SizeVerCursor
            onPressed: window.startSystemResize(Qt.BottomEdge)
        }
        MouseArea {
            x: 0; y: resizeHandles.grip
            width: resizeHandles.grip
            height: resizeHandles.height - 2 * resizeHandles.grip
            cursorShape: Qt.SizeHorCursor
            onPressed: window.startSystemResize(Qt.LeftEdge)
        }
        MouseArea {
            x: resizeHandles.width - resizeHandles.grip
            y: resizeHandles.grip
            width: resizeHandles.grip
            height: resizeHandles.height - 2 * resizeHandles.grip
            cursorShape: Qt.SizeHorCursor
            onPressed: window.startSystemResize(Qt.RightEdge)
        }
        MouseArea {
            x: 0; y: 0
            width: resizeHandles.grip; height: resizeHandles.grip
            cursorShape: Qt.SizeFDiagCursor
            onPressed: window.startSystemResize(Qt.LeftEdge | Qt.TopEdge)
        }
        MouseArea {
            x: resizeHandles.width - resizeHandles.grip; y: 0
            width: resizeHandles.grip; height: resizeHandles.grip
            cursorShape: Qt.SizeBDiagCursor
            onPressed: window.startSystemResize(Qt.RightEdge | Qt.TopEdge)
        }
        MouseArea {
            x: 0; y: resizeHandles.height - resizeHandles.grip
            width: resizeHandles.grip; height: resizeHandles.grip
            cursorShape: Qt.SizeBDiagCursor
            onPressed: window.startSystemResize(Qt.LeftEdge | Qt.BottomEdge)
        }
        MouseArea {
            x: resizeHandles.width - resizeHandles.grip
            y: resizeHandles.height - resizeHandles.grip
            width: resizeHandles.grip; height: resizeHandles.grip
            cursorShape: Qt.SizeFDiagCursor
            onPressed: window.startSystemResize(Qt.RightEdge | Qt.BottomEdge)
        }
    }

    Rectangle {
        id: errorToast
        z: 4
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 24 * window.unit
        width: Math.min(window.width - 32, Math.max(360, errorText.implicitWidth + 40))
        height: Math.max(55, errorText.implicitHeight + 26)
        radius: 11
        color: "#eaf8fc"
        border.color: "#9c3e3e"
        opacity: 0
        visible: opacity > 0
        Behavior on opacity { NumberAnimation { duration: 170 } }
        Text {
            id: errorText
            anchors.fill: parent
            anchors.margins: 13
            text: ""
            wrapMode: Text.Wrap
            color: "#802a2a"
            font.family: "Microsoft YaHei UI"
            font.pixelSize: 15
            verticalAlignment: Text.AlignVCenter
        }
        Timer {
            id: hideError
            interval: 5500
            onTriggered: errorToast.opacity = 0
        }
    }
    Connections {
        target: launcher
        function onLaunchError(message) {
            errorText.text = message
            errorToast.opacity = 1
            hideError.restart()
        }
    }
}
