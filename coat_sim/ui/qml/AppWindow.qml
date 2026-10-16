import QtQuick
import QtQuick.Window
import "." as LocalComponents

Window {
    id: appWindow
    default property alias pageContent: pageLayer.data
    property bool useCustomBackground: false
    property color customBackground: "transparent"
    property bool initialMaximized: false
    // Pages with their own Settings-based geometry restore can ask the launcher
    // to clamp that rectangle without replacing it with a module default.
    property bool pageManagesWindowGeometry: false
    property string caption: title
    // The page/controller owns status. Chrome only presents the current value.
    property string statusText: ""
    property string statusSeverity: "info"
    property string statusDetail: ""
    readonly property real titleBarHeight: Math.max(36,
        (themeColors ? themeColors.controlHeightSm : 30)
        + 2 * (themeColors ? themeColors.spaceXs : 4))
    readonly property real contentTopMargin: titleBarHeight
    readonly property var themeColors: typeof Theme !== "undefined" ? Theme.snapshot : null
    readonly property var themeAssets: typeof Theme !== "undefined" ? Theme.assets : ({})
    readonly property string themePaletteMode: typeof Theme !== "undefined" ? Theme.mode : "light"
    readonly property string themeBackgroundUrl: !useCustomBackground
        ? String(themeAssets.backgroundUrl || "") : ""
    // Keep built-in Controls and child popups on the same palette as custom
    // QML controls. Pages use ThemeControls wrappers for stable cross-style UI.
    palette.window: themeColors ? themeColors.colorWindow : "#F3F6FA"
    palette.windowText: themeColors ? themeColors.colorText : "#17212B"
    palette.base: themeColors ? themeColors.colorSurface : "#FFFFFF"
    palette.alternateBase: themeColors ? themeColors.colorSurfaceRaised : "#F8FAFC"
    palette.text: themeColors ? themeColors.colorText : "#17212B"
    palette.button: themeColors ? themeColors.colorSurfaceRaised : "#F8FAFC"
    palette.buttonText: themeColors ? themeColors.colorText : "#17212B"
    palette.highlight: themeColors ? themeColors.colorSelection : "#D8EAFE"
    palette.highlightedText: themeColors ? themeColors.colorText : "#17212B"
    palette.placeholderText: themeColors ? themeColors.colorTextMuted : "#526170"
    palette.mid: themeColors ? themeColors.colorBorder : "#D4DDE6"
    palette.dark: themeColors ? themeColors.colorBorderStrong : "#7C8B9A"
    palette.light: themeColors ? themeColors.colorSurface : "#FFFFFF"
    palette.link: themeColors ? themeColors.colorAccent : "#1769AA"
    palette.linkVisited: themeColors ? themeColors.colorAccentPressed : "#0B4A7C"
    palette.brightText: themeColors ? themeColors.colorError : "#B3261E"
    palette.disabled.windowText: themeColors ? themeColors.colorDisabledText : "#687786"
    palette.disabled.text: themeColors ? themeColors.colorDisabledText : "#687786"
    palette.disabled.buttonText: themeColors ? themeColors.colorDisabledText : "#687786"
    palette.disabled.placeholderText: themeColors ? themeColors.colorDisabledText : "#687786"
    palette.disabled.base: themeColors ? themeColors.colorDisabledSurface : "#E7ECF2"
    palette.disabled.alternateBase: themeColors ? themeColors.colorDisabledSurface : "#E7ECF2"
    palette.disabled.button: themeColors ? themeColors.colorDisabledSurface : "#E7ECF2"
    palette.disabled.window: themeColors ? themeColors.colorDisabledSurface : "#E7ECF2"
    // Keep native system-menu and sizing capabilities available to the OS
    // while drawing the title bar and controls in QML.
    flags: Qt.Window | Qt.FramelessWindowHint
         | Qt.WindowSystemMenuHint
         | Qt.WindowMinimizeButtonHint
         | Qt.WindowMaximizeButtonHint
    color: useCustomBackground ? customBackground
                               : themeColors ? themeColors.colorWindow
                                             : (active ? palette.active.window : palette.inactive.window)
    minimumWidth: 720
    minimumHeight: 500
    visible: false

    Item {
        id: pageLayer
        anchors.fill: parent

        Image {
            objectName: "themeBackgroundImage"
            anchors.fill: parent
            z: -1
            visible: appWindow.themeBackgroundUrl.length > 0
            source: appWindow.themeBackgroundUrl
            fillMode: {
                switch (String(appWindow.themeAssets.backgroundMode || "cover")) {
                case "contain": return Image.PreserveAspectFit
                case "stretch": return Image.Stretch
                case "tile": return Image.Tile
                case "center": return Image.Pad
                default: return Image.PreserveAspectCrop
                }
            }
            horizontalAlignment: Image.AlignHCenter
            verticalAlignment: Image.AlignVCenter
            smooth: true
            mipmap: true
        }
    }
    LocalComponents.WindowChrome {
        hostWindow: appWindow
        caption: appWindow.caption
        statusText: appWindow.statusText
        statusSeverity: appWindow.statusSeverity
        statusDetail: appWindow.statusDetail
    }
}
