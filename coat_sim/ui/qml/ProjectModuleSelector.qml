pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import "theme_controls" as ThemeControls

Item {
    id: root
    objectName: selectorRootObjectName
    property var navigation: null
    property string currentModuleKey: ""
    property string selectorRootObjectName: "projectModuleSelectorRoot"
    property string selectorObjectName: "projectModuleSelector"
    property string selectorAccessibleName: "打开工程窗口"
    property bool showSelector: true
    readonly property real operationBarMinimumWidth: 90
    readonly property real operationBarPreferredWidth: 140
    // Theme is the application singleton injected into each project engine.
    // qmllint disable unqualified
    property var themeTokens: Theme.snapshot
    // qmllint enable unqualified
    readonly property var modules: navigation && navigation.projectModules
                                  ? navigation.projectModules : []
    readonly property bool canCloseProject: !!navigation
        && typeof navigation.closeProject === "function"
    readonly property bool canManageProject: !!navigation
        && typeof navigation.openProjectManagement === "function"
    readonly property string currentModuleName: {
        const key = String(currentModuleKey || "")
        for (const option of modules) {
            if (String(option.key) === key)
                return String(option.name)
        }
        return key.length > 0 ? "未知窗口" : "当前窗口"
    }
    readonly property var filmKeys: ["film_design", "film_optimization"]
    readonly property var depositionKeys: ["process", "machine", "source", "mask", "substrate"]
    readonly property var simulationKeys: ["film_simulation", "main", "ray_trace", "film_appearance"]
    readonly property var categorizedKeys: filmKeys.concat(depositionKeys).concat(simulationKeys)

    visible: !!navigation && showSelector
    height: themeTokens.controlHeightLg
    implicitWidth: operationBarPreferredWidth
    width: implicitWidth

    function modulesForKeys(keys) {
        return modules.filter(function(option) { return keys.indexOf(String(option.key)) >= 0 })
    }

    function toolModules() {
        return modules.filter(function(option) {
            return String(option.key) !== "materials" && categorizedKeys.indexOf(String(option.key)) < 0
        })
    }

    function activateModule(key) {
        if (navigation && navigation.openProjectModule)
            navigation.openProjectModule(String(key))
    }

    function closeProject() {
        if (canCloseProject)
            navigation.closeProject()
    }

    function openProjectManagement() {
        if (canManageProject)
            navigation.openProjectManagement()
    }

    function activateModuleItem(item, key) {
        const selectedKey = String(key)
        activateModule(selectedKey)
        // MenuItem toggles a checkable action when activated. Restore the
        // binding after the native toggle so the mark always denotes this window.
        Qt.callLater(function() {
            if (item) {
                item.checked = Qt.binding(function() {
                    return root.currentModuleKey === selectedKey
                })
            }
        })
    }

    function openMenu() {
        if (!navigation)
            return
        moduleMenu.open()
    }

    component ProjectMenuItem: MenuItem {
        id: menuItem
        readonly property var tokens: root.themeTokens
        readonly property real leadingAdornmentWidth:
            menuItem.checkable && menuItem.indicator ? menuItem.indicator.width + menuItem.spacing : 0
        readonly property real trailingAdornmentWidth:
            menuItem.subMenu && menuItem.arrow ? menuItem.arrow.width + menuItem.spacing : 0
        font.family: menuItem.tokens.fontFamily
        font.pointSize: menuItem.tokens.fontSizeBody
        padding: menuItem.tokens.spaceSm
        indicator: Text {
            width: menuItem.tokens.spaceLg
            height: menuItem.tokens.spaceLg
            x: menuItem.mirrored ? menuItem.width - width - menuItem.rightPadding : menuItem.leftPadding
            y: menuItem.topPadding + (menuItem.availableHeight - height) / 2
            visible: menuItem.checked
            text: "✓"
            font.family: menuItem.tokens.fontFamily
            font.pointSize: menuItem.tokens.fontSizeBody
            font.weight: Font.DemiBold
            color: menuItem.highlighted ? menuItem.tokens.colorTextOnAccent : menuItem.tokens.colorText
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        arrow: Text {
            width: menuItem.tokens.spaceMd
            height: menuItem.tokens.spaceLg
            x: menuItem.mirrored ? menuItem.leftPadding : menuItem.width - width - menuItem.rightPadding
            y: menuItem.topPadding + (menuItem.availableHeight - height) / 2
            visible: !!menuItem.subMenu
            text: "›"
            font.family: menuItem.tokens.fontFamily
            font.pointSize: menuItem.tokens.fontSizeBody + 2
            color: menuItem.highlighted ? menuItem.tokens.colorTextOnAccent : menuItem.tokens.colorText
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
        contentItem: Item {
            implicitWidth: menuItemLabel.implicitWidth
                           + menuItem.leadingAdornmentWidth + menuItem.trailingAdornmentWidth
            implicitHeight: menuItemLabel.implicitHeight
            Text {
                id: menuItemLabel
                anchors.fill: parent
                anchors.leftMargin: menuItem.mirrored ? menuItem.trailingAdornmentWidth
                                                      : menuItem.leadingAdornmentWidth
                anchors.rightMargin: menuItem.mirrored ? menuItem.leadingAdornmentWidth
                                                       : menuItem.trailingAdornmentWidth
                text: menuItem.text
                font: menuItem.font
                color: !menuItem.enabled ? menuItem.tokens.colorDisabledText
                      : menuItem.highlighted ? menuItem.tokens.colorTextOnAccent
                                             : menuItem.tokens.colorText
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
            }
        }
        background: Rectangle {
            color: !menuItem.enabled ? menuItem.tokens.colorDisabledSurface
                  : menuItem.down || menuItem.highlighted ? menuItem.tokens.colorAccent
                  : menuItem.hovered ? menuItem.tokens.colorSurfaceRaised : menuItem.tokens.colorSurface
            border.width: menuItem.activeFocus ? menuItem.tokens.focusRingWidth : 0
            border.color: menuItem.tokens.colorFocusRing
        }
    }

    component ThemedProjectMenu: Menu {
        popupType: Popup.Item
        width: Math.max(232, implicitWidth)
        delegate: ProjectMenuItem {}
        palette.window: root.themeTokens.colorWindow
        palette.windowText: root.themeTokens.colorText
        palette.base: root.themeTokens.colorSurface
        palette.alternateBase: root.themeTokens.colorSurfaceRaised
        palette.text: root.themeTokens.colorText
        palette.button: root.themeTokens.colorSurfaceRaised
        palette.buttonText: root.themeTokens.colorText
        palette.highlight: root.themeTokens.colorAccent
        palette.highlightedText: root.themeTokens.colorTextOnAccent
        palette.mid: root.themeTokens.colorBorder
        palette.dark: root.themeTokens.colorBorderStrong
        background: Rectangle {
            color: root.themeTokens.colorSurface
            border.width: root.themeTokens.borderWidth
            border.color: root.themeTokens.colorBorderStrong
            radius: root.themeTokens.radiusSm
        }
    }

    ThemeControls.ThemeButton {
        id: selectorButton
        objectName: root.selectorObjectName
        width: Math.min(140, root.width)
        height: root.height
        text: root.currentModuleName
        Accessible.name: root.selectorAccessibleName
        ToolTip.visible: hovered
        ToolTip.text: root.currentModuleName
        onClicked: root.openMenu()
        contentItem: Item {
            Text {
                anchors.left: parent.left
                anchors.right: dropArrow.left
                anchors.rightMargin: root.themeTokens.spaceXs
                anchors.verticalCenter: parent.verticalCenter
                text: selectorButton.text
                font: selectorButton.font
                color: !selectorButton.enabled ? root.themeTokens.colorDisabledText
                      : selectorButton.primary || selectorButton.checked
                        ? root.themeTokens.colorTextOnAccent : root.themeTokens.colorText
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
                wrapMode: Text.NoWrap
            }
            Item {
                id: dropArrow
                width: 10
                height: 8
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                Rectangle {
                    width: 6
                    height: 1.5
                    radius: 1
                    x: 1
                    y: 2
                    rotation: 45
                    color: root.themeTokens.colorText
                }
                Rectangle {
                    width: 6
                    height: 1.5
                    radius: 1
                    x: 5
                    y: 2
                    rotation: -45
                    color: root.themeTokens.colorText
                }
            }
        }

        Keys.onDownPressed: function(event) {
            root.openMenu()
            event.accepted = true
        }

        ThemedProjectMenu {
            id: moduleMenu
            objectName: "projectModuleMenu"
            y: selectorButton.height
            modal: true
            focus: true
            onClosed: Qt.callLater(selectorButton.forceActiveFocus)

            ThemedProjectMenu {
                id: filmMenu
                objectName: "projectModuleFilmMenu"
                title: "膜系"
                Instantiator {
                    model: root.modulesForKeys(root.filmKeys)
                    delegate: ProjectMenuItem {
                        id: filmModuleItem
                        required property var modelData
                        objectName: "projectModuleAction_" + String(modelData.key)
                        text: String(modelData.name)
                        checkable: true
                        checked: root.currentModuleKey === String(modelData.key)
                        onTriggered: root.activateModuleItem(filmModuleItem, modelData.key)
                    }
                    onObjectAdded: function(index, object) { filmMenu.insertItem(index, object) }
                    onObjectRemoved: function(index, object) { filmMenu.removeItem(object) }
                }
            }

            ThemedProjectMenu {
                id: depositionMenu
                objectName: "projectModuleDepositionMenu"
                title: "沉积"
                Instantiator {
                    model: root.modulesForKeys(root.depositionKeys)
                    delegate: ProjectMenuItem {
                        id: depositionModuleItem
                        required property var modelData
                        objectName: "projectModuleAction_" + String(modelData.key)
                        text: String(modelData.name)
                        checkable: true
                        checked: root.currentModuleKey === String(modelData.key)
                        onTriggered: root.activateModuleItem(depositionModuleItem, modelData.key)
                    }
                    onObjectAdded: function(index, object) { depositionMenu.insertItem(index, object) }
                    onObjectRemoved: function(index, object) { depositionMenu.removeItem(object) }
                }
            }

            ThemedProjectMenu {
                id: simulationMenu
                objectName: "projectModuleSimulationMenu"
                title: "仿真"
                Instantiator {
                    model: root.modulesForKeys(root.simulationKeys)
                    delegate: ProjectMenuItem {
                        id: simulationModuleItem
                        required property var modelData
                        objectName: "projectModuleAction_" + String(modelData.key)
                        text: String(modelData.name)
                        checkable: true
                        checked: root.currentModuleKey === String(modelData.key)
                        onTriggered: root.activateModuleItem(simulationModuleItem, modelData.key)
                    }
                    onObjectAdded: function(index, object) { simulationMenu.insertItem(index, object) }
                    onObjectRemoved: function(index, object) { simulationMenu.removeItem(object) }
                }
            }

            MenuSeparator {
                objectName: "projectModuleToolsSeparator"
                visible: root.toolModules().length > 0
            }

            Instantiator {
                id: toolModuleItems
                model: root.toolModules()
                delegate: ProjectMenuItem {
                    id: toolModuleItem
                    required property var modelData
                    objectName: "projectModuleAction_" + String(modelData.key)
                    text: String(modelData.name)
                    checkable: true
                    checked: root.currentModuleKey === String(modelData.key)
                    onTriggered: root.activateModuleItem(toolModuleItem, modelData.key)
                }
                // Keep tool leaves before the independent close-project command.
                onObjectAdded: function(index, object) { moduleMenu.insertItem(4 + index, object) }
                onObjectRemoved: function(index, object) { moduleMenu.removeItem(object) }
            }

            // The close command is separate from the module list. The owner facade
            // performs the shared save/discard/cancel preflight before closing.
            Instantiator {
                id: closeProjectSeparators
                objectName: "projectCloseSeparators"
                model: root.canCloseProject ? 1 : 0
                delegate: MenuSeparator { objectName: "projectCloseSeparator" }
                onObjectAdded: function(_index, object) {
                    const lastItem = moduleMenu.itemAt(moduleMenu.count - 1)
                    const action = closeProjectActions.objectAt(0)
                    const insertIndex = action && lastItem === action
                        ? moduleMenu.count - 1 : moduleMenu.count
                    moduleMenu.insertItem(insertIndex, object)
                }
                onObjectRemoved: function(_index, object) { moduleMenu.removeItem(object) }
            }

            Instantiator {
                id: closeProjectActions
                objectName: "projectCloseActions"
                model: root.canCloseProject ? 1 : 0
                delegate: ProjectMenuItem {
                    objectName: "projectCloseAction"
                    text: "关闭工程"
                    onTriggered: root.closeProject()
                }
                onObjectAdded: function(_index, object) {
                    moduleMenu.insertItem(moduleMenu.count, object)
                }
                onObjectRemoved: function(_index, object) { moduleMenu.removeItem(object) }
            }

            // Keep the project-level management entry below module actions and
            // directly above the existing close separator/command.
            Instantiator {
                id: projectManagementActions
                objectName: "projectManagementActions"
                model: root.canManageProject ? 1 : 0
                delegate: ProjectMenuItem {
                    objectName: "projectManagementAction"
                    text: "工程管理…"
                    onTriggered: root.openProjectManagement()
                }
                onObjectAdded: function(_index, object) {
                    moduleMenu.insertItem(4 + root.toolModules().length, object)
                }
                onObjectRemoved: function(_index, object) { moduleMenu.removeItem(object) }
            }
        }
    }
}
