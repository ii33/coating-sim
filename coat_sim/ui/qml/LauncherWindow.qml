import QtQuick
import QtQuick.Window
import QtQuick.Effects
import QtQuick.Controls
import "."

AppWindow {
    id: window
    titleBase: "光学镀膜"
    captionBase: ""
    statusText: errorToast.visible ? errorText.text : ""
    statusSeverity: errorToast.visible ? "error" : "info"
    onClosing: function(close) {
        close.accepted = false
        if (shutdownConfirm.visible)
            shutdownConfirm.close()
        // The launcher bridge is installed on QQmlContext by the desktop entry.
        // qmllint disable unqualified
        if (launcher) launcher.closeLauncher()
        // qmllint enable unqualified
    }
    useCustomBackground: true
    customBackground: window.themeColors ? window.themeColors.colorWindow : "#e9f3f7"
    initialMaximized: false
    width: Math.min(1380, Math.max(640, Screen.width * 0.92))
    height: Math.min(900, Math.max(480, Screen.height * 0.86))
    minimumWidth: 640
    minimumHeight: 480
    x: Math.round((Screen.width - width) / 2)
    y: Math.round((Screen.height - height) / 2)

    // The project adapter is installed on QQmlContext by the desktop entry.
    // qmllint disable unqualified
    property var projectBridge: typeof launcherProjects !== "undefined" ? launcherProjects : null
    // qmllint enable unqualified
    // The desktop entry injects the launcher router on the root context.
    // qmllint disable unqualified
    readonly property var launcherBridge: typeof launcher !== "undefined" ? launcher : null
    // qmllint enable unqualified
    ShellConfirmDialog {
        id: shutdownConfirm
        objectName: "launcherShutdownConfirm"
        message: "退出程序将丢失项目中尚未保存的修改，以及膜系设计或光追请求中的未保存修改，是否继续？"
        parent: Overlay.overlay
        x: Math.round((parent.width - width) / 2)
        y: Math.round((parent.height - height) / 2)
        onAccepted: if (launcher) launcher.confirmShutdown()
        onRejected: if (launcher) launcher.cancelShutdown()
    }

    function themeColor(name, fallback) {
        return window.themeColors ? Qt.color(window.themeColors[name]) : Qt.color(fallback)
    }
    function tint(colorValue, opacity) {
        const value = Qt.color(colorValue)
        return Qt.rgba(value.r, value.g, value.b, opacity)
    }

    readonly property real unit: height / 720
    readonly property real coverScale: Math.max(
        width / (launcher ? launcher.backgroundWidth : 1),
        height / (launcher ? launcher.backgroundHeight : 1))
    readonly property real coverWidth: Math.max(
        width, Math.ceil((launcher ? launcher.backgroundWidth : 1) * coverScale))
    readonly property real coverHeight: Math.max(
        height, Math.ceil((launcher ? launcher.backgroundHeight : 1) * coverScale))
    readonly property real desiredPanelRight: 0.45 * width
    readonly property real launcherFooterReserve: Math.max(
        36 * window.unit,
        Math.ceil((window.themeColors
                  ? window.themeColors.fontSizeSmall * window.unit
                  : 8.25 * window.unit)
                 + 2 * Math.max(12, 17 * window.unit) + 12))
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
    // Reserve actual text width for six name characters beside the three
    // compact actions. Large fonts may widen only the panel, within this window.
    TextMetrics {
        id: recentNameMetrics
        text: "工程名称六字"
        font.family: window.themeColors ? window.themeColors.fontFamily : ""
        font.pointSize: window.themeColors ? window.themeColors.fontSizeBody : 9
        font.weight: Font.DemiBold
    }
    TextMetrics {
        id: recentActionsMetrics
        text: "复制导出移除"
        font.family: window.themeColors ? window.themeColors.fontFamily : ""
        font.pointSize: window.themeColors ? window.themeColors.fontSizeSmall : 8
        font.weight: Font.DemiBold
    }
    readonly property real recentPanelMinimumWidth: Math.ceil(
        recentNameMetrics.width + recentActionsMetrics.width + 112)
    readonly property real panelWidth: Math.max(1, Math.min(width - panelX - 12,
        Math.max(panelRight - panelX, recentPanelMinimumWidth)))
    readonly property real panelHeight: Math.round(0.85 * height)

    Image {
        id: backdrop
        x: -window.cropX
        y: -window.cropY
        width: window.coverWidth
        height: window.coverHeight
        source: launcher ? launcher.backgroundUrl : ""
        fillMode: Image.Stretch
        asynchronous: true
        cache: true
        smooth: true
        mipmap: true
    }

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
        visible: launcher !== null && !launcher.softwareMode
        source: panelCapture
        autoPaddingEnabled: false
        blurEnabled: launcher !== null && !launcher.softwareMode
        blur: 0.68
        blurMax: 32
        maskEnabled: launcher !== null && !launcher.softwareMode
        maskSource: panelMask
    }

    Rectangle {
        id: glass
        x: window.panelX
        y: window.panelY
        width: window.panelWidth
        height: window.panelHeight
        radius: panelMask.radius
        color: window.tint(window.themeColor("colorSurface", "#f6fbfd"),
                           window.themePaletteMode.indexOf("high-contrast") === 0
                               ? 1.0 : 0.84)
        border.color: window.themeColor("colorBorderStrong", "#9bb3bf")
        border.width: Math.max(1, Math.round(window.unit))
    }

    Item {
        id: content
        x: glass.x + Math.max(Math.round(22 * window.unit),
                              Math.round(0.027 * window.width))
        width: Math.max(1, glass.width - 2 * (x - glass.x))
        y: glass.y
        height: glass.height
        focus: true
        LauncherProjectManager {
            id: projectManager
            objectName: "launcherProjectManager"
            x: 0
            y: 0
            width: content.width
            // Keep the bottom attribution outside the scrollable project controls.
            height: Math.max(1, content.height - window.launcherFooterReserve)
            visible: true
            projectBridge: window.projectBridge
            themeTokens: window.themeColors
            onGlobalActionRequested: function(moduleKey) {
                if (window.launcherBridge
                        && (moduleKey === "settings" || moduleKey === "about" || moduleKey === "global_resources"))
                    window.launcherBridge.openWindow(moduleKey)
            }
        }

    }

    Item {
        id: attribution
        objectName: "launcherDesignedByFooter"
        x: glass.x + Math.max(18, Math.round(20 * window.unit))
        y: glass.y + glass.height - height - Math.max(12, Math.round(17 * window.unit))
        width: Math.max(1, glass.width - 2 * (x - glass.x))
        height: Math.max(authorCredit.implicitHeight, buildCredit.implicitHeight)
        opacity: 0.58
        Text {
            id: authorCredit
            objectName: "launcherAuthorCredit"
            text: "Designed by ii33"
            width: Math.min(implicitWidth, attribution.width)
            anchors.verticalCenter: parent.verticalCenter
            color: window.themeColor("colorTextMuted", "#466473")
            font.pointSize: Math.max(8, window.themeColors ? window.themeColors.fontSizeSmall * window.unit : 8.25 * window.unit)
            elide: Text.ElideRight
        }
        Text {
            id: buildCredit
            objectName: "launcherBuildCredit"
            text: " · Built with OpenAI Codex"
            x: authorCredit.width
            width: Math.max(0, attribution.width - x)
            anchors.verticalCenter: parent.verticalCenter
            color: authorCredit.color
            font.pointSize: authorCredit.font.pointSize * 0.8
            elide: Text.ElideRight
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
        color: window.tint(window.themeColor("colorError", "#b3261e"), window.themeColors ? 0.12 : 1.0)
        border.color: window.themeColor("colorError", "#9c3e3e")
        opacity: 0
        visible: opacity > 0
        Behavior on opacity { NumberAnimation { duration: 170 } }
        Text {
            id: errorText
            anchors.fill: parent
            anchors.margins: 13
            text: ""
            wrapMode: Text.Wrap
            color: window.themeColor("colorError", "#873c3c")
        }
        Timer {
            id: errorTimer
            interval: 15000
            onTriggered: errorToast.opacity = 0
        }
    }
    Connections {
        target: launcher
        function onShutdownConfirmationRequested() {
            if (!shutdownConfirm.visible)
                shutdownConfirm.open()
        }
        function onLaunchError(message) {
            errorText.text = message
            errorToast.opacity = 1
            errorTimer.restart()
        }
    }
}
