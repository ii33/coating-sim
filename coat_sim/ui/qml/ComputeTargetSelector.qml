import QtQuick
import QtQuick.Window
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

Item {
    id: selector
    required property var target
    property var registerNativeF4Combo: null
    property bool compact: false
    readonly property var operationBarAction: targetButton
    property var operationBarOverflowAnchor: null
    readonly property var hostWindow: Window.window
    implicitWidth: 150
    implicitHeight: targetButton.implicitHeight

    function indexOfId(rows, value) {
        for (let index = 0; index < rows.length; ++index)
            if (rows[index].id === value) return index
        return -1
    }
    function selectedLabel(rows, value, fallback) {
        const index = indexOfId(rows, value)
        return index < 0 ? fallback : rows[index].label
    }
    function canRestoreFocus(item) {
        if (!item || !item.visible || !item.enabled || item.width <= 0 || item.height <= 0
                || item.focusPolicy === undefined || item.focusPolicy === Qt.NoFocus
                || item.Accessible.ignored)
            return false
        const owner = selector.Window.window
        if (!owner || !owner.visible)
            return false
        const origin = item.mapToItem(owner.contentItem, 0, 0)
        return origin.x >= 0 && origin.y >= 0
                && origin.x + item.width <= owner.width
                && origin.y + item.height <= owner.height
    }
    function restorePopupFocus() {
        // A responsive bar keeps overflowed source controls alive but removes
        // their focus/accessibility. Return to its visible trigger instead.
        const candidates = [selector.operationBarOverflowAnchor, targetButton]
        for (const item of candidates) {
            if (canRestoreFocus(item)) {
                item.forceActiveFocus()
                return
            }
        }
    }
    function summary() {
        if (target.hostId !== "local")
            return target.canSubmit ? "远程 / " + selectedLabel(target.devices, target.deviceId, "设备")
                                    : "远程未就绪"
        const index = indexOfId(target.devices, target.deviceId)
        const device = index < 0 ? null : target.devices[index]
        if (!device) return "本机 / 检查中"
        if (device.backend === "CPU" || device.backend === "cpu") return "本机 / CPU"
        if (device.backend === "GPU" || device.backend === "gpu_cuda") return "本机 / CUDA"
        return "本机 / DirectML"
    }

    ThemeControls.ThemeButton {
        id: targetButton
        objectName: "computeTargetButton"
        anchors.fill: parent
        text: selector.compact ? "仿真设备 ▾" : "仿真设备 · " + selector.summary()
        Accessible.name: "仿真设备，" + selector.summary()
        Accessible.description: selector.target.message
        onClicked: targetPopup.open()
    }
    Popup {
        id: targetPopup
        objectName: "computeTargetPopup"
        parent: Overlay.overlay
        readonly property real edgeMargin: targetButton.themeTokens.spaceSm
        readonly property var ownerWindow: selector.hostWindow
        readonly property real workAreaTop: ownerWindow && ownerWindow.contentTopMargin !== undefined
                                           ? ownerWindow.contentTopMargin + edgeMargin : edgeMargin
        readonly property var anchorItem: selector.operationBarOverflowAnchor || selector
        // Sample after layout, including later placement by an operation bar.
        // mapToItem() in a binding does not track ancestor geometry changes.
        property point anchorPosition: Qt.point(0, 0)
        function updateAnchorPosition() {
            if (parent && anchorItem)
                anchorPosition = anchorItem.mapToItem(parent, 0, 0)
        }
        onAboutToShow: updateAnchorPosition()
        onWorkAreaTopChanged: if (visible) repositionTimer.restart()
        onAnchorItemChanged: if (visible) repositionTimer.restart()
        Timer {
            id: repositionTimer
            interval: 0
            onTriggered: targetPopup.updateAnchorPosition()
        }
        Connections {
            target: targetPopup.ownerWindow
            enabled: targetPopup.visible
            function onWidthChanged() { repositionTimer.restart() }
            function onHeightChanged() { repositionTimer.restart() }
        }
        Connections {
            target: targetPopup.anchorItem
            enabled: targetPopup.visible
            function onXChanged() { repositionTimer.restart() }
            function onYChanged() { repositionTimer.restart() }
            function onWidthChanged() { repositionTimer.restart() }
            function onHeightChanged() { repositionTimer.restart() }
        }
        readonly property real availablePopupHeight: parent
                ? Math.max(0, parent.height - workAreaTop - edgeMargin) : implicitHeight
        width: Math.max(0, Math.min(340, parent ? parent.width - edgeMargin * 2 : 340))
        implicitHeight: popupColumn.implicitHeight + topPadding + bottomPadding
        height: Math.min(implicitHeight, availablePopupHeight)
        padding: Theme.snapshot.spaceMd
        // Consume the outside press so closing the menu cannot also start a run.
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        x: parent ? Math.max(edgeMargin, Math.min(parent.width - width - edgeMargin,
                                                 anchorPosition.x)) : 0
        y: {
            if (!parent) return 0
            const below = anchorPosition.y + anchorItem.height
            const bottomLimit = parent.height - height - edgeMargin
            const preferred = below <= bottomLimit ? below : anchorPosition.y - height
            return Math.max(workAreaTop, Math.min(bottomLimit, preferred))
        }
        onOpened: hostBox.forceActiveFocus()
        onClosed: selector.restorePopupFocus()
        background: Rectangle {
            radius: Theme.snapshot.radiusMd
            color: Theme.snapshot.colorSurfaceRaised
            border.color: Theme.snapshot.colorBorder
            border.width: Theme.snapshot.borderWidth
        }
        contentItem: ScrollView {
            id: popupScroll
            objectName: "computeTargetPopupScroll"
            clip: true
            contentWidth: availableWidth
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ScrollBar.vertical.policy: ScrollBar.AsNeeded
            ColumnLayout {
                id: popupColumn
                width: popupScroll.availableWidth
                spacing: Theme.snapshot.spaceSm
                ThemeControls.ThemeLabel { text: "计算主机" }
                ThemeControls.ThemeComboBox {
                    id: hostBox
                    objectName: "computeHostSelector"
                    property bool nativeF4Popup: true
                    Component.onCompleted: if (selector.registerNativeF4Combo)
                                               selector.registerNativeF4Combo(this)
                    Layout.fillWidth: true
                    accessibleLabel: "计算主机"
                    accessibleDescription: selector.target.message
                    model: selector.target.hosts
                    textRole: "label"
                    valueRole: "id"
                    currentIndex: selector.indexOfId(selector.target.hosts, selector.target.hostId)
                    onActivated: selector.target.setHostId(currentValue)
                }
                ThemeControls.ThemeLabel { text: "计算设备" }
                ThemeControls.ThemeComboBox {
                    id: deviceBox
                    objectName: "computeDeviceSelector"
                    property bool nativeF4Popup: true
                    Component.onCompleted: if (selector.registerNativeF4Combo)
                                               selector.registerNativeF4Combo(this)
                    Layout.fillWidth: true
                    accessibleLabel: "计算设备"
                    accessibleDescription: selector.target.message
                    enabled: selector.target.devices.length > 0
                    model: selector.target.devices
                    textRole: "label"
                    valueRole: "id"
                    currentIndex: selector.indexOfId(selector.target.devices, selector.target.deviceId)
                    onActivated: {
                        const row = selector.target.devices[index]
                        if (row && row.available) selector.target.setDeviceId(row.id)
                        else currentIndex = selector.indexOfId(selector.target.devices, selector.target.deviceId)
                    }
                    delegate: ItemDelegate {
                        required property int index
                        readonly property var row: selector.target.devices[index]
                        width: deviceBox.popup.width
                        enabled: row ? row.available : false
                        text: row ? row.label + (!row.available ? " · 不可用"
                                    : row.support_level === "experimental" ? " · 实验加速" : "") : ""
                        ToolTip.visible: hovered && row && !!(row.reason || row.support_note)
                        ToolTip.text: row ? (row.reason || row.support_note || "") : ""
                    }
                }
                ThemeControls.ThemeLabel {
                    Layout.fillWidth: true
                    text: selector.target.message
                    wrapMode: Text.WordWrap
                    muted: true
                }
                ThemeControls.ThemeLabel {
                    objectName: "computeTargetSupportSummary"
                    Layout.fillWidth: true
                    text: selector.target.supportSummary || ""
                    visible: text.length > 0
                    wrapMode: Text.WordWrap
                    muted: true
                }
                ThemeControls.ThemeButton {
                    text: "重新检查"
                    onClicked: selector.target.refresh()
                }
                ThemeControls.ThemeButton {
                    objectName: "computeCudaProbeButton"
                    visible: selector.target.supportsCudaProbe && selector.target.hostId === "local"
                    text: "检测 NVIDIA CUDA"
                    onClicked: selector.target.requestAppearanceCudaProbe()
                }
            }
        }
    }
}
