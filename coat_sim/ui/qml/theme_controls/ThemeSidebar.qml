import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "." as ThemeControls

Pane {
    id: sidebar
    default property alias body: contentColumn.data
    property string title: ""
    property real sidebarWidth: 320
    readonly property var tokens: Theme.snapshot
    objectName: "themeSidebar"
    implicitWidth: sidebarWidth
    padding: tokens.spaceSm
    background: Rectangle {
        color: sidebar.tokens.colorSurface
        border.color: sidebar.tokens.colorBorder
        radius: sidebar.tokens.radiusSm
    }
    contentItem: ColumnLayout {
        spacing: sidebar.tokens.spaceSm
        ThemeControls.ThemeLabel {
            visible: sidebar.title.length > 0
            text: sidebar.title
            heading: true
            Layout.fillWidth: true
            wrapMode: Text.WrapAnywhere
        }
        ScrollView {
            id: scroll
            objectName: "themeSidebarScroll"
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: availableWidth
            clip: true
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ColumnLayout {
                id: contentColumn
                objectName: "themeSidebarContent"
                width: scroll.availableWidth
                spacing: sidebar.tokens.spaceSm
            }
        }
    }
}
