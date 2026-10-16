pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import "theme_controls" as ThemeControls

// Presentation only. The original controls remain the action/state owners.
Item {
    id: bar
    property real spacing: themeTokens.spaceXs
    // qmllint disable unqualified
    property var themeTokens: Theme.snapshot
    // qmllint enable unqualified
    property var itemMetrics: []
    property bool layoutReady: false
    property var overflowItems: []
    readonly property bool layoutPending: layoutTimer.running
    readonly property var overflowCarrier: {
        for (const entry of itemMetrics) {
            if (entry.item.operationBarOverflowButton !== undefined
                    && entry.item.projectResources !== null)
                return entry.item
        }
        return null
    }
    readonly property var overflowAnchor: overflowCarrier
        ? overflowCarrier.operationBarOverflowButton : overflowButton
    property var activeOverflowCarrier: null
    readonly property real contentWidth: planLayout().extent
    implicitWidth: {
        let result = 0
        for (const entry of itemMetrics)
            result += entry.preferred
        return result + Math.max(0, itemMetrics.length - 1) * spacing
    }
    implicitHeight: {
        let result = themeTokens.controlHeightMd
        for (const entry of itemMetrics)
            result = Math.max(result, entry.height)
        return result
    }
    height: implicitHeight
    clip: true
    Accessible.name: "操作栏"

    function collectMetrics() {
        const result = []
        for (const item of children) {
            if (item === overflowButton || item.operationBarExternalAnchor === true || !item.visible)
                continue
            const flexible = item.operationBarFlexible === true
            const preferred = item.operationBarPreferredWidth !== undefined
                ? Number(item.operationBarPreferredWidth)
                : flexible ? Math.min(240, item.implicitWidth)
                           : Math.max(item.width, item.implicitWidth)
            const minimum = item.operationBarMinimumWidth !== undefined
                ? Number(item.operationBarMinimumWidth) : flexible ? 0 : preferred
            result.push({item: item, preferred: preferred, minimum: minimum,
                         button: actionSource(item) !== null,
                         flexible: flexible,
                         height: Math.max(item.height, item.implicitHeight)})
        }
        return result
    }

    function actionSource(item) {
        if (item.operationBarAction !== undefined)
            return item.operationBarAction
        return typeof item.click === "function" && typeof item.checkable === "boolean"
            ? item : null
    }

    function allocateLayout(tailReserve) {
        const metrics = itemMetrics
        let fixed = 0
        let fixedCount = 0
        for (const entry of metrics) {
            if (!entry.button) {
                fixed += entry.minimum
                ++fixedCount
            }
        }
        let remaining = Math.max(0, width - tailReserve - fixed
                                - Math.max(0, fixedCount - 1) * spacing)
        const widths = []
        const overflow = []
        let shownCount = fixedCount
        for (const entry of metrics) {
            if (!entry.button)
                continue
            const cost = entry.preferred + (shownCount > 0 ? spacing : 0)
            if (cost <= remaining) {
                widths.push({item: entry.item, width: entry.preferred})
                remaining -= cost
                ++shownCount
            } else {
                overflow.push(entry.item)
            }
        }
        // Compact navigation and resource pickers keep their original priority.
        for (const entry of metrics) {
            if (!entry.button) {
                const growth = Math.min(remaining, Math.max(0, entry.preferred - entry.minimum))
                widths.push({item: entry.item, width: entry.minimum + growth})
                remaining -= growth
            }
        }
        return {widths: widths, overflow: overflow}
    }

    function planLayout() {
        let plan = allocateLayout(0)
        const carrier = overflowCarrier
        const placement = carrier ? plan.widths.find(value => value.item === carrier) : null
        const internalOverflow = !!carrier && (carrier.hasAdditionalResourceActions
            || (placement && placement.width + 0.01 < carrier.operationBarInlineWidth))
        const hasTail = plan.overflow.length > 0 || internalOverflow
        const tailWidth = carrier ? carrier.operationBarOverflowWidth : overflowButton.implicitWidth
        if (hasTail)
            plan = allocateLayout(tailWidth + spacing)
        let extent = 0
        for (const value of plan.widths)
            extent += value.width
        extent += Math.max(0, plan.widths.length - 1) * spacing
        if (hasTail)
            extent += tailWidth + (plan.widths.length ? spacing : 0)
        plan.extent = extent
        return plan
    }

    function isOverflowed(item) {
        return overflowItems.indexOf(item) >= 0
    }

    function anchorMenus(item, anchor) {
        // A button's existing submenu must open beside the visible ellipsis,
        // not beside its clipped, off-row original control.
        if (item.data === undefined)
            return
        for (const child of item.data) {
            if (child instanceof Menu)
                child.parent = anchor
        }
    }

    function applyLayout() {
        const plan = planLayout()
        if (overflowItems.length !== plan.overflow.length
                || plan.overflow.some((item, index) => overflowItems[index] !== item))
            overflowItems = plan.overflow
        if (activeOverflowCarrier !== overflowCarrier) {
            if (activeOverflowCarrier) {
                activeOverflowCarrier.operationBarOverflowActions = []
                activeOverflowCarrier.operationBarOverflowOwner = null
            }
            activeOverflowCarrier = overflowCarrier
        }
        if (activeOverflowCarrier) {
            activeOverflowCarrier.operationBarOverflowOwner = bar
            if (activeOverflowCarrier.operationBarOverflowActions !== overflowItems)
                activeOverflowCarrier.operationBarOverflowActions = overflowItems
        }
        let position = 0
        for (const entry of itemMetrics) {
            const item = entry.item
            const hidden = plan.overflow.indexOf(item) >= 0
            if (entry.button) {
                const source = actionSource(item)
                if (itemFocusPolicies.get(source) === undefined)
                    itemFocusPolicies.set(source, source.focusPolicy)
                if (hidden && source.activeFocus)
                    overflowAnchor.forceActiveFocus()
                source.focusPolicy = hidden ? Qt.NoFocus : itemFocusPolicies.get(source)
                source.Accessible.ignored = hidden
                anchorMenus(source, hidden ? overflowAnchor : source)
                if (item.operationBarOverflowAnchor !== undefined)
                    item.operationBarOverflowAnchor = hidden ? overflowAnchor : null
            }
            if (hidden) {
                // Clipping excludes drawing and mouse hits. Do not alter the
                // source visible/enabled bindings or native action shortcuts.
                item.x = -item.width - 1
                item.y = 0
                continue
            }
            const placement = plan.widths.find(value => value.item === item)
            if (!entry.button && placement)
                item.width = placement.width
            item.x = position
            item.y = Math.max(0, (height - item.height) / 2)
            position += item.width + spacing
        }
        // The one existing resource menu is visually hosted by the whole bar,
        // after every inline module action rather than inside its resource row.
        const anchor = overflowAnchor
        anchor.x = Math.min(Math.max(0, width - anchor.width), position)
        anchor.y = Math.max(0, (height - anchor.height) / 2)
    }

    function refreshLayout() {
        // Geometry is sampled before placement, never read through a binding
        // that also changes that geometry (notably wrapped labels/editors).
        const next = collectMetrics()
        if (itemMetrics.length !== next.length || next.some((entry, index) => {
                const previous = itemMetrics[index]
                return previous.item !== entry.item || previous.preferred !== entry.preferred
                    || previous.minimum !== entry.minimum || previous.button !== entry.button
                    || previous.height !== entry.height || previous.flexible !== entry.flexible
            }))
            itemMetrics = next
        applyLayout()
    }

    function triggerItem(item) {
        const source = item ? actionSource(item) : null
        if (source && item.visible && source.enabled)
            source.click()
    }

    // WeakMap never extends a removed control's lifetime.
    readonly property var itemFocusPolicies: new WeakMap()
    onWidthChanged: {
        if (layoutReady)
            refreshLayout()
        else
            layoutTimer.restart()
    }
    onHeightChanged: layoutTimer.restart()
    onSpacingChanged: layoutTimer.restart()
    onChildrenChanged: layoutTimer.restart()
    Component.onCompleted: {
        layoutReady = true
        refreshLayout()
    }
    Timer { id: layoutTimer; interval: 0; onTriggered: bar.refreshLayout() }
    Instantiator {
        model: bar.children
        delegate: Connections {
            required property var modelData
            target: modelData === overflowButton || modelData.operationBarExternalAnchor === true ? null : modelData
            ignoreUnknownSignals: true
            function onVisibleChanged() {
                if (bar.layoutReady)
                    bar.refreshLayout()
                else
                    layoutTimer.restart()
            }
            function onWidthChanged() { layoutTimer.restart() }
            function onHeightChanged() { layoutTimer.restart() }
            function onImplicitWidthChanged() { layoutTimer.restart() }
            function onImplicitHeightChanged() { layoutTimer.restart() }
            function onOperationBarPreferredWidthChanged() { layoutTimer.restart() }
            function onOperationBarMinimumWidthChanged() { layoutTimer.restart() }
        }
    }

    ThemeControls.ThemeButton {
        id: overflowButton
        objectName: "operationBarOverflowButton"
        visible: bar.overflowItems.length > 0 && bar.overflowCarrier === null
        text: "..."
        Accessible.name: "更多操作"
        z: 1
        onClicked: overflowMenu.open()
        Menu {
            id: overflowMenu
            objectName: "operationBarOverflowMenu"
            y: overflowButton.height
            Instantiator {
                model: bar.overflowCarrier === null ? bar.overflowItems : []
                delegate: MenuItem {
                    id: overflowItem
                    required property var modelData
                    readonly property var source: bar.actionSource(modelData)
                    objectName: "operationBarOverflow_" + String(modelData.objectName || "action")
                    text: String(source.text || source.Accessible.name || "操作")
                    enabled: visible && source.enabled
                    Accessible.ignored: !visible
                    visible: modelData.visible
                    height: visible ? implicitHeight : 0
                    checkable: source.checkable
                    checked: source.checked
                    onTriggered: {
                        bar.triggerItem(modelData)
                        // MenuItem's own native toggle must not become state.
                        checked = Qt.binding(function() { return overflowItem.source.checked })
                    }
                }
                onObjectAdded: (index, object) => overflowMenu.insertItem(index, object)
                onObjectRemoved: (index, object) => overflowMenu.removeItem(object)
            }
        }
    }
}
