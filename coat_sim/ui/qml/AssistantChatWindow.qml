pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import CoatUi 1.0
import "."
import "theme_controls" as ThemeControls

Window {
    id: chatWindow
    title: "看板娘 · 对话"
    width: 860; height: 720
    minimumWidth: 560; minimumHeight: 420
    visible: false
    readonly property var themeColors: typeof Theme !== "undefined" ? Theme.snapshot : null
    palette.window: themeColors ? themeColors.colorWindow : palette.window
    palette.windowText: themeColors ? themeColors.colorText : palette.windowText
    palette.base: themeColors ? themeColors.colorSurface : palette.base
    palette.alternateBase: themeColors ? themeColors.colorSurfaceRaised : palette.alternateBase
    palette.text: themeColors ? themeColors.colorText : palette.text
    palette.button: themeColors ? themeColors.colorSurfaceRaised : palette.button
    palette.buttonText: themeColors ? themeColors.colorText : palette.buttonText
    palette.highlight: themeColors ? themeColors.colorSelection : palette.highlight
    palette.highlightedText: themeColors ? themeColors.colorText : palette.highlightedText
    palette.placeholderText: themeColors ? themeColors.colorTextMuted : palette.placeholderText
    palette.mid: themeColors ? themeColors.colorBorder : palette.mid
    palette.dark: themeColors ? themeColors.colorBorderStrong : palette.dark
    palette.light: themeColors ? themeColors.colorSurface : palette.light
    palette.link: themeColors ? themeColors.colorAccent : palette.link
    palette.linkVisited: themeColors ? themeColors.colorAccentPressed : palette.linkVisited
    palette.brightText: themeColors ? themeColors.colorError : palette.brightText
    palette.disabled.windowText: themeColors ? themeColors.colorDisabledText : palette.disabled.windowText
    palette.disabled.text: themeColors ? themeColors.colorDisabledText : palette.disabled.text
    palette.disabled.buttonText: themeColors ? themeColors.colorDisabledText : palette.disabled.buttonText
    palette.disabled.placeholderText: themeColors ? themeColors.colorDisabledText : palette.disabled.placeholderText
    palette.disabled.base: themeColors ? themeColors.colorDisabledSurface : palette.disabled.base
    palette.disabled.alternateBase: themeColors ? themeColors.colorDisabledSurface : palette.disabled.alternateBase
    palette.disabled.button: themeColors ? themeColors.colorDisabledSurface : palette.disabled.button
    palette.disabled.window: themeColors ? themeColors.colorDisabledSurface : palette.disabled.window
    color: themeColors ? themeColors.colorWindow
                    : (active ? palette.active.window : palette.inactive.window)
    flags: Qt.Window | Qt.FramelessWindowHint
    property bool useCustomBackground: false
    property bool followOutput: true
    property bool manualScrollActive: false
    property bool automaticScroll: false
    property string scrollSessionId: ""
    property var sessionScrollPositions: ({})
    property string pendingScrollSessionId: ""
    property var pendingScrollAnchor: null
    property int pendingScrollRestoreAttempts: 0
    property int scrollRestoreGeneration: 0
    property var sessionRestoreTicket: null
    property var pendingBatchAnchor: null
    property var batchRestoreTicket: null
    property bool historyLoadGestureActive: false
    property real manualScrollPreviousY: 0
    property var rootLifecycleTicket: ({alive: true})
    property int lastScrollRestoreAttemptCount: 0
    property int lastScrollAnchorScanCount: 0
    property var expandedThoughts: ({})
    property var preview: ({})
    property var completionRows: []
    property int completionStart: 0
    property var selectionSurfaces: []
    property var selectedHistory: ({})
    property int selectionSequence: 0
    property bool selectionDragging: false
    property bool selectionUpdating: false
    property var selectionAnchor: ({x: 0, y: 0})
    property var selectionPointer: ({x: 0, y: 0})
    property string selectionAnchorKey: ""
    property int selectionAnchorPosition: 0
    readonly property color ink: themeColors ? themeColors.colorText : palette.active.windowText
    readonly property color quietInk: themeColors ? themeColors.colorTextMuted : palette.active.placeholderText
    readonly property color thoughtInk: themeColors ? themeColors.colorTextMuted
        : Qt.rgba(ink.r * 0.76 + color.r * 0.24,
                  ink.g * 0.76 + color.g * 0.24,
                  ink.b * 0.76 + color.b * 0.24, 1)
    readonly property font bodyFont: Qt.font({family: themeColors ? themeColors.fontFamily : fontReference.font.family,
                                             pointSize: Math.max(9, themeColors ? themeColors.fontSizeBody : fontReference.font.pointSize)})
    ThemeControls.ThemeLabel {
        id: fontReference
        visible: false
        font.family: chatWindow.themeColors ? chatWindow.themeColors.fontFamily : "Sans Serif"
        font.pointSize: chatWindow.themeColors ? chatWindow.themeColors.fontSizeBody : 9
    }
    readonly property color nameInk: themeColors ? themeColors.colorAccent : palette.active.highlight
    readonly property bool hasDraft: draft.text.trim().length > 0 || assistant.attachments.length > 0
    readonly property bool hasPausedMessages: assistant.pendingMessages.some(function(message) {
        return message.paused === true
    })

    component ThemedMenuItem: MenuItem {
        id: menuItem
        font.family: Theme.snapshot.fontFamily
        font.pointSize: Theme.snapshot.fontSizeBody
        padding: Theme.snapshot.spaceSm
        contentItem: ThemeControls.ThemeLabel {
            text: menuItem.text
            font: menuItem.font
            color: !menuItem.enabled ? Theme.snapshot.colorDisabledText
                                    : menuItem.highlighted ? Theme.snapshot.colorTextOnAccent
                                                           : Theme.snapshot.colorText
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            color: !menuItem.enabled ? Theme.snapshot.colorDisabledSurface
                  : menuItem.down || menuItem.highlighted ? Theme.snapshot.colorAccent
                  : menuItem.hovered ? Theme.snapshot.colorSurfaceRaised
                                     : Theme.snapshot.colorSurface
            border.width: menuItem.activeFocus ? Theme.snapshot.focusRingWidth : 0
            border.color: Theme.snapshot.colorFocusRing
        }
    }

    component ThemedListItem: ItemDelegate {
        id: listItem
        hoverEnabled: true
        focusPolicy: Qt.StrongFocus
        font.family: Theme.snapshot.fontFamily
        font.pointSize: Theme.snapshot.fontSizeBody
        padding: Theme.snapshot.spaceSm
        contentItem: ThemeControls.ThemeLabel {
            text: listItem.text
            font: listItem.font
            color: !listItem.enabled ? Theme.snapshot.colorDisabledText
                                    : listItem.highlighted ? Theme.snapshot.colorTextOnAccent
                                                           : Theme.snapshot.colorText
            wrapMode: Text.WordWrap
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: Theme.snapshot.radiusSm
            color: !listItem.enabled ? Theme.snapshot.colorDisabledSurface
                  : listItem.down || listItem.highlighted ? Theme.snapshot.colorAccent
                  : listItem.hovered ? Theme.snapshot.colorSurfaceRaised
                                     : Theme.snapshot.colorSurface
            border.width: listItem.activeFocus ? Theme.snapshot.focusRingWidth : Theme.snapshot.borderWidth
            border.color: listItem.activeFocus ? Theme.snapshot.colorFocusRing
                         : listItem.hovered && listItem.enabled ? Theme.snapshot.colorBorderStrong
                                                               : Theme.snapshot.colorBorder
        }
    }

    onInkChanged: { contextRing.requestPaint(); assistant.refreshAppearance(ink.toString()) }
    onVisibleChanged: {
        if (visible) {
            assistant.refreshAppearance(ink.toString())
            for (const surface of selectionSurfaces.slice(0))
                scheduleSelectionRestore(surface)
            requestFollow()
        } else {
            cancelPendingScrollRestore()
            selectionScroll.stop()
            endHistorySelection()
            for (const surface of selectionSurfaces.slice(0)) {
                if (surface.selectionTicket) surface.selectionTicket.alive = false
                surface.selectionTicket = null
            }
        }
    }
    onClosing: function(close) { close.accepted = false; cancelPendingScrollRestore(); hide() }
    Component.onDestruction: {
        rootLifecycleTicket.alive = false
        cancelPendingScrollRestore()
        const surfaces = selectionSurfaces.slice(0)
        for (const surface of surfaces) {
            if (surface && surface.selectionTicket) surface.selectionTicket.alive = false
        }
    }

    function sendDraft() {
        if (assistant.sendMessage(draft.text)) {
            draft.clear()
            completionPopup.close()
        }
    }
    function steerDraft() {
        if (assistant.steerMessage(draft.text)) {
            draft.clear()
            completionPopup.close()
        }
    }
    function pasteDraft() {
        if (!assistant.pasteClipboardImage()) draft.paste()
    }
    function contextPercentage() {
        const usage = assistant.contextUsage
        if (!(Number(usage.used) > 0)) return "—"
        if (!usage.known) return "?"
        const percentage = Math.max(0, Number(usage.ratio) || 0) * 100
        if (percentage < 1)
            return percentage >= 0.1 ? percentage.toFixed(1) + "%" : "＜1%"
        return Math.round(percentage) + "%"
    }
    function endPosition() {
        // Use the same range the ListView itself clamps against. Mixing the
        // realized tail geometry with contentHeight estimates makes a manual
        // scrollbar jump when virtualization creates or releases the tail row.
        return transcript.originY + Math.max(0, transcript.contentHeight - transcript.height)
    }
    function syncHistoryScrollBar() {
        if (!historyScrollBar || historyScrollBar.pressed) return
        const range = historyScrollBar.scrollRange
        const available = 1 - historyScrollBar.size
        const next = range > 0 && available > 0
            ? Math.max(0, Math.min(available,
                (transcript.contentY - transcript.originY) / range * available))
            : 0
        if (Math.abs(historyScrollBar.position - next) <= 0.0001) return
        historyScrollBar.syncingPosition = true
        historyScrollBar.position = next
        historyScrollBar.syncingPosition = false
    }
    function scheduleHistoryScrollBarSync() {
        if (!historyScrollBar || historyScrollBar.pressed) return
        scrollBarSyncTimer.restart()
    }
    function rememberSessionScroll() {
        if (!scrollSessionId || transcript.count <= 0) return
        const top = transcript.contentY
        const bottom = top + transcript.height
        const children = transcript.contentItem ? transcript.contentItem.children : []
        let anchorItem = null
        let nextItem = null
        lastScrollAnchorScanCount = 0
        for (let childIndex = 0; childIndex < children.length; ++childIndex) {
            lastScrollAnchorScanCount += 1
            const item = children[childIndex]
            if (!item || typeof item.index !== "number" || item.turnData === undefined)
                continue
            if (item.delegatePooled === true) continue
            if (item.y + item.height <= top) continue
            if (item.y < bottom) {
                if (!anchorItem || item.y < anchorItem.y) anchorItem = item
            } else if (!nextItem || item.y < nextItem.y) {
                nextItem = item
            }
        }
        if (!anchorItem) {
            const sampleCount = Math.min(8, Math.max(1, Math.ceil(transcript.height / 80)))
            for (let sample = 0; sample < sampleCount; ++sample) {
                lastScrollAnchorScanCount += 1
                const y = top + Math.min(transcript.height - 1, sample * 80 + 1)
                const index = transcript.indexAt(transcript.width / 2, y)
                if (index < 0) continue
                const item = transcript.itemAtIndex(index)
                if (item && (!anchorItem || item.y < anchorItem.y)) anchorItem = item
            }
        }
        if (!anchorItem) anchorItem = nextItem
        const anchorIndex = anchorItem ? anchorItem.index : -1
        const anchorKey = anchorItem && anchorItem.turnData
            ? String(anchorItem.turnData.key || "") : ""
        if (anchorKey.length > 0) assistant.rememberHistoryAnchor(anchorKey)
        const saved = Object.assign({}, sessionScrollPositions)
        saved[scrollSessionId] = {
            index: anchorIndex,
            key: anchorKey,
            viewportOffset: anchorItem ? anchorItem.y - transcript.contentY : 0,
            contentY: Math.max(0, transcript.contentY - transcript.originY),
            follow: followOutput
        }
        sessionScrollPositions = saved
    }
    function schedulePendingSessionAnchorRestore(item) {
        const ticket = sessionRestoreTicket
        const saved = pendingScrollAnchor
        if (!ticket || !ticket.alive || !saved || !pendingScrollSessionId
                || pendingScrollSessionId !== assistant.currentSessionId)
            return
        if (item && saved.key && (!item.turnData
                || String(item.turnData.key || "") !== String(saved.key)))
            return
        if (ticket.correctionQueued) return
        ticket.correctionQueued = true
        const owner = chatWindow
        const bridge = assistant
        const list = transcript
        Qt.callLater(function() {
            if (!ticket.alive) return
            ticket.correctionQueued = false
            if (owner.sessionRestoreTicket !== ticket
                    || bridge.currentSessionId !== pendingScrollSessionId
                    || list.count <= 0)
                return
            owner.restoreSessionScroll()
        })
    }
    function finishSessionAnchorRestore(ticket) {
        if (!ticket || !ticket.alive || sessionRestoreTicket !== ticket) return
        sessionRestoreTimer.stop()
        pendingScrollSessionId = ""
        pendingScrollAnchor = null
        pendingScrollRestoreAttempts = 0
        sessionRestoreTicket = null
        ticket.alive = false
        followOutput = false
    }
    function restoreSessionScroll(fromStabilityTimer) {
        const sessionId = pendingScrollSessionId
        const saved = pendingScrollAnchor
        const ticket = sessionRestoreTicket
        if (!ticket || !ticket.alive) return
        if (assistant.historyProjectionPending) return
        if (!sessionId || !saved || sessionId !== assistant.currentSessionId
                || transcript.count <= 0) return
        lastScrollRestoreAttemptCount = pendingScrollRestoreAttempts + 1
        if (saved.follow) {
            pendingScrollSessionId = ""
            pendingScrollAnchor = null
            pendingScrollRestoreAttempts = 0
            followOutput = true
            requestFollow()
            return
        }
        followOutput = false
        let resolvedIndex = saved.key ? assistant.visibleTurnIndex(saved.key) : -1
        if (saved.key && resolvedIndex < 0) {
            // A saved QML anchor may outlive its bounded three-session cache.
            // Never silently map it to an unrelated row at the same index.
            cancelPendingScrollRestore()
            followOutput = true
            requestFollow()
            return
        }
        if (resolvedIndex < 0) resolvedIndex = Number(saved.index) || 0
        const index = Math.max(0, Math.min(transcript.count - 1, resolvedIndex))
        const owner = chatWindow
        const bridge = assistant
        const list = transcript
        if (ticket.attempts === undefined) ticket.attempts = 0
        if (ticket.timerSamples === undefined) ticket.timerSamples = 0
        if (ticket.correctionPasses === undefined) ticket.correctionPasses = 0
        ticket.attempts += 1
        if (fromStabilityTimer === true) ticket.timerSamples += 1
        else ticket.correctionPasses += 1
        lastScrollRestoreAttemptCount = ticket.attempts
        // Rich blocks can settle after their turn delegate is first created.
        // Keep the key+offset ticket alive through a bounded layout window so
        // y/height changes can correct the anchor without replaying history.
        if (ticket.timerSamples > 8 || ticket.correctionPasses > 128) {
            finishSessionAnchorRestore(ticket)
            return
        }
        let item = list.itemAtIndex(index)
        if (!item || item.delegatePooled === true
                || (saved.key && (!item.turnData
                    || String(item.turnData.key || "") !== String(saved.key)))) {
            automaticScroll = true
            list.positionViewAtIndex(index, ListView.Beginning)
            automaticScroll = false
        }
        Qt.callLater(function() {
            if (!ticket.alive) return
            if (bridge.currentSessionId !== sessionId || list.count <= 0
                    || owner.sessionRestoreTicket !== ticket) return
            const currentIndex = saved.key ? bridge.visibleTurnIndex(saved.key) : index
            if (currentIndex < 0) {
                owner.cancelPendingScrollRestore()
                owner.followOutput = true
                owner.requestFollow()
                return
            }
            item = list.itemAtIndex(currentIndex)
            if (!item || item.delegatePooled === true
                    || (saved.key && (!item.turnData
                        || String(item.turnData.key || "") !== String(saved.key)))) {
                owner.pendingScrollRestoreAttempts = ticket.attempts
                sessionRestoreTimer.restart()
                return
            }
            const desiredOffset = Number(saved.viewportOffset || 0)
            const rawPosition = item.y - desiredOffset
            const targetPosition = Math.max(list.originY,
                Math.min(owner.endPosition(), rawPosition))
            if (Math.abs(list.contentY - targetPosition) > 0.5) {
                owner.automaticScroll = true
                list.contentY = targetPosition
                owner.automaticScroll = false
                ticket.stableSample = null
            }
            owner.followOutput = false
            const actualOffset = item.y - list.contentY
            const sample = {
                key: saved.key || "#" + currentIndex,
                index: currentIndex,
                y: item.y,
                height: item.height,
                contentHeight: list.contentHeight,
                offset: actualOffset
            }
            const previous = ticket.stableSample
            const stable = previous
                && previous.key === sample.key
                && previous.index === sample.index
                && Math.abs(previous.y - sample.y) <= 0.5
                && Math.abs(previous.height - sample.height) <= 0.5
                && Math.abs(previous.contentHeight - sample.contentHeight) <= 0.5
            if (fromStabilityTimer === true
                    && Math.abs(actualOffset - desiredOffset) <= 1 && stable) {
                finishSessionAnchorRestore(ticket)
                return
            }
            ticket.stableSample = sample
            owner.pendingScrollRestoreAttempts = ticket.attempts
            sessionRestoreTimer.restart()
        })
    }
    function selectSession(sessionId) {
        rememberSessionScroll()
        assistant.selectConversation(sessionId)
    }
    function startNewSession() {
        rememberSessionScroll()
        assistant.newConversation()
    }
    function nearBottom() {
        if (transcript.count === 0) return true
        const last = transcript.itemAtIndex(transcript.count - 1)
        if (last === null || !transcript.atYEnd) return false
        if (transcript.contentHeight <= transcript.height + 2) return true
        const distance = last.y + last.height - transcript.contentY - transcript.height
        // A stale estimate that places the final row above the viewport does
        // not authorize following output while the user is reading history.
        return distance >= -4 && distance <= 4
    }
    function requestFollow() {
        if (!visible || !transcript || !historyScrollBar) return
        if (followOutput && !manualScrollActive && !selectionDragging && !transcript.moving
                && !transcript.dragging && !historyScrollBar.pressed
                && transcript.count > 0 && !followTimer.running)
            followTimer.start()
    }
    function scrollToEnd() {
        requestFollow()
    }
    function followLatest() {
        if (!followOutput || manualScrollActive || selectionDragging || transcript.moving
                || transcript.dragging || historyScrollBar.pressed || !visible) return
        automaticScroll = true
        transcript.positionViewAtEnd()
        automaticScroll = false
    }
    function cancelPendingScrollRestore() {
        scrollRestoreGeneration += 1
        if (sessionRestoreTicket) sessionRestoreTicket.alive = false
        if (batchRestoreTicket) batchRestoreTicket.alive = false
        sessionRestoreTicket = null
        batchRestoreTicket = null
        pendingBatchAnchor = null
        sessionRestoreTimer.stop()
        pendingScrollSessionId = ""
        pendingScrollAnchor = null
        pendingScrollRestoreAttempts = 0
        historyLoadGestureActive = false
    }
    function captureBatchAnchor() {
        if (followOutput || transcript.count <= 0) {
            pendingBatchAnchor = null
            return
        }
        const top = transcript.contentY
        const children = transcript.contentItem ? transcript.contentItem.children : []
        let anchor = null
        for (const item of children) {
            if (!item || typeof item.index !== "number" || !item.turnData) continue
            if (item.delegatePooled === true) continue
            if (item.y + item.height <= top) continue
            if (!anchor || item.y < anchor.y) anchor = item
        }
        if (!anchor) return
        pendingBatchAnchor = {
            sessionId: assistant.currentSessionId,
            key: String(anchor.turnData.key || ""),
            viewportOffset: anchor.y - transcript.contentY
        }
    }
    function restoreBatchAnchor() {
        const anchor = pendingBatchAnchor
        const ticket = batchRestoreTicket
        if (!anchor || !ticket || !ticket.alive) return
        if (manualScrollActive) return
        ticket.owner = chatWindow
        ticket.bridge = assistant
        ticket.list = transcript
        Qt.callLater(function() {
            if (!ticket.alive) return
            const owner = ticket.owner
            const bridge = ticket.bridge
            const list = ticket.list
            if (owner.batchRestoreTicket !== ticket
                    || bridge.currentSessionId !== anchor.sessionId
                    || owner.manualScrollActive || owner.followOutput) return
            const index = bridge.visibleTurnIndex(anchor.key)
            if (index < 0) return
            list.positionViewAtIndex(index, ListView.Beginning)
            Qt.callLater(function() {
                if (!ticket.alive) return
                const savedOwner = ticket.owner
                const savedBridge = ticket.bridge
                const savedList = ticket.list
                if (savedOwner.batchRestoreTicket !== ticket
                        || savedBridge.currentSessionId !== anchor.sessionId
                        || savedOwner.manualScrollActive || savedOwner.followOutput) return
                const currentIndex = savedBridge.visibleTurnIndex(anchor.key)
                const item = currentIndex >= 0 ? savedList.itemAtIndex(currentIndex) : null
                if (item) savedList.contentY = Math.max(
                    savedList.originY,
                    Math.min(savedOwner.endPosition(), item.y - anchor.viewportOffset))
                savedOwner.pendingBatchAnchor = null
                savedOwner.batchRestoreTicket = null
                ticket.alive = false
            })
        })
    }
    function maybeLoadOlderHistory() {
        if (!historyLoadGestureActive || assistant.historyLoading
                || assistant.historyProjectionPending || assistant.historyError
                || followOutput || !assistant.hasOlderHistory
                || transcript.contentY > transcript.originY + 2) return
        historyLoadGestureActive = false
        assistant.loadOlderHistory()
    }
    function scheduleCompletionUpdate() {
        const ticket = rootLifecycleTicket
        const owner = chatWindow
        Qt.callLater(function() {
            if (!ticket.alive) return
            owner.updateCompletion()
        })
    }
    function jumpToBottom() {
        cancelPendingScrollRestore()
        manualScrollIdle.stop()
        manualScrollDecision.stop()
        followTimer.stop()
        transcript.cancelFlick()
        manualScrollActive = false
        followOutput = true
        automaticScroll = true
        transcript.positionViewAtEnd()
        automaticScroll = false
        requestFollow()
    }
    function beginManualScroll(allowOlderHistory) {
        cancelPendingScrollRestore()
        manualScrollActive = true
        followOutput = false
        historyLoadGestureActive = allowOlderHistory === true
        manualScrollPreviousY = transcript.contentY
        followTimer.stop()
        manualScrollDecision.stop()
        manualScrollIdle.restart()
    }
    function endManualScroll() {
        if (!manualScrollActive || selectionDragging || transcript.moving || transcript.dragging || historyScrollBar.pressed) return
        manualScrollIdle.stop()
        // Wheel/scrollbar events can precede ListView's pending layout by one frame.
        manualScrollDecision.restart()
    }
    function finishManualScroll() {
        if (!manualScrollActive || selectionDragging || transcript.moving || transcript.dragging || historyScrollBar.pressed) return
        manualScrollActive = false
        followOutput = nearBottom()
        maybeLoadOlderHistory()
        if (pendingBatchAnchor && batchRestoreTicket && batchRestoreTicket.alive)
            restoreBatchAnchor()
        requestFollow()
    }
    function scrollHistoryBy(distance) {
        beginManualScroll(distance < 0)
        transcript.cancelFlick()
        transcript.contentY = Math.max(transcript.originY,
            Math.min(endPosition(), transcript.contentY + distance))
        endManualScroll()
    }
    function handleHistoryWheel(event) {
        const pixels = event.pixelDelta.y
        const angle = event.angleDelta.y
        if (pixels === 0 && angle === 0) {
            event.accepted = false
            return
        }
        // A blocking WheelHandler must apply the motion itself. Relying on event
        // propagation here leaves both Flickable and a long TextArea unscrolled.
        const distance = pixels !== 0 ? pixels
                         : angle / 120 * Math.max(historyFontMetrics.height, 16) * assistant.wheelScrollLines
        beginManualScroll(distance > 0)
        transcript.cancelFlick()
        manualScrollDecision.stop()
        transcript.contentY = Math.max(transcript.originY,
                                       Math.min(endPosition(), transcript.contentY - distance))
        manualScrollIdle.restart()
        event.accepted = true
    }
    function handleHistoryKey(event) {
        if ((event.modifiers & (Qt.ControlModifier | Qt.MetaModifier)) && event.key === Qt.Key_C) {
            if (copyHistorySelection()) { event.accepted = true; return }
        }
        const controlJump = event.modifiers === Qt.ControlModifier
                            && (event.key === Qt.Key_Home || event.key === Qt.Key_End)
        if (!controlJump && event.modifiers & (Qt.ControlModifier | Qt.ShiftModifier | Qt.AltModifier | Qt.MetaModifier))
            return
        let position = transcript.contentY
        if (event.key === Qt.Key_PageUp) position -= transcript.height * 0.85
        else if (event.key === Qt.Key_PageDown) position += transcript.height * 0.85
        else if (event.key === Qt.Key_Up) position -= 40
        else if (event.key === Qt.Key_Down) position += 40
        else if (event.key === Qt.Key_Home) position = transcript.originY
        else if (event.key === Qt.Key_End) position = endPosition()
        else return
        beginManualScroll(position < transcript.contentY)
        transcript.cancelFlick()
        transcript.contentY = Math.max(transcript.originY, Math.min(endPosition(), position))
        if (event.key === Qt.Key_End) {
            jumpToBottom()
        } else endManualScroll()
        event.accepted = true
    }

    // Selection spans virtualized paragraph editors without rebuilding one
    // enormous rich-text document. Retain selected plain text when rows unload.
    function registerSelection(surface) {
        selectionSurfaces = selectionSurfaces.concat([surface])
        if (selectionDragging) updateHistorySelection()
    }
    function unregisterSelection(surface) {
        selectionSurfaces = selectionSurfaces.filter(function(item) { return item !== surface })
    }
    function clearHistorySelection() {
        selectionUpdating = true
        for (const surface of selectionSurfaces) surface.editor.deselect()
        selectionUpdating = false
        selectedHistory = ({})
    }
    function selectionBelongsTo(surface, rootItem) {
        let item = surface
        while (item) {
            if (item === rootItem) return true
            item = item.parent
        }
        return false
    }
    function suspendTurnSelection(turnItem) {
        for (const surface of selectionSurfaces) {
            if (!selectionBelongsTo(surface, turnItem)) continue
            syncHistorySelection(surface, false)
            if (surface.selectionTicket) surface.selectionTicket.alive = false
            surface.selectionSuppressed = true
            selectionUpdating = true
            surface.editor.deselect()
            selectionUpdating = false
        }
    }
    function resumeTurnSelection(turnItem) {
        for (const surface of selectionSurfaces) {
            if (!selectionBelongsTo(surface, turnItem)) continue
            surface.selectionSuppressed = false
            scheduleSelectionRestore(surface)
        }
    }
    function selectionEntry(surface, keyOverride) {
        const top = surface.editor.mapToItem(transcript.contentItem, 0, 0)
        const key = keyOverride === undefined ? surface.selectionKey : keyOverride
        return {key: key, top: top.y, bottom: top.y + surface.editor.height,
                start: surface.editor.selectionStart, end: surface.editor.selectionEnd,
                text: surface.editor.selectedText}
    }
    function syncHistorySelection(surface, nativeChange, keyOverride) {
        const key = keyOverride === undefined ? surface.selectionKey : keyOverride
        if (!surface || !surface.editor || !key
                || selectionUpdating || selectionDragging) return
        const previous = selectedHistory[key]
        const record = selectionEntry(surface, key)
        let records = Object.assign({}, selectedHistory)
        // A keyboard selection belongs to the focused editor. A streaming
        // rewrite only refreshes its existing portion of a wider selection.
        if (nativeChange && surface.editor.activeFocus
                && (!previous || previous.start !== record.start || previous.end !== record.end)) {
            selectionUpdating = true
            for (const other of selectionSurfaces) {
                if (other !== surface) other.editor.deselect()
            }
            selectionUpdating = false
            records = ({})
        }
        if (record.text.length > 0 && (previous || surface.editor.activeFocus))
            records[key] = record
        else delete records[key]
        selectedHistory = records
    }
    function scheduleSelectionRestore(surface) {
        if (surface.selectionTicket) surface.selectionTicket.alive = false
        const owner = chatWindow
        const editor = surface.editor
        const key = String(surface.selectionKey || "")
        const ticket = {alive: true, owner: owner, surface: surface, editor: editor, key: key}
        surface.selectionTicket = ticket
        Qt.callLater(function() {
            if (!ticket.alive) return
            const currentOwner = ticket.owner
            const currentSurface = ticket.surface
            const currentEditor = ticket.editor
            if (currentSurface.selectionKey !== ticket.key) return
            const record = currentOwner.selectedHistory[ticket.key]
            if (record && currentSurface.selectionKey === ticket.key) {
                currentOwner.selectionUpdating = true
                currentEditor.select(record.start, record.end)
                currentOwner.selectionUpdating = false
                currentOwner.syncHistorySelection(currentSurface, false)
            }
        })
    }
    function beginHistorySelection(surface, x, y) {
        clearHistorySelection()
        surface.editor.forceActiveFocus()
        selectionAnchor = surface.editor.mapToItem(transcript.contentItem, x, y)
        selectionAnchorKey = surface.selectionKey
        selectionAnchorPosition = surface.editor.positionAt(x, y)
        selectionPointer = surface.editor.mapToItem(transcript, x, y)
        selectionDragging = true
        beginManualScroll()
        selectionScroll.start()
        updateHistorySelection()
    }
    function moveHistorySelection(surface, x, y) {
        selectionPointer = surface.editor.mapToItem(transcript, x, y)
        updateHistorySelection()
    }
    function updateHistorySelection() {
        if (!selectionDragging) return
        const endpoint = transcript.mapToItem(transcript.contentItem, selectionPointer.x, selectionPointer.y)
        const forward = endpoint.y > selectionAnchor.y
            || (endpoint.y === selectionAnchor.y && endpoint.x >= selectionAnchor.x)
        const first = forward ? selectionAnchor : endpoint
        const last = forward ? endpoint : selectionAnchor
        const records = Object.assign({}, selectedHistory)
        for (const key of Object.keys(records)) {
            if (records[key].bottom < first.y || records[key].top > last.y) delete records[key]
        }
        selectionUpdating = true
        for (const surface of selectionSurfaces) {
            const editor = surface.editor
            if (!editor.visible || editor.height <= 0) continue
            const point = editor.mapToItem(transcript.contentItem, 0, 0)
            if (point.y + editor.height < first.y || point.y > last.y) {
                editor.deselect()
                delete records[surface.selectionKey]
                continue
            }
            let start = first.y <= point.y ? 0
                : editor.positionAt(first.x - point.x, first.y - point.y)
            let end = last.y >= point.y + editor.height ? editor.length
                : editor.positionAt(last.x - point.x, last.y - point.y)
            if (surface.selectionKey === selectionAnchorKey) {
                if (forward) start = selectionAnchorPosition
                else end = selectionAnchorPosition
            }
            editor.select(Math.min(start, end), Math.max(start, end))
            const record = selectionEntry(surface)
            if (record.text.length > 0) records[surface.selectionKey] = record
            else delete records[surface.selectionKey]
        }
        selectionUpdating = false
        selectedHistory = records
    }
    function endHistorySelection() {
        selectionDragging = false
        selectionScroll.stop()
        manualScrollIdle.stop()
        manualScrollDecision.stop()
        manualScrollActive = false
    }
    function copyHistorySelection() {
        // Refresh live editors; retain text from virtualized, unloaded rows.
        for (const surface of selectionSurfaces) {
            if (selectedHistory[surface.selectionKey]) syncHistorySelection(surface, false)
        }
        const records = Object.values(selectedHistory).filter(function(record) { return record.text.length > 0 })
        records.sort(function(a, b) { return a.top - b.top })
        if (records.length === 0) return false
        assistant.copyText(records.map(function(record) { return record.text }).join("\n"))
        return true
    }

    component SelectionSurface: MouseArea {
        id: selectionSurface
        required property var editor
        property string selectionKey: ""
        property string registeredSelectionKey: ""
        property var selectionTicket: null
        property bool selectionSuppressed: false
        readonly property var selectionOwner: chatWindow
        anchors.fill: parent
        enabled: !selectionSuppressed
        acceptedButtons: Qt.LeftButton
        preventStealing: true
        hoverEnabled: true
        cursorShape: editor.linkAt(mouseX, mouseY).length > 0 ? Qt.PointingHandCursor : Qt.IBeamCursor
        onPressed: function(mouse) {
            if (editor.linkAt(mouse.x, mouse.y).length > 0) {
                chatWindow.clearHistorySelection()
                mouse.accepted = false
                return
            }
            chatWindow.beginHistorySelection(selectionSurface, mouse.x, mouse.y)
        }
        onPositionChanged: function(mouse) {
            if (pressed && chatWindow.selectionDragging)
                chatWindow.moveHistorySelection(selectionSurface, mouse.x, mouse.y)
        }
        onReleased: chatWindow.endHistorySelection()
        onCanceled: chatWindow.endHistorySelection()
        onSelectionKeyChanged: {
            if (!registeredSelectionKey || registeredSelectionKey === selectionKey) return
            const owner = selectionOwner
            const oldKey = registeredSelectionKey
            // updateBlock saves the old key before replacing text. This second
            // sync covers editor reuse paths where only the key binding changed.
            if (editor.selectedText.length > 0 && !owner.selectedHistory[oldKey])
                owner.syncHistorySelection(selectionSurface, false, oldKey)
            owner.selectionUpdating = true
            editor.deselect()
            owner.selectionUpdating = false
            registeredSelectionKey = selectionKey
            owner.scheduleSelectionRestore(selectionSurface)
        }
        onSelectionSuppressedChanged: if (selectionSuppressed) {
            const owner = selectionOwner
            if (editor.selectedText.length > 0)
                owner.syncHistorySelection(selectionSurface, false, registeredSelectionKey)
            if (selectionTicket) selectionTicket.alive = false
            owner.selectionUpdating = true
            editor.deselect()
            owner.selectionUpdating = false
        }
        onDoubleClicked: function(mouse) {
            chatWindow.endHistorySelection()
            chatWindow.clearHistorySelection()
            editor.cursorPosition = editor.positionAt(mouse.x, mouse.y)
            editor.selectWord()
            const records = ({})
            records[selectionKey] = chatWindow.selectionEntry(selectionSurface)
            chatWindow.selectedHistory = records
        }
        Component.onCompleted: {
            if (selectionKey.length === 0) selectionKey = "selection/" + (++chatWindow.selectionSequence)
            registeredSelectionKey = selectionKey
            chatWindow.registerSelection(selectionSurface)
            chatWindow.scheduleSelectionRestore(selectionSurface)
        }
        Component.onDestruction: {
            const ticket = selectionTicket
            if (ticket) ticket.alive = false
            const owner = selectionOwner
            try {
                if (registeredSelectionKey.length > 0 && editor.selectedText.length > 0)
                    owner.syncHistorySelection(selectionSurface, false, registeredSelectionKey)
                owner.unregisterSelection(selectionSurface)
            } catch (error) {}
        }
        Connections {
            target: selectionSurface.editor
            function onSelectedTextChanged() {
                chatWindow.syncHistorySelection(selectionSurface, true)
            }
        }
        PreviewCursor {
            target: selectionSurface
            normalShape: selectionSurface.cursorShape
            active: selectionSurface.containsMouse && assistant.isPreviewableImageLink(
                selectionSurface.editor.linkAt(selectionSurface.mouseX, selectionSurface.mouseY))
        }
    }

    Timer {
        id: selectionScroll
        interval: 30
        repeat: true
        onTriggered: {
            const y = chatWindow.selectionPointer.y
            const delta = y < 24 ? -Math.min(30, 4 + (24 - y) / 3)
                : y > transcript.height - 24 ? Math.min(30, 4 + (y - transcript.height + 24) / 3) : 0
            if (delta === 0) return
            transcript.contentY = Math.max(transcript.originY,
                Math.min(chatWindow.endPosition(), transcript.contentY + delta))
            chatWindow.updateHistorySelection()
        }
    }

    FontMetrics {
        id: historyFontMetrics
        font: chatWindow.bodyFont
    }
    Timer {
        id: followTimer
        interval: 48
        repeat: false
        onTriggered: chatWindow.followLatest()
    }
    Timer {
        id: manualScrollIdle
        interval: 120
        repeat: false
        onTriggered: chatWindow.endManualScroll()
    }
    Timer {
        id: manualScrollDecision
        interval: 16
        repeat: false
        onTriggered: chatWindow.finishManualScroll()
    }
    Timer {
        id: sessionRestoreTimer
        interval: 80
        repeat: false
        onTriggered: chatWindow.restoreSessionScroll(true)
    }
    Timer {
        id: scrollBarSyncTimer
        interval: 0
        repeat: false
        onTriggered: chatWindow.syncHistoryScrollBar()
    }
    function reasoningName(value) {
        return ({auto: "自动", none: "无推理", minimal: "极轻量", low: "轻量",
                 medium: "标准", high: "深入", xhigh: "更深入", max: "最高", ultra: "极高",
                 thinking_enabled: "开启思考", thinking_disabled: "关闭思考"})[value] || value
    }
    function sourceName(source) {
        const names = {main: "看板娘", analyst: "分析助手", summary: "结果整理",
                       execute_design: "设计助手", execute_windows: "窗口助手",
                       execute_simulation: "仿真助手", execute_correction: "修正板助手"}
        return names[source] || source || "看板娘"
    }
    function toggleThought(key) {
        const values = Object.assign({}, expandedThoughts)
        values[key] = !values[key]
        expandedThoughts = values
    }

    component RichBody: Column {
        id: richBody
        property var document: null
        property string fallbackText: ""
        property string fallbackHtml: ""
        property bool suspended: false
        property color textColor: chatWindow.ink
        readonly property bool renderedDocumentReady: renderedBlocks.count > 0
        // Only nearby blocks own a QTextDocument. The lightweight wrappers keep
        // measured heights when their text editor leaves the visible region.
        readonly property real viewportTop: {
            const scrollPosition = transcript.contentY
            return mapToItem(transcript, 0, 0).y
        }
        spacing: 0
        Repeater {
            id: renderedBlocks
            // Keep per-block height placeholders while a ListView delegate is
            // pooled. Only the TextEdit Loader below is suspended, so pooling
            // does not collapse this Column and invalidate ListView geometry.
            model: richBody.document ? richBody.document.blocks : null
            delegate: Item {
                id: blockSlot
                required property var blockData
                property real measuredHeight: -1
                property bool pinned: false
                property bool suppressPinUpdates: false
                property bool suppressHeightUpdates: false
                property bool heightCommitPending: false
                property var heightCommitTicket: null
                function restoreHeight() {
                    if (!blockData || !richBody.document) { measuredHeight = -1; return }
                    measuredHeight = richBody.document.measuredBlockHeight(
                        String(blockData.key || ""), String(blockData.layoutKey || ""), Math.round(width))
                }
                function rememberHeight(value) {
                    if (!(value > 0)) return
                    if (Math.abs(measuredHeight - value) < 1.0) return
                    measuredHeight = value
                    if (richBody.document && blockData)
                        richBody.document.rememberBlockHeight(String(blockData.key || ""),
                            String(blockData.layoutKey || ""), Math.round(width), value)
                }
                function scheduleHeightCommit() {
                    if (suppressHeightUpdates || heightCommitPending) return
                    heightCommitPending = true
                    const expectedKey = String(blockData.key || "")
                    const expectedLayoutKey = String(blockData.layoutKey || "")
                    const expectedWidth = Math.round(width)
                    if (heightCommitTicket) heightCommitTicket.alive = false
                    const ticket = {alive: true, slot: blockSlot, loader: blockLoader,
                                    body: richBody, key: expectedKey,
                                    layoutKey: expectedLayoutKey, width: expectedWidth}
                    heightCommitTicket = ticket
                    Qt.callLater(function() {
                        if (!ticket.alive) return
                        const slot = ticket.slot
                        const loader = ticket.loader
                        const body = ticket.body
                        slot.heightCommitPending = false
                        slot.heightCommitTicket = null
                        if (body.suspended || !loader.active || !loader.item) {
                            ticket.alive = false
                            return
                        }
                        if (String(slot.blockData.key || "") !== ticket.key
                                || String(slot.blockData.layoutKey || "") !== ticket.layoutKey
                                || Math.round(slot.width) !== ticket.width) {
                            slot.scheduleHeightCommit()
                            ticket.alive = false
                            return
                        }
                        slot.rememberHeight(loader.item.contentHeight)
                        ticket.alive = false
                    })
                }
                readonly property real estimatedHeight: {
                    const characters = Math.max(1, Math.floor(width / Math.max(1, historyFontMetrics.averageCharacterWidth)))
                    const lines = blockData.lineLengths || [String(blockData.text || "").length]
                    let count = 0
                    for (let index = 0; index < lines.length; ++index)
                        count += Math.max(1, Math.ceil(lines[index] / characters))
                    return Math.max(historyFontMetrics.height, count * historyFontMetrics.height)
                }
                readonly property bool nearby: richBody.viewportTop + y + estimatedHeight >= -transcript.height
                    && richBody.viewportTop + y <= transcript.height * 2
                width: richBody.width
                // Retain a positive height while Loader creates/destroys its
                // text document. ListView owns its scroll anchor; per-block
                // writes to contentY would apply a second, conflicting offset.
                height: measuredHeight >= 0 ? measuredHeight : estimatedHeight
                onWidthChanged: restoreHeight()
                onBlockDataChanged: restoreHeight()
                Component.onCompleted: restoreHeight()
                Component.onDestruction: {
                    if (heightCommitTicket) heightCommitTicket.alive = false
                    heightCommitTicket = null
                }
                Loader {
                    id: blockLoader
                    objectName: "chatBlockLoader"
                    anchors.left: parent.left; anchors.right: parent.right
                    active: !richBody.suspended && richBody.visible
                        && (blockSlot.nearby || blockSlot.pinned)
                    sourceComponent: TextEdit {
                        id: blockText
                        readonly property var blockData: blockSlot.blockData
                        property string renderedKey: ""
                        property string renderedSource: ""
                        property bool renderedRich: false
                        width: blockSlot.width
                        height: contentHeight
                        onContentHeightChanged: blockSlot.scheduleHeightCommit()
                        textFormat: TextEdit.PlainText
                        font: chatWindow.bodyFont
                        readOnly: true; selectByMouse: false; persistentSelection: true
                        wrapMode: TextEdit.Wrap
                        color: richBody.textColor
                        selectionColor: chatWindow.palette.active.highlight
                        selectedTextColor: chatWindow.palette.active.highlightedText
                        function syncVirtualizationPin() {
                            // Ignore teardown notifications and transient selection edits.
                            if (!blockLoader.active || blockSlot.suppressPinUpdates) return
                            blockSlot.pinned = activeFocus || selectionStart !== selectionEnd
                        }
                        function updateBlock() {
                            const key = String(blockData.key || "")
                            const rich = blockData.rich === true
                            const source = String((rich ? blockData.html : blockData.text) || "")
                            if (key === renderedKey && rich === renderedRich && source === renderedSource) return
                            const oldSource = renderedSource
                            const oldRich = renderedRich
                            const sourceChanged = rich !== oldRich || source !== oldSource
                            const changedKey = key !== renderedKey
                            if (changedKey && renderedKey.length > 0 && blockSelection)
                                chatWindow.syncHistorySelection(blockSelection, false,
                                    blockSelection.registeredSelectionKey)
                            const priorSelection = changedKey ? null
                                : chatWindow.selectedHistory[blockSelection.selectionKey]
                             const start = selectionStart
                             const end = selectionEnd
                            const position = cursorPosition
                            const wasUpdating = chatWindow.selectionUpdating
                            blockSlot.suppressPinUpdates = true
                            blockSlot.suppressHeightUpdates = true
                            chatWindow.selectionUpdating = true
                            // Only append to the same plain tail. Completed rich blocks,
                            // rewritten source and appearance changes replace one block.
                            if (!changedKey && !rich && !oldRich
                                    && source.length >= oldSource.length && source.indexOf(oldSource) === 0) {
                                insert(oldSource.length, source.substring(oldSource.length))
                            } else {
                                textFormat = rich ? TextEdit.RichText : TextEdit.PlainText
                                text = source
                            }
                            renderedKey = key
                            renderedSource = source
                            renderedRich = rich
                            let restoredPriorSelection = false
                            if (changedKey) {
                                deselect()
                            } else if (start !== end && !rich && !oldRich
                                    && source.indexOf(oldSource) === 0) {
                                if (position === start) select(Math.min(end, length), Math.min(start, length))
                                else select(Math.min(start, length), Math.min(end, length))
                            } else cursorPosition = Math.min(position, length)
                            if (priorSelection && priorSelection.text.length > 0 && sourceChanged) {
                                const plainSource = String(blockData.text || "")
                                const selectionStartInSource = plainSource.indexOf(priorSelection.text)
                                if (selectionStartInSource >= 0 && !rich) {
                                    select(selectionStartInSource,
                                           selectionStartInSource + priorSelection.text.length)
                                    restoredPriorSelection = true
                                } else {
                                    deselect()
                                    const retained = Object.assign({}, chatWindow.selectedHistory)
                                    retained[blockSelection.selectionKey] = priorSelection
                                    chatWindow.selectedHistory = retained
                                }
                            }
                            chatWindow.selectionUpdating = wasUpdating
                            if (!priorSelection || restoredPriorSelection || !sourceChanged)
                                chatWindow.syncHistorySelection(blockSelection, false)
                            if (!rich && richBody.document) richBody.document.requestRichBlock(key)
                            blockSlot.suppressPinUpdates = false
                            blockSlot.suppressHeightUpdates = false
                            blockSlot.scheduleHeightCommit()
                            syncVirtualizationPin()
                        }
                        onBlockDataChanged: updateBlock()
                        onActiveFocusChanged: syncVirtualizationPin()
                        onSelectedTextChanged: syncVirtualizationPin()
                        Component.onCompleted: {
                            updateBlock()
                            syncVirtualizationPin()
                        }
                        Keys.onPressed: function(event) { chatWindow.handleHistoryKey(event) }
                        onLinkActivated: function(link) { assistant.activateLink(link) }
                        onLinkHovered: function(link) {
                            blockCursor.normalShape = link.length > 0 ? Qt.PointingHandCursor : Qt.IBeamCursor
                            blockCursor.active = assistant.isPreviewableImageLink(link)
                        }
                        PreviewCursor { id: blockCursor; target: blockText }
                        SelectionSurface {
                            id: blockSelection
                            editor: blockText
                            selectionKey: (richBody.document ? richBody.document.identityKey : "")
                                + "/" + String(blockText.blockData.key || "")
                        }
                    }
                }
            }
        }
        TextEdit {
            id: legacyText
            objectName: "chatBodyFallbackText"
            width: richBody.width
            height: visible ? contentHeight : 0
            visible: !richBody.renderedDocumentReady
                && !richBody.suspended
                && richBody.visible
                && (richBody.fallbackHtml.length > 0 || richBody.fallbackText.length > 0)
            // Do not keep a hidden or pooled fallback QTextDocument laying
            // out a second copy of the source while block placeholders remain.
            text: !richBody.suspended && richBody.visible
                ? (richBody.fallbackHtml || richBody.fallbackText) : ""
            textFormat: richBody.fallbackHtml ? TextEdit.RichText : TextEdit.PlainText
            font: chatWindow.bodyFont
            readOnly: true; selectByMouse: false; persistentSelection: true
            wrapMode: TextEdit.Wrap
            color: richBody.textColor
            selectionColor: chatWindow.palette.active.highlight
            selectedTextColor: chatWindow.palette.active.highlightedText
            Keys.onPressed: function(event) { chatWindow.handleHistoryKey(event) }
            onLinkActivated: function(link) { assistant.activateLink(link) }
            onLinkHovered: function(link) {
                legacyCursor.normalShape = link.length > 0 ? Qt.PointingHandCursor : Qt.IBeamCursor
                legacyCursor.active = assistant.isPreviewableImageLink(link)
            }
            PreviewCursor { id: legacyCursor; target: legacyText }
            SelectionSurface { editor: legacyText }
        }
    }

    component ThoughtCard: Rectangle {
        id: card
        required property string thoughtKey
        required property string sourceName
        required property string contentText
        property bool suspended: false
        property var document: null
        property bool finished: true
        property bool interrupted: false
        readonly property bool expanded: chatWindow.expandedThoughts[thoughtKey] === true
        visible: contentText.length > 0
        height: visible ? Math.max((expanded && document ? thoughtBody.implicitHeight : thoughtText.implicitHeight) + 16, thoughtControls.height + 12) : 0
        radius: 6
        color: chatWindow.themeColors ? chatWindow.themeColors.colorSurfaceRaised
                                      : Qt.rgba(chatWindow.palette.active.alternateBase.r,
                                                chatWindow.palette.active.alternateBase.g,
                                                chatWindow.palette.active.alternateBase.b, 0.45)
        border.width: 1
        border.color: chatWindow.themeColors ? chatWindow.themeColors.colorBorder
                                            : Qt.rgba(chatWindow.palette.active.mid.r,
                                                      chatWindow.palette.active.mid.g,
                                                      chatWindow.palette.active.mid.b, 0.45)
        HoverHandler { id: thoughtHover }
        ThemeControls.ThemeLabel {
            id: thoughtText
            visible: !card.expanded || !card.document
            x: 9; y: 8
            width: Math.max(0, card.width - thoughtControls.width - 26)
            text: card.expanded ? card.contentText : card.contentText.substring(0, 1500)
            textFormat: Text.PlainText
            color: chatWindow.thoughtInk
            font: chatWindow.bodyFont
            wrapMode: Text.Wrap
            maximumLineCount: card.expanded ? 2147483647 : 3
            elide: Text.ElideRight
            TapHandler { onTapped: chatWindow.toggleThought(card.thoughtKey) }
        }
        RichBody {
            id: thoughtBody
            x: 9; y: 8
            width: Math.max(0, card.width - thoughtControls.width - 26)
            suspended: card.suspended
            visible: card.expanded && card.document !== null
            document: visible ? card.document : null
            textColor: chatWindow.thoughtInk
        }
        Row {
            id: thoughtControls
            anchors.right: parent.right; anchors.rightMargin: 5
            y: 4
            spacing: 2
            ThemeControls.ThemeButton { flat: true;
                text: "复制"
                opacity: thoughtHover.hovered || hovered || activeFocus ? 1 : 0
                topPadding: 3; bottomPadding: 3; leftPadding: 5; rightPadding: 5
                palette.buttonText: chatWindow.quietInk
                onClicked: assistant.copyText(card.contentText)
                ToolTip.visible: hovered; ToolTip.text: "复制完整思考文字，包括折叠部分"
            }
            ThemeControls.ThemeButton { flat: true;
                text: card.expanded ? "收起" : "展开"
                topPadding: 3; bottomPadding: 3; leftPadding: 5; rightPadding: 5
                palette.buttonText: chatWindow.quietInk
                onClicked: chatWindow.toggleThought(card.thoughtKey)
                ToolTip.visible: hovered
                ToolTip.text: card.sourceName + (card.interrupted ? " · 已中断" : card.finished ? "" : " · 思考中")
            }
        }
    }
    function updateCompletion() {
        const before = draft.text.substring(0, draft.cursorPosition)
        const match = before.match(/(?:^|\s)(@(?:\{[^}]*|[^\s]*)|\/[a-z]*)$/i)
        if (!match) { completionPopup.close(); return }
        completionStart = draft.cursorPosition - match[1].length
        completionRows = assistant.completions(match[1])
        if (completionRows.length) {
            completionList.currentIndex = 0
            completionPopup.open()
        } else completionPopup.close()
    }
    function acceptCompletion(index) {
        if (index < 0 || index >= completionRows.length) return
        const item = completionRows[index]
        const end = draft.cursorPosition
        const text = draft.text.substring(0, completionStart) + item.insert + draft.text.substring(end)
        draft.text = text
        draft.cursorPosition = completionStart + item.insert.length
        completionPopup.close()
        draft.forceActiveFocus()
        if (item.insert.startsWith("@") && !item.insert.endsWith(" ")) updateCompletion()
    }

    ColumnLayout {
        id: page
        anchors.fill: parent
        anchors.margins: 14
        anchors.topMargin: chatChrome.barHeight
        spacing: 7

        RowLayout {
            Layout.fillWidth: true
            ThemeControls.ThemeButton { objectName: "chatSessionListButton"; text: "会话列表"; onClicked: sessionDrawer.open() }
            ThemeControls.ThemeButton { objectName: "chatNewSessionButton"; flat: true; text: "新会话"; enabled: !assistant.busy; onClicked: chatWindow.startNewSession() }
            Item { Layout.fillWidth: true }
            ThemeControls.ThemeButton { flat: true; text: "复制对话"; onClicked: copyMenu.open() }
            ThemeControls.ThemeButton { flat: true; text: "Token"; onClicked: assistant.showTokenStatistics() }
        }

        Item {
            id: transcriptHost
            Layout.fillWidth: true
            Layout.fillHeight: true

            ListView {
                id: transcript
                objectName: "chatTranscript"
                anchors.fill: parent
                clip: true
                reuseItems: true
                model: assistant.turnModel
                cacheBuffer: height
                spacing: 18
                boundsBehavior: Flickable.StopAtBounds
                focus: true
                activeFocusOnTab: true
                Keys.onPressed: function(event) { chatWindow.handleHistoryKey(event) }
                Item {
                    // Stay in viewport coordinates, above rich text but with no mouse
                    // press handler: selecting text and clicking links still work.
                    parent: transcript
                    anchors.fill: parent
                    z: 10
                    WheelHandler {
                        target: null
                        blocking: true
                        orientation: Qt.Vertical
                        acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                        onWheel: function(event) { chatWindow.handleHistoryWheel(event) }
                    }
                }
                ThemeControls.ThemeButton { flat: true;
                    parent: transcript
                    anchors.right: parent.right
                    anchors.rightMargin: 28
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: 12
                    z: 30
                    text: "↓ 跳到底部"
                    visible: !chatWindow.followOutput && !chatWindow.nearBottom()
                    onClicked: chatWindow.jumpToBottom()
                    ToolTip.visible: hovered
                    ToolTip.text: "回到最新消息并继续跟随输出"
                    background: Rectangle {
                        radius: 18
                        color: parent.hovered ? chatWindow.palette.active.highlight : chatWindow.palette.active.button
                        border.width: 1
                        border.color: chatWindow.palette.active.mid
                    }
                }
                RowLayout {
                    parent: transcript
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 8
                    z: 31
                    visible: assistant.historyError.length === 0
                        && (assistant.historyLoading || assistant.historyProjectionPending)
                    ThemeControls.ThemeLabel {
                        text: assistant.historyLoading ? "正在加载对话历史…" : "正在加载更早的对话…"
                        color: chatWindow.quietInk
                        background: Rectangle {
                            color: chatWindow.themeColors ? chatWindow.themeColors.colorSurface
                                                          : chatWindow.palette.active.base
                            opacity: 0.94
                            radius: 6
                        }
                    }
                }
                RowLayout {
                    parent: transcript
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 8
                    z: 31
                    visible: assistant.historyError.length > 0
                    ThemeControls.ThemeLabel {
                        Layout.fillWidth: true
                        text: assistant.historyError
                        color: chatWindow.themeColors ? chatWindow.themeColors.colorError : "#b3261e"
                        wrapMode: Text.Wrap
                        background: Rectangle {
                            color: chatWindow.themeColors ? chatWindow.themeColors.colorSurface
                                                          : chatWindow.palette.active.base
                            opacity: 0.94
                            radius: 6
                        }
                    }
                    ThemeControls.ThemeButton {
                        flat: true
                        text: "重试"
                        enabled: !assistant.historyLoading
                        onClicked: assistant.retryHistoryLoad()
                    }
                }
                onMovementEnded: {
                    if (chatWindow.manualScrollActive) chatWindow.endManualScroll()
                    else chatWindow.requestFollow()
                }
                onDraggingChanged: if (dragging) chatWindow.beginManualScroll()
                onContentHeightChanged: {
                    chatWindow.scheduleHistoryScrollBarSync()
                    if (chatWindow.pendingScrollAnchor)
                        chatWindow.schedulePendingSessionAnchorRestore()
                    else chatWindow.requestFollow()
                }
                onContentYChanged: {
                    if (chatWindow.manualScrollActive
                            && Math.abs(contentY - chatWindow.manualScrollPreviousY) > 0.01) {
                        chatWindow.historyLoadGestureActive = contentY < chatWindow.manualScrollPreviousY
                            && !chatWindow.followOutput
                        chatWindow.manualScrollPreviousY = contentY
                    }
                    chatWindow.scheduleHistoryScrollBarSync()
                    chatWindow.maybeLoadOlderHistory()
                }
                onOriginYChanged: chatWindow.scheduleHistoryScrollBarSync()
                onHeightChanged: {
                    chatWindow.scheduleHistoryScrollBarSync()
                    chatWindow.requestFollow()
                }
                onCountChanged: {
                    chatWindow.scheduleHistoryScrollBarSync()
                    if (chatWindow.pendingScrollAnchor)
                        chatWindow.schedulePendingSessionAnchorRestore()
                    else chatWindow.requestFollow()
                }
                delegate: Column {
                    id: turn
                    required property var turnData
                    required property int index
                    readonly property var modelData: turnData
                    property bool delegatePooled: false
                    ListView.onPooled: {
                        chatWindow.suspendTurnSelection(turn)
                        delegatePooled = true
                    }
                    ListView.onReused: {
                        delegatePooled = false
                        chatWindow.resumeTurnSelection(turn)
                    }
                    readonly property var imageFiles: (modelData.files || []).filter(function(file) {
                        return file.image && file.valid
                    })
                    readonly property var otherFiles: (modelData.files || []).filter(function(file) {
                        return !file.image || !file.valid
                    })
                    onYChanged: {
                        if (chatWindow.pendingScrollAnchor
                                && String(chatWindow.pendingScrollAnchor.key || "").length > 0
                                && String(chatWindow.pendingScrollAnchor.key)
                                    === String(turn.turnData.key || ""))
                            chatWindow.schedulePendingSessionAnchorRestore(turn)
                    }
                    onHeightChanged: {
                        if (chatWindow.pendingScrollAnchor
                                && String(chatWindow.pendingScrollAnchor.key || "").length > 0
                                && String(chatWindow.pendingScrollAnchor.key)
                                    === String(turn.turnData.key || ""))
                            chatWindow.schedulePendingSessionAnchorRestore(turn)
                        if (index === transcript.count - 1) chatWindow.requestFollow()
                    }
                    width: transcript.width - 16
                    spacing: 7
                    HoverHandler { id: turnHover }

                    RowLayout {
                        width: turn.width
                        spacing: 8
                        Image {
                            source: assistant.headUrl
                            Layout.preferredWidth: 28; Layout.preferredHeight: 28
                            visible: turn.modelData.role === "assistant"
                            fillMode: Image.PreserveAspectCrop
                            mipmap: true
                            sourceSize.width: 56; sourceSize.height: 56
                        }
                        ThemeControls.ThemeLabel {
                            text: turn.modelData.name
                            font.bold: true
                            color: chatWindow.nameInk
                            Layout.preferredHeight: 28; verticalAlignment: Text.AlignVCenter
                        }
                        BusyIndicator { running: turn.modelData.running; visible: running; Layout.preferredWidth: 24; Layout.preferredHeight: 24 }
                        Item { Layout.fillWidth: true }
                        ThemeControls.ThemeButton { flat: true;
                            text: "复制"
                            opacity: turnHover.hovered || hovered || activeFocus ? 1 : 0
                            onClicked: turn.modelData.role === "assistant"
                                ? assistant.copyTurn(turn.modelData.key)
                                : assistant.copyText(turn.modelData.text || "")
                            ToolTip.visible: hovered
                            ToolTip.text: turn.modelData.role === "assistant" ? "复制本轮完整输出与思考" : "复制用户输入"
                        }
                    }

                    Repeater {
                        model: turn.modelData.hasSteps === true ? [] : turn.modelData.thoughts || []
                        delegate: ThoughtCard {
                            id: thought
                            required property var modelData
                            thoughtKey: turn.modelData.key + "/" + modelData.stepId
                            sourceName: chatWindow.sourceName(modelData.source)
                            contentText: modelData.text || ""
                            document: modelData.document || null
                            finished: modelData.done === true
                            interrupted: modelData.interrupted === true
                            suspended: turn.delegatePooled
                            width: turn.width
                        }
                    }

                    Repeater {
                        id: modelSteps
                        model: turn.modelData.stepModel || null
                        delegate: Column {
                            id: modelStep
                            required property var stepData
                            readonly property var modelData: stepData
                            width: turn.width
                            spacing: 5
                            ThoughtCard {
                                width: parent.width
                                suspended: turn.delegatePooled
                                thoughtKey: turn.modelData.key + "/" + modelStep.modelData.stepId
                                sourceName: chatWindow.sourceName(modelStep.modelData.source)
                                contentText: modelStep.modelData.thinking || ""
                                document: modelStep.modelData.thoughtDocument || null
                                finished: modelStep.modelData.thinkingDone === true
                                interrupted: ["failed", "cancelled"].indexOf(modelStep.modelData.status) >= 0
                            }
                            Column {
                                width: parent.width
                                visible: modelStep.modelData.hasOutput === true
                                spacing: 5
                                HoverHandler { id: outputHover }
                                RowLayout {
                                    width: parent.width
                                    ThemeControls.ThemeLabel {
                                        Layout.fillWidth: true
                                        text: chatWindow.sourceName(modelStep.modelData.source) + " · 输出"
                                        color: chatWindow.quietInk
                                    }
                                    ThemeControls.ThemeButton { flat: true;
                                        text: "复制"
                                        opacity: outputHover.hovered || hovered || activeFocus ? 1 : 0
                                        onClicked: assistant.copyText(modelStep.modelData.document
                                            ? modelStep.modelData.document.sourceText : modelStep.modelData.output || "")
                                        ToolTip.visible: hovered; ToolTip.text: "复制本次完整输出"
                                    }
                                }
                                RichBody {
                                    width: parent.width
                                    suspended: turn.delegatePooled
                                    document: modelStep.modelData.document || null
                                    fallbackText: turn.delegatePooled ? "" : (modelStep.modelData.output || "")
                                    fallbackHtml: turn.delegatePooled ? "" : (modelStep.modelData.html || "")
                                }
                            }
                        }
                    }

                    RichBody {
                        id: turnBody
                        objectName: "chatTurnBody_" + turn.index
                        width: turn.width
                        suspended: turn.delegatePooled
                        visible: turn.modelData.hasSteps !== true
                            && (turnBody.renderedDocumentReady
                                || fallbackHtml.length > 0 || fallbackText.length > 0)
                        document: turn.modelData.document || (typeof assistant.documentForTurn === "function"
                            ? assistant.documentForTurn(turn.modelData.key, turn.modelData.documentStyle || "")
                            : null)
                        fallbackText: turn.delegatePooled ? "" : (turn.modelData.text
                            || (turnBody.document && !turnBody.renderedDocumentReady
                                ? turnBody.document.sourceText : ""))
                        fallbackHtml: turn.delegatePooled ? "" : (turn.modelData.html || "")
                    }

                    Repeater {
                        model: turn.modelData.interjections || []
                        delegate: Rectangle {
                            id: interjection
                            required property var modelData
                            width: turn.width
                            height: interjectionBody.implicitHeight + 16
                            radius: 6
                            color: chatWindow.palette.active.alternateBase
                            border.color: chatWindow.palette.active.mid
                            HoverHandler { id: interjectionHover }
                            Column {
                                id: interjectionBody
                                x: 9; y: 8; width: parent.width - 18
                                spacing: 5
                                RowLayout {
                                    width: parent.width
                                    ThemeControls.ThemeLabel {
                                        text: (interjection.modelData.name || "用户") + " · 插话"
                                        font.bold: true
                                        color: chatWindow.nameInk
                                        Layout.fillWidth: true
                                    }
                                    ThemeControls.ThemeLabel {
                                        text: interjection.modelData.stateLabel || ""
                                        color: chatWindow.quietInk
                                    }
                                    ThemeControls.ThemeButton { flat: true;
                                        text: "复制"
                                        opacity: interjectionHover.hovered || hovered || activeFocus ? 1 : 0
                                        onClicked: assistant.copyText(interjection.modelData.text || "")
                                    }
                                    ThemeControls.ThemeButton { flat: true;
                                        text: "撤回"
                                        visible: interjection.modelData.canWithdraw === true
                                        onClicked: assistant.withdrawSteering(interjection.modelData.id)
                                        ToolTip.visible: hovered; ToolTip.text: "尚未应用时撤回，并恢复输入和附件；当前任务继续。"
                                    }
                                }
                                RichBody {
                                    width: parent.width
                                    suspended: turn.delegatePooled
                                    document: interjection.modelData.document || null
                                    fallbackText: turn.delegatePooled ? "" : (interjection.modelData.text || "")
                                    fallbackHtml: turn.delegatePooled ? "" : (interjection.modelData.html || "")
                                    visible: document ? document.hasText
                                                      : fallbackText.length > 0 || fallbackHtml.length > 0
                                }
                                Flow {
                                    width: parent.width
                                    spacing: 4
                                    Repeater {
                                        model: interjection.modelData.files || []
                                        delegate: ThemeControls.ThemeButton { flat: true;
                                            required property var modelData
                                            text: modelData.name || "引用文件"
                                            width: Math.min(interjectionBody.width, implicitWidth)
                                            enabled: modelData.valid !== false
                                            onClicked: assistant.previewFile(modelData.path)
                                        }
                                    }
                                }
                            }
                        }
                    }

                    Flow {
                        width: turn.width
                        visible: turn.imageFiles.length > 0
                        spacing: 8
                        Repeater {
                            model: turn.imageFiles
                            delegate: Rectangle {
                                id: fileTile
                                required property var modelData
                                width: Math.min(turn.width, 144)
                                height: tileContent.implicitHeight + 12
                                radius: 5
                                color: chatWindow.palette.active.alternateBase
                                border.color: chatWindow.palette.active.mid
                                Column {
                                    id: tileContent
                                    x: 6; y: 6; width: parent.width - 12
                                    spacing: 4
                                    Image {
                                        width: parent.width
                                        height: 72
                                        source: fileTile.modelData.url
                                        fillMode: Image.PreserveAspectFit
                                        asynchronous: true; mipmap: true
                                        sourceSize.width: 288; sourceSize.height: 144
                                    }
                                    ThemeControls.ThemeLabel {
                                        width: parent.width
                                        text: fileTile.modelData.name
                                        wrapMode: Text.WrapAnywhere
                                        maximumLineCount: 2
                                        elide: Text.ElideRight
                                        color: chatWindow.nameInk
                                    }
                                }
                                TapHandler {
                                    onTapped: assistant.previewFile(fileTile.modelData.path)
                                }
                                ToolTip.visible: fileHover.hovered
                                ToolTip.text: modelData.reference || modelData.name
                                HoverHandler { id: fileHover }
                                PreviewCursor {
                                    target: fileTile
                                    active: fileHover.hovered
                                }
                            }
                        }
                    }

                    Flow {
                        width: turn.width
                        visible: turn.otherFiles.length > 0
                        spacing: 6
                        Repeater {
                            model: turn.otherFiles
                            delegate: ThemeControls.ThemeButton { flat: true;
                                id: fileNameButton
                                required property var modelData
                                width: Math.min(turn.width, implicitWidth)
                                padding: 4
                                text: modelData.name || "文件"
                                enabled: modelData.valid !== false
                                onClicked: assistant.previewFile(modelData.path)
                                contentItem: ThemeControls.ThemeLabel {
                                    text: fileNameButton.text
                                    font: fileNameButton.font
                                    color: fileNameButton.enabled ? chatWindow.nameInk : chatWindow.quietInk
                                    elide: Text.ElideRight
                                    verticalAlignment: Text.AlignVCenter
                                }
                                background: Rectangle {
                                    radius: 3
                                    color: fileNameButton.hovered ? chatWindow.palette.active.alternateBase : "transparent"
                                }
                                ToolTip.visible: hovered
                                ToolTip.text: modelData.valid !== false ? (modelData.reference || modelData.name)
                                    : modelData.error || "文件未授权或已移除"
                                HoverHandler {
                                    cursorShape: fileNameButton.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                                }
                            }
                        }
                    }

                    ThemeControls.ThemeLabel {
                        visible: turn.modelData.running
                        width: turn.width
                        text: turn.modelData.progress
                        wrapMode: Text.Wrap
                        color: chatWindow.quietInk
                    }
                    ThemeControls.ThemeLabel {
                        visible: (turn.modelData.notice || "").length > 0
                        width: turn.width
                        text: turn.modelData.notice || ""
                        wrapMode: Text.Wrap
                        color: chatWindow.ink
                    }
                    ThemeControls.ThemeLabel {
                        visible: turn.modelData.footer.length > 0
                        width: turn.width
                        text: turn.modelData.footer
                        wrapMode: Text.Wrap
                        color: chatWindow.quietInk
                    }
                    Rectangle {
                        width: turn.width; height: 1
                        color: chatWindow.palette.active.mid
                        opacity: 0.45
                    }
                }
            }

        Item {
            id: historyScrollBar
            objectName: "chatHistoryScrollBar"
            parent: transcriptHost
            anchors.top: transcript.top
            anchors.right: transcript.right
            anchors.bottom: transcript.bottom
            z: 20
            implicitWidth: hovered || pressed ? 10 : 7
            width: implicitWidth
            property bool hovered: false
            property bool pressed: false
            property bool draggingThumb: false
            property real dragOffset: 0
            property bool syncingPosition: false
            property real position: 0
            readonly property real scrollRange: Math.max(0, chatWindow.endPosition() - transcript.originY)
            readonly property real fixedThumbLength: 48
            readonly property real size: height > 0 ? Math.min(1, fixedThumbLength / height) : 1
            readonly property real visualPosition: position
            readonly property bool active: hovered || pressed || transcript.moving
            visible: scrollRange > 0
            focus: true
            activeFocusOnTab: true
            function clampPosition(value) {
                return Math.max(0, Math.min(1 - size, Number(value) || 0))
            }
            function setScrollPosition(value) {
                const available = 1 - size
                const next = clampPosition(value)
                position = next
                if (scrollRange > 0 && available > 0)
                    transcript.contentY = transcript.originY + next / available * scrollRange
            }
            function page(direction) {
                chatWindow.scrollHistoryBy(direction * transcript.height * 0.85)
            }
            function increase() { chatWindow.scrollHistoryBy(40) }
            function decrease() { chatWindow.scrollHistoryBy(-40) }
            Rectangle {
                anchors.fill: parent
                radius: width / 2
                color: historyScrollBar.hovered ? Qt.rgba(0.5, 0.5, 0.5, 0.10) : "transparent"
            }
            Rectangle {
                id: thumb
                x: 0
                y: historyScrollBar.position * historyScrollBar.height
                width: historyScrollBar.width
                height: Math.min(historyScrollBar.height, historyScrollBar.fixedThumbLength)
                radius: width / 2
                color: chatWindow.themeColors ? chatWindow.themeColors.colorText
                                              : chatWindow.palette.active.windowText
                opacity: historyScrollBar.pressed ? 0.50 : historyScrollBar.hovered ? 0.35 : 0.18
                border.width: historyScrollBar.activeFocus ? 1 : 0
                border.color: chatWindow.themeColors ? chatWindow.themeColors.colorFocusRing
                                                    : chatWindow.palette.active.highlight
                Behavior on opacity { NumberAnimation { duration: 120 } }
            }
            MouseArea {
                id: scrollBarMouse
                anchors.fill: parent
                hoverEnabled: true
                acceptedButtons: Qt.LeftButton
                onEntered: historyScrollBar.hovered = true
                onExited: if (!pressed) historyScrollBar.hovered = false
                onPressed: function(mouse) {
                    historyScrollBar.forceActiveFocus()
                    historyScrollBar.pressed = true
                    chatWindow.beginManualScroll()
                    if (mouse.y < thumb.y) historyScrollBar.page(-1)
                    else if (mouse.y > thumb.y + thumb.height) historyScrollBar.page(1)
                    else {
                        historyScrollBar.draggingThumb = true
                        historyScrollBar.dragOffset = mouse.y - thumb.y
                    }
                    mouse.accepted = true
                }
                onPositionChanged: function(mouse) {
                    if (!pressed || !historyScrollBar.draggingThumb) return
                    if (historyScrollBar.height > 0)
                        historyScrollBar.setScrollPosition(
                            (mouse.y - historyScrollBar.dragOffset) / historyScrollBar.height)
                }
                onReleased: {
                    historyScrollBar.pressed = false
                    historyScrollBar.draggingThumb = false
                    historyScrollBar.hovered = containsMouse
                    chatWindow.syncHistoryScrollBar()
                    chatWindow.endManualScroll()
                }
                onCanceled: {
                    historyScrollBar.pressed = false
                    historyScrollBar.draggingThumb = false
                    chatWindow.syncHistoryScrollBar()
                    chatWindow.endManualScroll()
                }
            }
            Keys.onPressed: function(event) { chatWindow.handleHistoryKey(event) }
            Accessible.role: Accessible.ScrollBar
            Accessible.name: "对话历史滚动条"
            Accessible.description: "拖动查看历史消息，点击轨道翻页，使用方向键逐步滚动，Page Up 和 Page Down 翻页"
            Accessible.focusable: true
            Accessible.onIncreaseAction: historyScrollBar.increase()
            Accessible.onDecreaseAction: historyScrollBar.decrease()
            Accessible.onNextPageAction: historyScrollBar.page(1)
            Accessible.onPreviousPageAction: historyScrollBar.page(-1)
            Accessible.onScrollDownAction: historyScrollBar.increase()
            Accessible.onScrollUpAction: historyScrollBar.decrease()
            Component.onCompleted: chatWindow.scheduleHistoryScrollBarSync()
        }
        }

        Flow {
            Layout.fillWidth: true
            visible: assistant.attachments.length > 0
            spacing: 5
            Repeater {
                model: assistant.attachments
                delegate: Row {
                    required property int index
                    required property var modelData
                    spacing: 2
                    ThemeControls.ThemeButton { flat: true;
                        text: modelData.name
                        width: Math.min(220, implicitWidth)
                        onClicked: assistant.previewFile(modelData.path)
                    }
                    ThemeControls.ThemeButton { flat: true; text: "×"; onClicked: assistant.removeAttachment(index) }
                }
            }
        }

        RowLayout {
            visible: assistant.queuedCount > 0
            Layout.fillWidth: true
            spacing: 6
            ThemeControls.ThemeButton { flat: true;
                text: (chatWindow.hasPausedMessages ? "已暂停 · " : "")
                      + assistant.queuedCount + " 条待发送 ▾"
                onClicked: queuePopup.open()
                ToolTip.visible: hovered
                ToolTip.text: "默认在当前输出结束后依次发送；停止或出错后需手动继续。"
            }
            ThemeControls.ThemeButton {
                text: "继续队列"
                visible: chatWindow.hasPausedMessages
                enabled: !assistant.busy
                onClicked: assistant.resumeQueuedMessages()
            }
            Item { Layout.fillWidth: true }
        }

        Rectangle {
            id: draftBox
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(170, Math.max(90, draft.contentHeight + 20))
            radius: 6
            color: chatWindow.palette.active.base
            border.color: draft.activeFocus
                          ? (chatWindow.themeColors ? chatWindow.themeColors.colorFocusRing
                                                    : chatWindow.palette.active.highlight)
                          : chatWindow.palette.active.mid
            HoverHandler { id: draftHover }
            ScrollView {
                anchors.fill: parent
                anchors.margins: 8
                clip: true
                TextArea {
                    id: draft
                    placeholderText: assistant.busy
                        ? "可继续输入；Enter 排队，或点击插话。Ctrl+V 可粘贴图片"
                        : "输入消息；@ 引用文件，/ 命令。Enter 发送，Shift+Enter 换行"
                    selectByMouse: true
                    wrapMode: TextEdit.Wrap
                    background: null
                    onActiveFocusChanged: if (activeFocus) chatWindow.clearHistorySelection()
                    onTextChanged: chatWindow.scheduleCompletionUpdate()
                    Keys.onPressed: function(event) {
                        if (event.matches(StandardKey.Paste)) {
                            // Leave ordinary text paste to TextArea's native handling.
                            if (assistant.pasteClipboardImage()) event.accepted = true
                        } else if (completionPopup.visible && (event.key === Qt.Key_Tab || event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) {
                            chatWindow.acceptCompletion(completionList.currentIndex); event.accepted = true
                        } else if (completionPopup.visible && event.key === Qt.Key_Down) {
                            completionList.currentIndex = Math.min(completionList.count - 1, completionList.currentIndex + 1); event.accepted = true
                        } else if (completionPopup.visible && event.key === Qt.Key_Up) {
                            completionList.currentIndex = Math.max(0, completionList.currentIndex - 1); event.accepted = true
                        } else if (event.key === Qt.Key_Escape && completionPopup.visible) {
                            completionPopup.close(); event.accepted = true
                        } else if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && !(event.modifiers & Qt.ShiftModifier)) {
                            chatWindow.sendDraft(); event.accepted = true
                        }
                    }
                    TapHandler {
                        acceptedButtons: Qt.RightButton
                        onTapped: function(eventPoint) {
                            draftMenu.x = eventPoint.position.x
                            draftMenu.y = eventPoint.position.y
                            draftMenu.open()
                        }
                    }
                    Menu {
                        id: draftMenu
                        ThemedMenuItem {
                            text: "粘贴（含图片）"
                            onTriggered: chatWindow.pasteDraft()
                        }
                        MenuSeparator { }
                        ThemedMenuItem {
                            text: "复制"
                            enabled: draft.selectedText.length > 0
                            onTriggered: draft.copy()
                        }
                        ThemedMenuItem {
                            text: "剪切"
                            enabled: draft.selectedText.length > 0
                            onTriggered: draft.cut()
                        }
                        ThemedMenuItem {
                            text: "全选"
                            enabled: draft.text.length > 0
                            onTriggered: draft.selectAll()
                        }
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 6
            ThemeControls.ThemeButton { flat: true;
                text: "＋"
                Accessible.name: "添加附件"
                onClicked: attachmentDialog.open()
                ToolTip.visible: hovered; ToolTip.text: "图片、音频、PDF 与文本文件"
            }
            ThemeControls.ThemeButton { flat: true;
                text: "复制"
                enabled: draft.text.length > 0
                opacity: draftHover.hovered || hovered || activeFocus ? 1 : 0
                onClicked: assistant.copyText(draft.text)
                ToolTip.visible: hovered; ToolTip.text: "复制输入框的全部文字"
            }
            ThemeControls.ThemeComboBox {
                Layout.preferredWidth: 103
                topPadding: 4; bottomPadding: 4
                model: [{text: "讨论", value: "chat"}, {text: "操作", value: "operate"}, {text: "攻关", value: "research"}]
                ToolTip.visible: hovered
                ToolTip.text: "讨论：只读分析与计划；操作、攻关：按授权执行设计和计算。"
                textRole: "text"; valueRole: "value"
                currentIndex: ["chat", "operate", "research"].indexOf(assistant.taskMode)
                enabled: !assistant.busy
                onActivated: assistant.setTaskMode(currentValue)
                Accessible.name: "任务模式"
            }
            ThemeControls.ThemeComboBox {
                Layout.preferredWidth: 103
                topPadding: 4; bottomPadding: 4
                model: assistant.reasoningOptions.map(function(value) { return {value: value, text: chatWindow.reasoningName(value)} })
                textRole: "text"; valueRole: "value"
                currentIndex: assistant.reasoningOptions.indexOf(assistant.reasoningStrength)
                enabled: !assistant.busy && assistant.reasoningSupported
                onActivated: assistant.setReasoningStrength(currentValue)
                Accessible.name: "推理强度"
            }
            Item { Layout.fillWidth: true }
            Item {
                implicitWidth: 40; implicitHeight: 40
                Layout.preferredWidth: 40; Layout.preferredHeight: 40
                Canvas {
                    id: contextRing
                    anchors.fill: parent
                    onPaint: {
                        const ctx = getContext("2d")
                        ctx.clearRect(0, 0, width, height)
                        ctx.lineWidth = 3
                        ctx.strokeStyle = chatWindow.palette.active.mid
                        ctx.beginPath(); ctx.arc(20,20,16,0,Math.PI * 2); ctx.stroke()
                        ctx.strokeStyle = chatWindow.palette.active.highlight
                        const ratio = Math.max(0, Math.min(1, Number(assistant.contextUsage.ratio) || 0))
                        ctx.beginPath(); ctx.arc(20,20,16,-Math.PI/2,-Math.PI/2+Math.PI*2*ratio); ctx.stroke()
                    }
                }
                ThemeControls.ThemeLabel {
                    anchors.centerIn: parent
                    text: chatWindow.contextPercentage()
                    font.pointSize: Math.max(8, Theme.snapshot.fontSizeBody * 0.78)
                }
                TapHandler { onTapped: contextDialog.open() }
                HoverHandler { id: contextHover }
                ToolTip.visible: contextHover.hovered
                ToolTip.text: "上下文 " + assistant.contextUsage.label
            }
            ThemeControls.ThemeButton {
                primary: true
                text: assistant.busy ? "排队" : "发送"
                enabled: chatWindow.hasDraft
                onClicked: chatWindow.sendDraft()
                ToolTip.visible: hovered
                ToolTip.text: assistant.busy ? "等待当前输出结束后自动发送" : "发送消息"
            }
            ThemeControls.ThemeButton { flat: true;
                text: "插话"
                visible: assistant.busy
                enabled: chatWindow.hasDraft && !assistant.cancelling
                onClicked: chatWindow.steerDraft()
                ToolTip.visible: hovered
                ToolTip.text: "当前任务继续，下一模型决策点处理；插话不会停止任务。"
            }
            ThemeControls.ThemeButton { flat: true;
                text: assistant.cancelling ? "停止中" : "停止"
                visible: assistant.busy
                enabled: !assistant.cancelling
                onClicked: assistant.cancelTask()
            }
        }

    }

    Popup {
        id: queuePopup
        x: page.x
        y: Math.max(40, draftBox.y + page.y - height - 6)
        width: Math.min(480, chatWindow.width - 32)
        height: Math.min(300, queueList.contentHeight + 16)
        focus: false
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: ThemeControls.ThemeSurface { padding: 0; contentSpacing: 0 }
        contentItem: ListView {
            id: queueList
            clip: true
            model: assistant.pendingMessages
            spacing: 5
            delegate: Rectangle {
                id: queuedMessage
                required property var modelData
                width: queueList.width
                height: queueEntry.implicitHeight + 12
                radius: 4
                color: chatWindow.palette.active.alternateBase
                ColumnLayout {
                    id: queueEntry
                    x: 6; y: 6; width: parent.width - 12
                    spacing: 3
                    RowLayout {
                        Layout.fillWidth: true
                        ThemeControls.ThemeLabel {
                            Layout.fillWidth: true
                            text: queuedMessage.modelData.prompt || "附件消息"
                            color: chatWindow.ink
                            wrapMode: Text.Wrap
                            maximumLineCount: 2
                            elide: Text.ElideRight
                        }
                        ThemeControls.ThemeButton { flat: true;
                            text: "撤回"
                            onClicked: assistant.withdrawQueuedMessage(queuedMessage.modelData.id)
                            ToolTip.visible: hovered; ToolTip.text: "移回输入框，保留文字和附件"
                        }
                        ThemeControls.ThemeButton { flat: true;
                            text: "×"
                            Accessible.name: "删除此待发送消息"
                            onClicked: assistant.removeQueuedMessage(queuedMessage.modelData.id)
                        }
                    }
                    ThemeControls.ThemeLabel {
                        Layout.fillWidth: true
                        text: (queuedMessage.modelData.paused ? "已暂停" : "等待当前输出结束")
                              + ((queuedMessage.modelData.files || []).length > 0
                                  ? " · " + queuedMessage.modelData.files.length + " 个附件" : "")
                        color: chatWindow.quietInk
                    }
                }
            }
            ScrollBar.vertical: ScrollBar { }
        }
    }

    Menu {
        id: copyMenu
        x: Math.max(14, chatWindow.width - width - 14)
        y: 76
        background: ThemeControls.ThemeSurface { padding: 0; contentSpacing: 0 }
        ThemedMenuItem { text: "复制完整对话"; onTriggered: assistant.copyConversation() }
        ThemedMenuItem { text: "选择并复制完整对话"; onTriggered: completeConversationDialog.open() }
    }

    ThemeControls.ThemeDialogBase {
        id: completeConversationDialog
        title: "完整对话 · 选择与复制"
        anchors.centerIn: parent
        width: Math.min(940, chatWindow.width - 32)
        height: Math.min(760, chatWindow.height - 64)
        modal: true
        standardButtons: Dialog.Close
        // One document preserves selection across virtualized transcript rows.
        onOpened: completeConversationText.text = assistant.conversationText
        ColumnLayout {
            anchors.fill: parent
            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeButton { text: "复制全部"; onClicked: assistant.copyText(completeConversationText.text) }
                ThemeControls.ThemeButton { text: "全选"; onClicked: { completeConversationText.forceActiveFocus(); completeConversationText.selectAll() } }
                ThemeControls.ThemeButton { text: "复制所选"; enabled: completeConversationText.selectedText.length > 0; onClicked: completeConversationText.copy() }
                Item { Layout.fillWidth: true }
            }
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true
                contentWidth: availableWidth
                clip: true
                TextArea {
                    id: completeConversationText
                    width: parent.width
                    readOnly: true; selectByMouse: true
                    textFormat: TextEdit.PlainText
                    wrapMode: TextEdit.Wrap
                    background: null
                    color: chatWindow.ink
                }
            }
        }
    }

    Popup {
        id: completionPopup
        x: page.x
        y: Math.max(48, draftBox.y + page.y - height - 6)
        width: Math.min(420, chatWindow.width - 32)
        height: Math.min(280, completionList.contentHeight + 16)
        focus: false
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: ThemeControls.ThemeSurface { padding: 0; contentSpacing: 0 }
        contentItem: ListView {
            id: completionList
            clip: true
            model: chatWindow.completionRows
            delegate: ThemedListItem {
                required property int index
                required property var modelData
                width: completionList.width
                height: 58
                highlighted: index === completionList.currentIndex
                text: modelData.label + "\n" + modelData.detail
                onClicked: chatWindow.acceptCompletion(index)
            }
        }
    }

    FileDialog {
        id: attachmentDialog
        title: "引用本地文件（会复制到授权 data 目录）"
        fileMode: FileDialog.OpenFiles
        currentFolder: assistant.attachmentDirectory
        onAccepted: assistant.addAttachments(selectedFiles)
    }

    FileDialog {
        id: applicationDialog
        property string fileReference: ""
        title: "选择用于打开此文件的应用程序"
        fileMode: FileDialog.OpenFile
        onAccepted: assistant.openWithApplication(fileReference, selectedFile.toString())
    }

    Drawer {
        id: sessionDrawer
        width: Math.min(390, chatWindow.width * 0.8)
        height: chatWindow.height
        background: ThemeControls.ThemeSurface { padding: 0; contentSpacing: 0 }
        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 14
            anchors.topMargin: chatChrome.barHeight
            RowLayout {
                ThemeControls.ThemeLabel { text: "会话列表"; font.bold: true; Layout.fillWidth: true }
                ThemeControls.ThemeButton { text: "新建"; enabled: !assistant.busy; onClicked: { assistant.newConversation(); sessionDrawer.close() } }
            }
            ListView {
                Layout.fillWidth: true; Layout.fillHeight: true
                clip: true; model: assistant.sessions
                delegate: RowLayout {
                    required property var modelData
                    width: ListView.view.width
                    ThemedListItem {
                        text: modelData.title
                        Layout.fillWidth: true
                        highlighted: modelData.id === assistant.currentSessionId
                        enabled: !assistant.busy
                        onClicked: { chatWindow.selectSession(modelData.id); sessionDrawer.close() }
                    }
                    ThemeControls.ThemeButton { flat: true;
                        text: "删除"
                        enabled: !assistant.busy
                        onClicked: { deleteDialog.sessionId = modelData.id; deleteDialog.open() }
                    }
                }
                ScrollBar.vertical: ScrollBar { }
            }
        }
    }

    ThemeControls.ThemeDialogBase {
        id: deleteDialog
        property string sessionId: ""
        title: "删除会话"
        anchors.centerIn: parent
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        ThemeControls.ThemeLabel { text: "删除该会话和消息？工程文件与 Token 记账会保留。" ; wrapMode: Text.Wrap; width: 320 }
        onAccepted: assistant.deleteConversation(sessionId)
    }

    ThemeControls.ThemeDialogBase {
        id: tokenDialog
        property bool showOtherModels: false
        title: "Token 用量"
        anchors.centerIn: parent
        width: Math.min(620, chatWindow.width - 40)
        height: Math.min(560, chatWindow.height - 80)
        modal: true
        standardButtons: Dialog.Close
        onOpened: {
            showOtherModels = false
            const ticket = chatWindow.rootLifecycleTicket
            const owner = chatWindow
            const scroll = tokenScroll
            Qt.callLater(function() {
                if (!ticket.alive) return
                if (owner.visible && scroll.contentItem && scroll.contentItem.contentY !== undefined)
                    scroll.contentItem.contentY = 0
            })
        }
        ScrollView {
            id: tokenScroll
            anchors.fill: parent
            contentWidth: availableWidth
            Column {
                width: tokenScroll.availableWidth
                spacing: 10
                TextArea {
                    width: parent.width
                    height: contentHeight + topPadding + bottomPadding
                    text: assistant.tokenSummary
                    readOnly: true; selectByMouse: true
                    wrapMode: TextEdit.Wrap; background: null
                }
                ThemeControls.ThemeButton { flat: true;
                    width: parent.width
                    visible: assistant.otherModelTokenUsage.length > 0
                    text: (tokenDialog.showOtherModels ? "▾ " : "▸ ")
                          + "其他模型（" + assistant.otherModelTokenUsage.length + "）"
                    onClicked: tokenDialog.showOtherModels = !tokenDialog.showOtherModels
                    Accessible.name: "展开或收起其他模型用量"
                }
                Repeater {
                    model: tokenDialog.showOtherModels ? assistant.otherModelTokenUsage : []
                    delegate: TextArea {
                        required property var modelData
                        width: tokenScroll.availableWidth
                        height: contentHeight + topPadding + bottomPadding
                        text: modelData.summary
                        readOnly: true; selectByMouse: true
                        wrapMode: TextEdit.Wrap; background: null
                    }
                }
            }
        }
    }

    ThemeControls.ThemeDialogBase {
        id: contextDialog
        title: "上下文用量"
        anchors.centerIn: parent
        width: Math.min(580, chatWindow.width - 40)
        height: Math.min(520, chatWindow.height - 80)
        modal: true
        standardButtons: Dialog.Close
        ColumnLayout {
            anchors.fill: parent
            RowLayout {
                Layout.fillWidth: true
                ThemeControls.ThemeButton { text: assistant.compacting ? "压缩处理中" : "压缩上下文"; enabled: !assistant.compacting; onClicked: assistant.compactContext() }
                ThemeControls.ThemeLabel { Layout.fillWidth: true; text: assistant.compactionStatus; wrapMode: Text.Wrap; color: chatWindow.thoughtInk }
            }
        ScrollView {
            id: contextScroll
            Layout.fillWidth: true; Layout.fillHeight: true
            contentWidth: availableWidth
            TextArea {
                width: contextScroll.availableWidth
                readOnly: true; selectByMouse: true
                wrapMode: TextEdit.Wrap; background: null
                text: "运行上下文 / 有效输入预算：" + assistant.contextUsage.label + " Token\n"
                      + "总窗口容量：" + assistant.tokenText(assistant.contextUsage.total_capacity || assistant.contextUsage.capacity || 0) + " Token\n"
                      + "输出预留：" + assistant.tokenText(assistant.contextUsage.output_reserved || 0) + " Token\n"
                      + "\u81ea\u52a8\u538b\u7f29\u89e6\u53d1\u70b9\uff1a" + assistant.tokenText(assistant.contextUsage.compaction_trigger || 0) + " Token; \u76ee\u6807\uff1a" + assistant.tokenText(assistant.contextUsage.compaction_target || 0) + " Token.\n\n"
                      + "分项估算（本项目约定）\n"
                      + "对话历史：" + assistant.tokenText(assistant.contextUsage.messages || 0) + " Token\n"
                      + "工具定义：" + assistant.tokenText(assistant.contextUsage.tools || 0) + " Token\n"
                      + "系统说明：" + assistant.tokenText(assistant.contextUsage.instructions || 0) + " Token\n"
                      + "媒体内容：" + assistant.tokenText(assistant.contextUsage.media || 0) + " Token\n"
                      + "请求结构：" + assistant.tokenText(assistant.contextUsage.overhead || 0) + " Token\n\n"
                      + "服务器实际输入：" + (assistant.contextUsage.provider_input_tokens !== undefined ? assistant.tokenText(assistant.contextUsage.provider_input_tokens) + " Token"
                          : assistant.contextUsage.actual === true ? assistant.tokenText(assistant.contextUsage.used) + " Token"
                          : assistant.contextUsage.actual && assistant.contextUsage.actual.input_tokens !== undefined
                          ? assistant.tokenText(assistant.contextUsage.actual.input_tokens) + " Token" : "尚未返回") + "\n\n"
                      + "分项是请求前估算；服务器实际值可能不同，不以分项相加冒充实际消耗。"
                      + (assistant.contextUsage.known ? "有效输入预算来自当前请求的运行时读数。" : "尚无当前请求的有效输入预算读数；总窗口容量见上方。")
            }
        }
        }
    }

    ThemeControls.ThemeDialogBase {
        id: previewDialog
        title: chatWindow.preview.name || "文件预览"
        anchors.centerIn: parent
        width: Math.min(900, chatWindow.width - 32)
        height: Math.min(730, chatWindow.height - 64)
        modal: true
        standardButtons: Dialog.Close
        ColumnLayout {
            anchors.fill: parent
            Image {
                visible: chatWindow.preview.previewKind === "image"
                source: visible ? chatWindow.preview.url : ""
                Layout.fillWidth: true; Layout.fillHeight: true
                fillMode: Image.PreserveAspectFit
                asynchronous: true; mipmap: true
                sourceSize.width: Math.min(4096, Math.max(1, width * 2))
                sourceSize.height: Math.min(4096, Math.max(1, height * 2))
            }
            ScrollView {
                id: plainFilePreview
                visible: chatWindow.preview.previewKind === "plaintext"
                Layout.fillWidth: true; Layout.fillHeight: true
                contentWidth: availableWidth
                clip: true
                TextArea {
                    width: plainFilePreview.availableWidth
                    text: chatWindow.preview.content || ""
                    readOnly: true; selectByMouse: true
                    wrapMode: TextEdit.Wrap
                    textFormat: TextEdit.PlainText
                    color: chatWindow.ink
                    background: null
                }
            }
            ScrollView {
                id: filePreviewScroll
                visible: ["richtext", "table"].indexOf(chatWindow.preview.previewKind) >= 0
                Layout.fillWidth: true; Layout.fillHeight: true
                contentWidth: availableWidth
                clip: true
                TextArea {
                    id: richFilePreview
                    width: filePreviewScroll.availableWidth
                    text: chatWindow.preview.content || ""
                    readOnly: true; selectByMouse: true
                    wrapMode: TextEdit.Wrap
                    textFormat: TextEdit.RichText
                    color: chatWindow.ink
                    background: null
                    onLinkActivated: function(link) { assistant.activateLink(link) }
                    onLinkHovered: function(link) {
                        filePreviewCursor.normalShape = link.length > 0 ? Qt.PointingHandCursor : Qt.IBeamCursor
                        filePreviewCursor.active = assistant.isPreviewableImageLink(link)
                    }
                    PreviewCursor { id: filePreviewCursor; target: richFilePreview }
                }
            }
            ThemeControls.ThemeLabel {
                visible: chatWindow.preview.truncated === true
                text: "内容较大，预览显示前 512 KiB；表格最多显示 500 行、50 列。"
                color: chatWindow.quietInk
                wrapMode: Text.Wrap
                Layout.fillWidth: true
            }
        }
    }

    Connections {
        target: assistant
        function onTranscriptChanged() {
            if (assistant.historyProjectionPending) return
            if (chatWindow.pendingScrollAnchor)
                chatWindow.schedulePendingSessionAnchorRestore()
            else chatWindow.requestFollow()
        }
        function onHistoryProjectionChanged() {
            if (!assistant.historyProjectionPending && chatWindow.pendingScrollAnchor)
                chatWindow.schedulePendingSessionAnchorRestore()
            if (!assistant.historyProjectionPending && chatWindow.historyLoadGestureActive)
                chatWindow.maybeLoadOlderHistory()
        }
        function onHistoryProjectionBatchAboutToInsert(count) {
            chatWindow.captureBatchAnchor()
            if (chatWindow.pendingBatchAnchor) {
                if (chatWindow.batchRestoreTicket) chatWindow.batchRestoreTicket.alive = false
                chatWindow.batchRestoreTicket = {
                    alive: true,
                    generation: ++chatWindow.scrollRestoreGeneration
                }
            }
        }
        function onHistoryProjectionBatchInserted() {
            chatWindow.scheduleHistoryScrollBarSync()
            chatWindow.restoreBatchAnchor()
            if (chatWindow.followOutput && !chatWindow.pendingScrollAnchor)
                followTimer.restart()
        }
        function onStreamingChanged() { chatWindow.requestFollow() }
        function onConversationChanged() {
            transcript.cancelFlick()
            chatWindow.cancelPendingScrollRestore()
            manualScrollIdle.stop()
            manualScrollDecision.stop()
            chatWindow.clearHistorySelection()
            chatWindow.manualScrollActive = false
            chatWindow.scrollSessionId = String(assistant.currentSessionId || "")
            const saved = chatWindow.sessionScrollPositions[chatWindow.scrollSessionId]
            if (saved && !saved.follow) {
                chatWindow.pendingScrollSessionId = chatWindow.scrollSessionId
                chatWindow.pendingScrollAnchor = saved
                chatWindow.pendingScrollRestoreAttempts = 0
                chatWindow.sessionRestoreTicket = {
                    alive: true,
                    generation: ++chatWindow.scrollRestoreGeneration,
                    sessionId: chatWindow.scrollSessionId,
                    attempts: 0,
                    timerSamples: 0,
                    correctionPasses: 0,
                    correctionQueued: false,
                    stableSample: null
                }
                chatWindow.lastScrollRestoreAttemptCount = 0
                chatWindow.followOutput = false
                if (transcript.count > 0) sessionRestoreTimer.restart()
            } else {
                chatWindow.pendingScrollSessionId = ""
                chatWindow.pendingScrollAnchor = null
                chatWindow.pendingScrollRestoreAttempts = 0
                chatWindow.followOutput = true
                chatWindow.requestFollow()
            }
        }
        function onSessionsRequested() { sessionDrawer.open() }
        function onStatisticsRequested() { tokenDialog.open() }
        function onContextChanged() { contextRing.requestPaint() }
        function onConfigurationChanged() { contextRing.requestPaint() }
        function onQueueChanged() { if (assistant.queuedCount === 0) queuePopup.close() }
        function onAttachmentRequested() { attachmentDialog.open() }
        function onApplicationRequested(value) {
            applicationDialog.fileReference = value
            applicationDialog.open()
        }
        function onPreviewRequested(value) {
            chatWindow.preview = value
            previewDialog.open()
        }
        function onDraftRestored(value) {
            // Explicit withdrawal restores without replacing an existing draft.
            if (value.length > 0) draft.text = draft.text.length > 0 ? draft.text + "\n\n" + value : value
            draft.cursorPosition = draft.text.length
            draft.forceActiveFocus()
        }
    }
    Component.onCompleted: chatWindow.scrollSessionId = String(assistant.currentSessionId || "")
    WindowChrome {
        id: chatChrome
        hostWindow: chatWindow
        statusText: assistant.statusMessage === "准备就绪。" ? "" : assistant.statusMessage
        statusSeverity: assistant.busy ? "busy" : "info"
    }
}
