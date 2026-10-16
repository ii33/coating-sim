import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: header
    property string title: ""
    property string statusText: ""
    property string statusSeverity: "info"
    property string statusDetail: ""
    property Item tooltipBoundsItem: null

    readonly property var themeTokens: Theme.snapshot
    readonly property string fullStatus: statusDetail.length ? statusDetail : statusText
    readonly property string severityLabel: statusSeverity === "error" ? "错误"
        : statusSeverity === "warning" ? "注意"
        : statusSeverity === "success" ? "完成"
        : statusSeverity === "busy" ? "进行中" : "状态"
    readonly property Item tooltipBounds: tooltipBoundsItem || Overlay.overlay

    function resetStatusTip() {
        Qt.callLater(function() {
            statusControl.tooltipDismissed = false
            const view = statusTipScroll.contentItem
            if (view && typeof view.contentY !== "undefined") view.contentY = 0
        })
    }

    onStatusTextChanged: resetStatusTip()
    onStatusDetailChanged: resetStatusTip()
    onStatusSeverityChanged: resetStatusTip()

    QtObject {
        id: tooltipPositionTracker
        property int revision: 0
        function refresh() { revision += 1 }
    }

    onWidthChanged: tooltipPositionTracker.refresh()
    onHeightChanged: tooltipPositionTracker.refresh()

    implicitWidth: 560
    implicitHeight: visible ? Math.max(38, row.implicitHeight + 2 * themeTokens.spaceXs) : 0
    height: implicitHeight
    visible: title.length > 0 || statusText.length > 0

    RowLayout {
        id: row
        anchors.fill: parent
        anchors.leftMargin: header.themeTokens.spaceLg
        anchors.rightMargin: header.themeTokens.spaceLg
        anchors.topMargin: header.themeTokens.spaceXs
        anchors.bottomMargin: header.themeTokens.spaceXs
        spacing: header.themeTokens.spaceSm

        Text {
            id: titleLabel
            objectName: "themePopupHeaderTitleText"
            Layout.fillWidth: true
            Layout.minimumWidth: header.statusText.length > 0 ? 90 : 0
            Layout.preferredWidth: header.statusText.length > 0 ? row.width * 0.42 : row.width
            text: header.title
            elide: Text.ElideRight
            verticalAlignment: Text.AlignVCenter
            color: header.themeTokens.colorText
            font.family: header.themeTokens.fontFamily
            font.pointSize: header.themeTokens.fontSizeHeading
            font.weight: header.themeTokens.fontWeightEmphasis
            Accessible.role: Accessible.StaticText
            Accessible.name: text
        }

        Control {
            id: statusControl
            objectName: "themePopupHeaderStatus"
            visible: header.statusText.length > 0
            Layout.fillWidth: visible
            Layout.minimumWidth: visible ? 100 : 0
            Layout.preferredWidth: visible ? row.width * 0.58 : 0
            implicitHeight: Math.max(28, header.themeTokens.controlHeightSm)
            activeFocusOnTab: visible
            hoverEnabled: true
            padding: 2
            property bool tooltipDismissed: false

            Accessible.role: Accessible.StaticText
            Accessible.name: header.severityLabel + "：" + header.fullStatus
            Accessible.description: "弹窗状态；悬停或聚焦可读取完整提示，点击后可用方向键和翻页键滚动"

            onHoveredChanged: {
                if (hovered) tooltipPositionTracker.refresh()
                if (!hovered && !activeFocus) tooltipDismissed = false
            }
            onActiveFocusChanged: {
                if (activeFocus) tooltipPositionTracker.refresh()
                if (!activeFocus && !hovered) tooltipDismissed = false
            }
            onVisibleChanged: if (!visible) tooltipDismissed = false
            Keys.onPressed: function(event) {
                const view = statusTipScroll.contentItem
                if (event.key === Qt.Key_Escape) {
                    tooltipDismissed = true
                    statusToolTip.close()
                    event.accepted = true
                    return
                }
                if (!view || typeof view.contentY === "undefined") return
                let delta = 0
                if (event.key === Qt.Key_Down) delta = Math.max(16, view.height / 8)
                else if (event.key === Qt.Key_Up) delta = -Math.max(16, view.height / 8)
                else if (event.key === Qt.Key_PageDown) delta = view.height * 0.8
                else if (event.key === Qt.Key_PageUp) delta = -view.height * 0.8
                else return
                view.contentY = Math.max(0, Math.min(
                    Math.max(0, view.contentHeight - view.height), view.contentY + delta))
                event.accepted = true
            }

            background: Rectangle {
                color: "transparent"
                border.width: statusControl.activeFocus ? header.themeTokens.focusRingWidth : 0
                border.color: header.themeTokens.colorFocusRing
                radius: header.themeTokens.radiusSm
            }

            contentItem: Text {
                objectName: "themePopupHeaderStatusText"
                text: header.severityLabel + "：" + header.statusText
                elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
                font.family: header.themeTokens.fontFamily
                font.pointSize: header.themeTokens.fontSizeBody
                color: header.statusSeverity === "error" ? header.themeTokens.colorError
                    : header.statusSeverity === "warning" ? header.themeTokens.colorWarning
                    : header.themeTokens.colorText
            }

            MouseArea {
                anchors.fill: parent
                acceptedButtons: Qt.LeftButton
                onClicked: statusControl.forceActiveFocus(Qt.MouseFocusReason)
            }

            Popup {
                id: statusToolTip
                objectName: "themePopupHeaderStatusToolTip"
                parent: header.tooltipBounds
                visible: !statusControl.tooltipDismissed
                    && (statusControl.hovered || statusControl.activeFocus)
                closePolicy: Popup.NoAutoClose
                padding: 8
                property string tipText: header.severityLabel + "：" + header.fullStatus
                width: header.tooltipBounds
                    ? Math.max(1, Math.min(620, header.tooltipBounds.width - 16)) : 400
                height: {
                    const revision = tooltipPositionTracker.revision
                    const tipHeight = statusTipText.implicitHeight + 2 * padding
                    const bounds = header.tooltipBounds
                    if (!bounds) return Math.min(tipHeight, 640)
                    const point = statusControl.mapToItem(bounds, 0, 0)
                    const below = point.y + statusControl.height + 4
                    return Math.min(tipHeight, Math.max(1, bounds.height - below - 8))
                }
                x: {
                    const revision = tooltipPositionTracker.revision
                    const bounds = header.tooltipBounds
                    if (!bounds) return 8
                    const point = statusControl.mapToItem(bounds, 0, 0)
                    return Math.max(8, Math.min(point.x, bounds.width - width - 8))
                }
                y: {
                    const revision = tooltipPositionTracker.revision
                    const bounds = header.tooltipBounds
                    if (!bounds) return 8
                    const point = statusControl.mapToItem(bounds, 0, 0)
                    const below = point.y + statusControl.height + 4
                    return Math.max(8, below)
                }
                contentItem: ScrollView {
                    id: statusTipScroll
                    objectName: "themePopupHeaderStatusTipScroll"
                    clip: true
                    contentWidth: availableWidth
                    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                    Text {
                        id: statusTipText
                        objectName: "themePopupHeaderStatusTipFullText"
                        width: statusTipScroll.availableWidth
                        text: statusToolTip.tipText
                        color: header.themeTokens.colorText
                        font.family: header.themeTokens.fontFamily
                        font.pointSize: header.themeTokens.fontSizeBody
                        wrapMode: Text.WrapAnywhere
                    }
                }
                background: Rectangle {
                    color: header.themeTokens.colorSurfaceRaised
                    border.color: header.themeTokens.colorBorderStrong
                    radius: header.themeTokens.radiusSm
                }
            }
        }
    }
}
