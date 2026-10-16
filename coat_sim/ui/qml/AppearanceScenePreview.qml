import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls
import CoatUi 1.0
import "."

Item {
    id: root
    required property var controller
    property var sceneData: controller.previewScene
    readonly property real aspectRatio: {
        const value = Number(sceneData.aspect_ratio)
        return Number.isFinite(value) && value > 0 ? value : 4 / 3
    }
    readonly property bool sceneSupported: Boolean(sceneData.supported)
    readonly property bool cameraValid: Boolean(sceneData.camera && sceneData.camera.valid)

    Rectangle {
        anchors.fill: parent
        color: Theme.snapshot.colorWindow
        border.color: Theme.snapshot.colorBorder
        border.width: Theme.snapshot.borderWidth
        radius: Theme.snapshot.radiusSm
    }

    Item {
        id: viewFrame
        objectName: "appearanceSceneFrame"
        anchors.centerIn: parent
        width: Math.min(root.width, root.height * root.aspectRatio)
        height: root.aspectRatio > 0 ? width / root.aspectRatio : root.height
        visible: root.sceneSupported && root.cameraValid
        clip: true

        AppearanceSceneCanvas {
            id: sceneCanvas
            objectName: "appearanceSceneCanvas"
            anchors.fill: parent
            sceneData: root.sceneData
            surfaceColor: Theme.snapshot.colorSurface
            gridColor: Theme.snapshot.colorBorder
        }

        MouseArea {
            id: cameraMouseArea
            objectName: "appearanceCameraMouseArea"
            anchors.fill: parent
            enabled: root.sceneSupported && root.cameraValid
            acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton
            hoverEnabled: true
            property real lastX: 0
            property real lastY: 0
            property int dragButton: Qt.NoButton

            onPressed: function(mouse) {
                lastX = mouse.x
                lastY = mouse.y
                dragButton = mouse.button
                root.controller.beginCameraGesture()
            }
            onPositionChanged: function(mouse) {
                if (dragButton === Qt.NoButton)
                    return
                const dx = mouse.x - lastX
                const dy = mouse.y - lastY
                const pan = dragButton !== Qt.LeftButton || (mouse.modifiers & Qt.ControlModifier)
                if (pan)
                    root.controller.panCamera(dx, dy, height)
                else
                    root.controller.orbitCamera(dx, dy)
                lastX = mouse.x
                lastY = mouse.y
            }
            onReleased: function(mouse) {
                dragButton = Qt.NoButton
                root.controller.commitCameraGesture()
            }
            onCanceled: {
                dragButton = Qt.NoButton
                root.controller.commitCameraGesture()
            }
            onWheel: function(wheel) {
                root.controller.beginCameraGesture()
                const pixelX = Number(wheel.pixelDelta.x)
                const pixelY = Number(wheel.pixelDelta.y)
                const deltaX = pixelX !== 0 ? pixelX : Number(wheel.angleDelta.x) / 8.0
                const deltaY = pixelY !== 0 ? pixelY : Number(wheel.angleDelta.y) / 8.0
                if (deltaX !== 0)
                    root.controller.panCamera(deltaX, 0, height)
                if (deltaY !== 0)
                    root.controller.zoomCamera(Math.exp(-deltaY * 0.0015))
                wheelCommit.restart()
                wheel.accepted = true
            }
        }

        PinchHandler {
            id: cameraPinchHandler
            target: null
            enabled: root.sceneSupported && root.cameraValid
            property real previousScale: 1.0
            property point previousTranslation: Qt.point(0, 0)
            onActiveChanged: {
                if (active) {
                    previousScale = 1.0
                    previousTranslation = Qt.point(0, 0)
                    root.controller.beginCameraGesture()
                } else {
                    root.controller.commitCameraGesture()
                }
            }
            onTranslationChanged: function(value) {
                const dx = Number(value.x) - previousTranslation.x
                const dy = Number(value.y) - previousTranslation.y
                if (dx !== 0 || dy !== 0)
                    root.controller.panCamera(dx, dy, viewFrame.height)
                previousTranslation = Qt.point(Number(value.x), Number(value.y))
            }
            onScaleChanged: {
                const currentScale = Math.max(0.01, Number(scale))
                const factor = previousScale / currentScale
                if (Number.isFinite(factor) && factor > 0)
                    root.controller.zoomCamera(factor)
                previousScale = currentScale
            }
        }

        Rectangle {
            anchors.fill: parent
            color: "transparent"
            border.color: Theme.snapshot.colorBorder
            border.width: Theme.snapshot.borderWidth
        }
    }

    Label {
        anchors.left: parent.left
        anchors.bottom: parent.bottom
        anchors.margins: Theme.snapshot.spaceSm
        visible: root.sceneSupported && root.cameraValid
        text: "mm · XY 平面 · 中性几何预览"
        color: Theme.snapshot.colorTextMuted
        font.pointSize: Theme.snapshot.fontSizeSmall
    }
    Label {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.margins: Theme.snapshot.spaceSm
        visible: root.sceneSupported && root.cameraValid
        objectName: "appearanceAreaLightIndicator"
        text: root.sceneData.area_source.enabled ? "面光源" : "面光源关闭"
        color: root.sceneData.area_source.enabled ? "#cf9b52" : Theme.snapshot.colorTextMuted
        font.pointSize: Theme.snapshot.fontSizeSmall
    }
    Label {
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.leftMargin: Theme.snapshot.spaceSm
        anchors.topMargin: Theme.snapshot.spaceLg + Theme.snapshot.spaceSm
        visible: root.sceneSupported && root.cameraValid
        objectName: "appearanceEnvironmentIndicator"
        text: root.sceneData.environment_enabled ? "均匀环境已启用" : "均匀环境关闭"
        color: root.sceneData.environment_enabled ? Theme.snapshot.colorText : Theme.snapshot.colorTextMuted
        font.pointSize: Theme.snapshot.fontSizeSmall
    }
    Label {
        anchors.centerIn: parent
        visible: !root.sceneSupported
        text: "预览目前只支持平面矩形基片。"
        color: Theme.snapshot.colorTextMuted
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignHCenter
        width: Math.min(parent.width - Theme.snapshot.spaceLg * 2, 360)
    }
    Label {
        anchors.centerIn: parent
        visible: root.sceneSupported && !root.cameraValid
        text: root.sceneData.camera.validation_error
        color: Theme.snapshot.colorWarning
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignHCenter
        width: Math.min(parent.width - Theme.snapshot.spaceLg * 2, 420)
    }

    Rectangle {
        objectName: "appearanceCameraFloatingControls"
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: Theme.snapshot.spaceSm
        width: cameraActions.implicitWidth + Theme.snapshot.spaceXs * 2
        height: cameraActions.implicitHeight + Theme.snapshot.spaceXs * 2
        radius: Theme.snapshot.radiusSm
        color: Theme.snapshot.colorSurface
        border.color: Theme.snapshot.colorBorder
        RowLayout {
            id: cameraActions
            anchors.fill: parent
            anchors.margins: Theme.snapshot.spaceXs
            spacing: Theme.snapshot.spaceXs
            ThemeControls.ThemeButton {
                objectName: "appearanceCameraZoomIn"
                text: "+"
                Accessible.name: "放大相机取景"
                enabled: root.sceneSupported && root.cameraValid
                ToolTip.visible: hovered
                ToolTip.text: Accessible.name
                onClicked: {
                    root.controller.beginCameraGesture()
                    root.controller.zoomCamera(0.8)
                    root.controller.commitCameraGesture()
                }
            }
            ThemeControls.ThemeButton {
                objectName: "appearanceCameraZoomOut"
                text: "−"
                Accessible.name: "缩小相机取景"
                enabled: root.sceneSupported && root.cameraValid
                ToolTip.visible: hovered
                ToolTip.text: Accessible.name
                onClicked: {
                    root.controller.beginCameraGesture()
                    root.controller.zoomCamera(1.25)
                    root.controller.commitCameraGesture()
                }
            }
            ThemeControls.ThemeButton {
                objectName: "appearanceCameraReset"
                text: "复位"
                Accessible.name: "将相机恢复为初始姿态"
                ToolTip.visible: hovered
                ToolTip.text: Accessible.name
                onClicked: root.controller.resetCamera()
            }
            ThemeControls.ThemeButton {
                objectName: "appearanceCameraHelp"
                text: "?"
                Accessible.name: "取景操作说明"
                Accessible.description: "左键拖动旋转；右键、中键或 Ctrl 加左键拖动平移；滚轮和触摸板缩放；触摸手势缩放和平移。"
                ToolTip.visible: hovered || activeFocus
                ToolTip.text: Accessible.description
            }
        }
    }

    Timer {
        id: wheelCommit
        interval: 140
        repeat: false
        onTriggered: root.controller.commitCameraGesture()
    }
}
