pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "."

ColumnLayout {
    id: panel
    required property var controller
    required property var themeTokens
    signal applyRequested()
    signal saveRequested()
    spacing: themeTokens.spaceSm
    function selectPage(index) { panel.controller.selectWorkflow(index) }
    Component.onCompleted: selectPage(pages.currentIndex)

    TabBar {
        id: pages
        objectName: "filmSensitivityWorkflowTabs"
        Layout.fillWidth: true
        enabled: !panel.controller.busy
        Component.onCompleted: currentIndex = 1
        onCurrentIndexChanged: panel.selectPage(currentIndex)
        Repeater {
            model: [{label: "参数表预期误差分析", name: "filmSensitivityTableTab"},
                    {label: "理想敏感度优化", name: "filmSensitivityIdealTab"},
                    {label: "高级兼容", name: "filmSensitivityLegacyTab"}]
            delegate: TabButton {
                id: tab
                required property var modelData
                objectName: modelData.name
                text: modelData.label
                contentItem: Text {
                    text: tab.text
                    color: tab.checked ? panel.themeTokens.colorTextOnAccent : panel.themeTokens.colorText
                    font: tab.font
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
                background: Rectangle {
                    color: tab.checked ? panel.themeTokens.colorAccent
                          : tab.hovered ? panel.themeTokens.colorSurfaceRaised
                          : panel.themeTokens.colorSurface
                    radius: panel.themeTokens.radiusSm
                    border.color: tab.visualFocus ? panel.themeTokens.colorFocusRing : panel.themeTokens.colorBorder
                    border.width: 1
                }
            }
        }
    }

    StackLayout {
        objectName: "filmSensitivityWorkflowPages"
        Layout.fillWidth: true
        Layout.fillHeight: true
        currentIndex: pages.currentIndex

        FilmThicknessErrorParameterPanel {
            controller: panel.controller
            themeTokens: panel.themeTokens
            onSaveRequested: panel.saveRequested()
        }
        FilmIdealSensitivityPanel {
            controller: panel.controller
            themeTokens: panel.themeTokens
            onApplyRequested: panel.applyRequested()
            onSaveRequested: panel.saveRequested()
        }
        FilmSensitivityPanel {
            controller: panel.controller
            themeTokens: panel.themeTokens
            onApplyRequested: panel.applyRequested()
            onSaveRequested: panel.saveRequested()
        }
    }
}
