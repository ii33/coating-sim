pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls

// Searchable material selector backed by the already-loaded material options.
// It filters this in-memory snapshot only; typing never asks the catalog service
// to scan files or rebuild its material list.
FocusScope {
    id: control
    readonly property var themeTokens:
        typeof Theme.tokenValues !== "undefined" ? Theme.tokenValues : Theme.snapshot

    property var model: []
    property string textRole: "name"
    property string valueRole: "id"
    property int currentIndex: -1
    property string displayText: ""
    property string placeholderText: "搜索材料"
    property string accessibleLabel: ""
    property string accessibleDescription: ""
    property string emptyText: "无匹配材料"
    property string optionObjectNamePrefix: "themeMaterialOption_"
    property string searchText: ""
    property bool queryEdited: false
    property int highlightedIndex: -1
    property bool searching: false
    property var sourceRows: []
    property var filteredRows: []

    readonly property var currentValue: valueAt(currentIndex)
    readonly property string currentText: currentDisplayText
    readonly property var optionsListView: optionList
    readonly property var emptyState: emptyLabel
    readonly property bool popupVisible: pickerPopup.visible
    readonly property point popupAnchorPosition: positionInWindow(
        Qt.point(0, control.height))
    readonly property string currentDisplayText: displayText.length > 0
                                                   ? displayText : nameAt(currentIndex)
    readonly property real rowHeight: Math.max(control.themeTokens.controlHeightMd,
                                               height)

    signal activated(int index)

    implicitWidth: 220
    implicitHeight: control.themeTokens.controlHeightMd

    function sourceRow(index) {
        return index >= 0 && index < sourceRows.length ? sourceRows[index] : null
    }

    function nameAt(index) {
        const row = sourceRow(index)
        if (!row) return ""
        const value = row[textRole]
        return value === undefined || value === null ? "" : String(value)
    }

    function valueAt(index) {
        const row = sourceRow(index)
        if (!row) return ""
        const value = row[valueRole]
        return value === undefined || value === null ? "" : String(value)
    }

    function normalized(value) {
        return String(value || "").trim().toLowerCase()
    }

    function positionInWindow(point) {
        let item = control
        let position = point
        while (item) {
            position = Qt.point(position.x + Number(item.x || 0),
                                position.y + Number(item.y || 0))
            item = item.parent
        }
        return position
    }

    function rebuildSourceRows() {
        const rows = []
        if (Array.isArray(model)) {
            for (let i = 0; i < model.length; ++i) rows.push(model[i])
        } else if (model && typeof model.get === "function") {
            const count = Number(model.count || 0)
            for (let i = 0; i < count; ++i) rows.push(model.get(i))
        } else if (model && Number(model.length) > 0) {
            for (let i = 0; i < Number(model.length); ++i) rows.push(model[i])
        }
        sourceRows = rows
        refreshMatches()
    }

    function refreshMatches() {
        const query = searching && queryEdited ? normalized(searchText) : ""
        const matches = []
        for (let i = 0; i < sourceRows.length; ++i) {
            if (!query || normalized(nameAt(i)).indexOf(query) >= 0)
                matches.push({sourceIndex: i})
        }
        filteredRows = matches
        if (highlightedIndex >= matches.length) highlightedIndex = -1
    }

    function candidateLabel(index) {
        const name = nameAt(index)
        const normalizedName = normalized(name)
        if (!normalizedName) return ""
        let sameNameCount = 0
        for (let i = 0; i < sourceRows.length; ++i) {
            if (normalized(nameAt(i)) === normalizedName) ++sameNameCount
        }
        if (sameNameCount < 2) return name

        const row = sourceRow(index)
        const details = []
        const category = String(row.category || row.material_category || row.role || "").trim()
        if (category) details.push(category)
        const identity = String(row.material_id || row.id || row.key || "").trim()
        if (identity && details.indexOf(identity) < 0) details.push(identity)
        if (!details.length) details.push("选项 " + String(index + 1))
        return name + " · " + details.join(" · ")
    }

    function beginSearch() {
        if (!enabled) return
        searching = true
        searchText = currentDisplayText
        queryEdited = false
        highlightedIndex = -1
        refreshMatches()
        // Opening the overlay during the initial mouse press would cancel
        // the native editor release and its pending full-value selection.
        // The shared behavior opens it after that focus selection instead.
        if (!field.activeFocus && !pickerPopup.visible) pickerPopup.open()
    }

    function resetSearch() {
        searching = false
        searchText = ""
        queryEdited = false
        highlightedIndex = -1
        refreshMatches()
    }

    function exactNameIndex(query) {
        const target = normalized(query)
        if (!target) return -1
        let result = -1
        for (let i = 0; i < sourceRows.length; ++i) {
            if (normalized(nameAt(i)) !== target) continue
            if (result >= 0) return -1
            result = i
        }
        return result
    }

    function closeAfterSelection() {
        popupCloseReason = "selection"
        if (pickerPopup.visible) pickerPopup.close()
        resetSearch()
        field.focus = false
    }

    function selectSourceIndex(index) {
        if (index < 0 || index >= sourceRows.length) return false
        closeAfterSelection()
        activated(index)
        return true
    }

    function selectFilteredIndex(index) {
        if (index < 0 || index >= filteredRows.length) return false
        return selectSourceIndex(Number(filteredRows[index].sourceIndex))
    }

    function confirmExactName() {
        if (field.inputMethodComposing || !queryEdited) return false
        const index = exactNameIndex(searchText)
        if (index < 0) return false
        return selectSourceIndex(index)
    }

    function finishEditing() {
        if (!searching) return
        if (!confirmExactName()) resetSearch()
        if (pickerPopup.visible) {
            popupCloseReason = "blur"
            pickerPopup.close()
        }
    }

    function acceptInput() {
        if (field.inputMethodComposing) return
        if (highlightedIndex >= 0 && highlightedIndex < filteredRows.length) {
            selectFilteredIndex(highlightedIndex)
            return
        }
        if (!confirmExactName()) {
            finishEditing()
            field.focus = false
        }
    }

    onModelChanged: rebuildSourceRows()
    onTextRoleChanged: refreshMatches()
    onValueRoleChanged: refreshMatches()
    Component.onCompleted: rebuildSourceRows()

    TextField {
        id: field
        objectName: control.objectName ? control.objectName + "_input" : "themeMaterialPickerInput"
        anchors.fill: parent
        text: control.searching ? control.searchText : control.currentDisplayText
        placeholderText: control.currentDisplayText.length > 0
                         ? control.currentDisplayText : control.placeholderText
        selectByMouse: true
        activeFocusOnTab: true
        rightPadding: control.themeTokens.spaceMd + arrow.width
        font.family: control.themeTokens.fontFamily
        font.pointSize: control.themeTokens.fontSizeBody
        font.weight: control.themeTokens.fontWeightNormal
        color: !control.enabled ? control.themeTokens.colorDisabledText : control.themeTokens.colorText
        selectionColor: control.themeTokens.colorSelection
        selectedTextColor: control.themeTokens.colorText
        cursorDelegate: Rectangle {
            objectName: "themeMaterialPickerCursor"
            width: Math.max(1, control.themeTokens.borderWidth)
            color: control.themeTokens.colorText
            visible: field.activeFocus
        }
        Accessible.name: control.accessibleLabel.length > 0
                         ? control.accessibleLabel : control.currentDisplayText
        Accessible.description: control.accessibleDescription
        readonly property bool sharedInputFocusManaged: true
        readonly property var sharedInputFocusOwner: control
        InputFocusBehavior {
            target: field
            blurOnAccepted: false
            onFocusSelectionApplied: {
                if (field.activeFocus && control.searching && !pickerPopup.visible)
                    pickerPopup.open()
            }
        }
        onTextEdited: {
            control.searching = true
            control.queryEdited = true
            control.searchText = text
            control.highlightedIndex = -1
            control.refreshMatches()
            if (!pickerPopup.visible) pickerPopup.open()
        }
        onActiveFocusChanged: {
            if (activeFocus) {
                finishTimer.stop()
                if (!control.searching) control.beginSearch()
            } else {
                finishTimer.restart()
            }
        }
        onEditingFinished: {
            if (!activeFocus && control.searching) control.finishEditing()
        }
        background: Rectangle {
            radius: control.themeTokens.radiusSm
            color: !control.enabled ? control.themeTokens.colorDisabledSurface
                                   : control.themeTokens.colorSurface
            border.width: field.activeFocus ? control.themeTokens.focusRingWidth
                                            : control.themeTokens.borderWidth
            border.color: field.activeFocus ? control.themeTokens.colorFocusRing
                         : field.hovered && control.enabled ? control.themeTokens.colorBorderStrong
                                                             : control.themeTokens.colorBorder
        }
        Text {
            id: arrow
            anchors.right: parent.right
            anchors.rightMargin: control.themeTokens.spaceSm
            anchors.verticalCenter: parent.verticalCenter
            text: "▾"
            font.family: control.themeTokens.fontFamily
            font.pointSize: control.themeTokens.fontSizeBody
            color: !control.enabled ? control.themeTokens.colorDisabledText
                                    : control.themeTokens.colorTextMuted
        }
        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function(event) {
            if (field.inputMethodComposing) return
            if (event.key === Qt.Key_Down || event.key === Qt.Key_Up) {
                if (!pickerPopup.visible) {
                    control.beginSearch()
                    pickerPopup.open()
                }
                if (control.filteredRows.length > 0) {
                    const last = control.filteredRows.length - 1
                    if (control.highlightedIndex < 0)
                        control.highlightedIndex = event.key === Qt.Key_Down ? 0 : last
                    else
                        control.highlightedIndex = (control.highlightedIndex
                            + (event.key === Qt.Key_Down ? 1 : last)) % control.filteredRows.length
                    optionList.positionViewAtIndex(control.highlightedIndex, ListView.Contain)
                }
                event.accepted = true
            } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                control.acceptInput()
                event.accepted = true
            } else if (event.key === Qt.Key_Escape) {
                control.popupCloseReason = "escape"
                control.resetSearch()
                if (pickerPopup.visible) pickerPopup.close()
                field.focus = false
                event.accepted = true
            }
        }
    }

    Timer {
        id: finishTimer
        interval: 0
        onTriggered: {
            if (!field.activeFocus && control.searching)
                control.finishEditing()
        }
    }

    property string popupCloseReason: ""
    Popup {
        id: pickerPopup
        objectName: control.objectName ? control.objectName + "_popup" : "themeMaterialPickerPopup"
        parent: Overlay.overlay
        x: control.popupAnchorPosition.x
        y: control.popupAnchorPosition.y
        width: control.width
        padding: control.themeTokens.borderWidth
        focus: false
        closePolicy: Popup.CloseOnPressOutside | Popup.CloseOnEscape
        implicitHeight: popupContent.implicitHeight + padding * 2
        onClosed: {
            if (control.popupCloseReason === "selection"
                    || control.popupCloseReason === "escape") {
                control.popupCloseReason = ""
                return
            }
            if (control.searching) control.finishEditing()
            field.focus = false
            control.popupCloseReason = ""
        }

        contentItem: Item {
            id: popupContent
            implicitHeight: optionList.visible
                            ? optionList.implicitHeight : emptyLabel.implicitHeight
            ListView {
                id: optionList
                objectName: control.objectName ? control.objectName + "_options" : "themeMaterialPickerOptions"
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                clip: true
                interactive: true
                visible: control.filteredRows.length > 0
                model: control.filteredRows
                currentIndex: control.highlightedIndex
                implicitHeight: Math.min(contentHeight, control.rowHeight * 8)
                boundsBehavior: Flickable.StopAtBounds
                ScrollIndicator.vertical: ScrollIndicator { }
                WheelHandler {
                    target: null
                    onWheel: function(event) {
                        const wheelDelta = event.pixelDelta.y !== 0
                                ? event.pixelDelta.y
                                : event.angleDelta.y / 120 * control.rowHeight * 3
                        const maximum = Math.max(0, optionList.contentHeight
                                                - optionList.height)
                        optionList.contentY = Math.max(0, Math.min(
                            maximum, optionList.contentY - wheelDelta))
                        event.accepted = true
                    }
                }
                delegate: ItemDelegate {
                    id: option
                    required property var modelData
                    required property int index
                    readonly property int sourceIndex: Number(modelData.sourceIndex)
                    readonly property bool currentSelection: option.sourceIndex === control.currentIndex
                    objectName: control.optionObjectNamePrefix
                                + String(control.valueAt(sourceIndex) || sourceIndex)
                    width: optionList.width
                    height: control.rowHeight
                    text: control.candidateLabel(sourceIndex)
                    hoverEnabled: true
                    highlighted: index === control.highlightedIndex
                    Accessible.name: text
                    onHoveredChanged: if (hovered) control.highlightedIndex = index
                    onClicked: control.selectFilteredIndex(index)
                    contentItem: Text {
                        text: option.text
                        font.family: control.themeTokens.fontFamily
                        font.pointSize: control.themeTokens.fontSizeBody
                        color: !option.enabled ? control.themeTokens.colorDisabledText
                               : option.highlighted ? control.themeTokens.colorTextOnAccent
                                                    : control.themeTokens.colorText
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                    }
                    background: Rectangle {
                        radius: control.themeTokens.radiusSm
                        color: !option.enabled ? control.themeTokens.colorDisabledSurface
                               : option.highlighted ? control.themeTokens.colorAccent
                               : option.currentSelection ? control.themeTokens.colorSelection
                               : option.hovered ? control.themeTokens.colorSurfaceRaised : control.themeTokens.colorSurface
                        border.width: option.visualFocus ? control.themeTokens.focusRingWidth : 0
                        border.color: control.themeTokens.colorFocusRing
                    }
                }
            }
            Label {
                id: emptyLabel
                objectName: control.objectName ? control.objectName + "_emptyState"
                                               : "themeMaterialPickerEmptyState"
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                visible: control.filteredRows.length === 0
                height: control.themeTokens.controlHeightMd
                text: control.emptyText
                color: control.themeTokens.colorTextMuted
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                font.family: control.themeTokens.fontFamily
                font.pointSize: control.themeTokens.fontSizeBody
            }
        }
        background: Rectangle {
            color: control.themeTokens.colorSurface
            radius: control.themeTokens.radiusSm
            border.width: control.themeTokens.borderWidth
            border.color: control.themeTokens.colorBorderStrong
        }
    }
}
