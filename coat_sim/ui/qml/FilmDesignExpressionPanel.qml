import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "theme_controls" as ThemeControls

Rectangle {
    id: panel
    objectName: "filmDesignExpressionPanel"
    required property var controller
    required property var themeTokens
    property string targetSide: "front"
    property string expressionDraft: controller.expressionText || ""
    property bool expressionDraftPending: false
    signal saveTemplateRequested()
    signal loadTemplateRequested()

    color: Theme.snapshot.colorSurface
    radius: Theme.snapshot.radiusMd
    border.color: Theme.snapshot.colorBorder
    border.width: Theme.snapshot.borderWidth
    // ScrollView supplies the actual width. Natural text/row widths must not
    // push a narrow panel's children beyond that viewport.
    implicitWidth: 0
    implicitHeight: contentLayout.implicitHeight + Theme.snapshot.spaceMd * 2

    FilmDesignLayerColumnMetrics {
        id: symbolColumnMetrics
        themeTokens: panel.themeTokens
        availableWidth: symbolList.width
        horizontalPadding: Theme.snapshot.spaceXs / 2
        columnGap: symbolList.width < 470 ? 2 : Theme.snapshot.spaceXs
        compact: true
    }


    function commitDraft(normalize) {
        expressionCommitTimer.stop()
        if (expressionArea.inputMethodComposing) return false
        if (expressionDraftPending) {
            if (!controller.setExpressionText(expressionDraft)) return false
            expressionDraftPending = false
        }
        if (normalize) {
            controller.normalizeExpressionText()
            expressionDraft = controller.expressionText
            expressionArea.text = expressionDraft
        }
        return true
    }

    function syncExpressionText() {
        if (!expressionDraftPending) {
            expressionDraft = controller.expressionText
            expressionArea.text = expressionDraft
        }
    }

    Connections {
        target: panel.controller
        function onExpressionTextChanged() { panel.syncExpressionText() }
    }
    ColumnLayout {
        id: contentLayout
        anchors.fill: parent
        anchors.margins: Theme.snapshot.spaceMd
        spacing: Theme.snapshot.spaceSm
        ThemeControls.ThemeLabel {
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            wrapMode: Text.WordWrap
            text: "膜堆表达式与符号表"
            font.pointSize: Theme.snapshot.fontSizeHeading
            font.weight: Theme.snapshot.fontWeightEmphasis
            color: Theme.snapshot.colorText
        }


    ThemeControls.ThemeLabel {
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        wrapMode: Text.WordWrap
        text: panel.controller.expressionInSync
              ? "表达式与当前层堆标记为同步。"
              : "表达式尚未展开，或当前层堆在展开后已修改。"
        color: panel.controller.expressionInSync
               ? Theme.snapshot.colorSuccess : Theme.snapshot.colorWarning
    }
    ThemeControls.ThemeLabel {
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        text: "支持 A–Z 符号、正厚度倍率、括号重复 ^整数以及 G1/G2 分组。编辑时保留原文，失焦后将 ASCII 小写转为大写；展开由设计服务解析。"
        wrapMode: Text.WordWrap
        color: Theme.snapshot.colorTextMuted
    }
    TextArea {
        id: expressionArea
        objectName: "filmDesignExpressionArea"
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        Layout.preferredHeight: 112
        Accessible.name: "多行膜堆表达式"
        placeholderText: "示例：\nG1 (HL)^5 G2 (HL)^5\n或：1.2 (0.5 A B 0.5 C)^2"
        text: panel.controller.expressionText
        wrapMode: TextEdit.Wrap
        selectByMouse: true
        inputMethodHints: Qt.ImhMultiLine
        font.family: Theme.snapshot.fontFamily
        font.pointSize: Theme.snapshot.fontSizeBody
        color: Theme.snapshot.colorText
        selectionColor: Theme.snapshot.colorSelection
        selectedTextColor: Theme.snapshot.colorText
        background: Rectangle {
            color: Theme.snapshot.colorSurface
            radius: Theme.snapshot.radiusSm
            border.width: expressionArea.activeFocus ? Theme.snapshot.focusRingWidth : Theme.snapshot.borderWidth
            border.color: expressionArea.activeFocus ? Theme.snapshot.colorFocusRing : Theme.snapshot.colorBorder
        }
        onTextChanged: {
            if (text === panel.controller.expressionText) {
                panel.expressionDraft = text
                panel.expressionDraftPending = false
                expressionCommitTimer.stop()
                return
            }
            if (!activeFocus) return
            panel.expressionDraft = text
            panel.expressionDraftPending = true
            panel.controller.markEditingDirty()
            expressionCommitTimer.restart()
        }
        onInputMethodComposingChanged: {
            if (!inputMethodComposing && panel.expressionDraftPending)
                expressionCommitTimer.restart()
        }
        onActiveFocusChanged: if (!activeFocus) panel.commitDraft(true)
    }
    Timer {
        id: expressionCommitTimer
        interval: 180
        repeat: false
        onTriggered: {
            if (expressionArea.inputMethodComposing) {
                restart()
                return
            }
            panel.commitDraft(false)
        }
    }
    RowLayout {
        id: symbolActions
        objectName: "filmDesignSymbolHeader"
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        Layout.preferredWidth: Math.max(0, contentLayout.width)
        Layout.maximumWidth: Number.POSITIVE_INFINITY
        spacing: Theme.snapshot.spaceXs
        ThemeControls.ThemeButton {
            objectName: "filmDesignAddSymbol"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            text: "添加符号"
            Accessible.name: "添加表达式符号 A 到 Z"
            Layout.preferredWidth: Math.max(0,
                (symbolActions.width - 3 * symbolActions.spacing) / 4)
            ToolTip.visible: hovered
            ToolTip.text: "添加一个 A–Z 表达式符号行"
            leftPadding: Theme.snapshot.spaceXs
            rightPadding: Theme.snapshot.spaceXs
            enabled: !panel.controller.expressionBusy
            onClicked: if (panel.commitDraft(true)) panel.controller.addSymbol()
        }
        ThemeControls.ThemeButton {
            objectName: "filmDesignSaveExpressionTemplate"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            text: "保存模板"
            Accessible.name: "保存表达式模板"
            Layout.preferredWidth: Math.max(0,
                (symbolActions.width - 3 * symbolActions.spacing) / 4)
            ToolTip.visible: hovered
            ToolTip.text: "保存表达式与符号材料到 SQLite 模板库"
            leftPadding: Theme.snapshot.spaceXs
            rightPadding: Theme.snapshot.spaceXs
            enabled: !panel.controller.expressionBusy
            onClicked: if (panel.commitDraft(true)) panel.saveTemplateRequested()
        }
        ThemeControls.ThemeButton {
            objectName: "filmDesignLoadExpressionTemplate"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            text: "加载模板"
            Accessible.name: "加载表达式模板"
            Layout.preferredWidth: Math.max(0,
                (symbolActions.width - 3 * symbolActions.spacing) / 4)
            ToolTip.visible: hovered
            ToolTip.text: "从 SQLite 模板库加载表达式与符号材料"
            leftPadding: Theme.snapshot.spaceXs
            rightPadding: Theme.snapshot.spaceXs
            enabled: !panel.controller.expressionBusy
            onClicked: if (panel.commitDraft(true)) panel.loadTemplateRequested()
        }
        ThemeControls.ThemeButton {
            objectName: "filmDesignApplyExpression"
            Layout.fillWidth: true
            Layout.minimumWidth: 0
            text: "应用膜堆"
            primary: true
            Accessible.name: "将表达式应用到当前膜堆"
            Layout.preferredWidth: Math.max(0,
                (symbolActions.width - 3 * symbolActions.spacing) / 4)
            ToolTip.visible: hovered
            ToolTip.text: "将当前表达式展开到所选的正面或背面膜堆"
            leftPadding: Theme.snapshot.spaceXs
            rightPadding: Theme.snapshot.spaceXs
            enabled: !panel.controller.expressionBusy
            onClicked: if (panel.commitDraft(true)) panel.controller.expandExpressionAsync(panel.targetSide)
        }
    }
    ThemeControls.ThemeLabel {
        objectName: "filmDesignExpressionOperation"
        Layout.fillWidth: true
        visible: panel.controller.expressionBusy
        text: panel.controller.expressionOperation
        color: Theme.snapshot.colorInfo
        elide: Text.ElideRight
    }
    RowLayout {
        objectName: "filmDesignSymbolColumns"
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        Layout.preferredWidth: 0
        Layout.maximumWidth: Number.POSITIVE_INFINITY
        Layout.leftMargin: symbolColumnMetrics.horizontalPadding
        Layout.rightMargin: symbolColumnMetrics.horizontalPadding
        spacing: symbolColumnMetrics.columnGap
        ThemeControls.ThemeLabel {
            objectName: "filmDesignSymbolHeaderIndex"
            text: "符号"
            Layout.preferredWidth: symbolColumnMetrics.indexWidth
            Layout.minimumWidth: symbolColumnMetrics.indexWidth
            Layout.maximumWidth: symbolColumnMetrics.indexWidth
            color: Theme.snapshot.colorTextMuted
            horizontalAlignment: Text.AlignHCenter
        }
        ThemeControls.ThemeLabel {
            objectName: "filmDesignSymbolHeaderMaterial"
            text: "膜材质"
            Layout.preferredWidth: symbolColumnMetrics.materialWidth
            Layout.minimumWidth: symbolColumnMetrics.materialWidth
            Layout.maximumWidth: symbolColumnMetrics.materialWidth
            color: Theme.snapshot.colorTextMuted
            elide: Text.ElideRight
        }
        ThemeControls.ThemeLabel {
            objectName: "filmDesignSymbolHeaderDataset"
            text: "光学数据集"
            Layout.preferredWidth: symbolColumnMetrics.datasetWidth
            Layout.minimumWidth: symbolColumnMetrics.datasetWidth
            Layout.maximumWidth: symbolColumnMetrics.datasetWidth
            color: Theme.snapshot.colorTextMuted
            elide: Text.ElideRight
        }
        Item {
            objectName: "filmDesignSymbolHeaderGroupPlaceholder"
            Layout.preferredWidth: symbolColumnMetrics.groupWidth
            Layout.minimumWidth: symbolColumnMetrics.groupWidth
            Layout.maximumWidth: symbolColumnMetrics.groupWidth
            Layout.minimumHeight: symbolColumnMetrics.themeTokens.controlHeightSm
        }
        ThemeControls.ThemeLabel {
            objectName: "filmDesignSymbolHeaderQw"
            text: "光学厚度\nQW"
            Layout.preferredWidth: symbolColumnMetrics.qwWidth
            Layout.minimumWidth: symbolColumnMetrics.qwWidth
            Layout.maximumWidth: symbolColumnMetrics.qwWidth
            Layout.minimumHeight: implicitHeight
            color: Theme.snapshot.colorTextMuted
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeBody
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            wrapMode: Text.WordWrap
            Accessible.name: "光学厚度，单位 QW"
        }
        ThemeControls.ThemeLabel {
            objectName: "filmDesignSymbolHeaderNm"
            text: "物理厚度\nnm"
            Layout.preferredWidth: symbolColumnMetrics.nmWidth
            Layout.minimumWidth: symbolColumnMetrics.nmWidth
            Layout.maximumWidth: symbolColumnMetrics.nmWidth
            Layout.minimumHeight: implicitHeight
            color: Theme.snapshot.colorTextMuted
            font.family: Theme.snapshot.fontFamily
            font.pointSize: Theme.snapshot.fontSizeBody
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            wrapMode: Text.WordWrap
            Accessible.name: "物理厚度，单位 nm"
        }
        ThemeControls.ThemeLabel {
            objectName: "filmDesignSymbolHeaderInsert"
            text: "+"
            Layout.preferredWidth: symbolColumnMetrics.insertWidth
            Layout.minimumWidth: symbolColumnMetrics.insertWidth
            Layout.maximumWidth: symbolColumnMetrics.insertWidth
            color: Theme.snapshot.colorTextMuted
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            Accessible.name: "插入符号行"
        }
        ThemeControls.ThemeLabel {
            objectName: "filmDesignSymbolHeaderDelete"
            text: "×"
            Layout.preferredWidth: symbolColumnMetrics.deleteWidth
            Layout.minimumWidth: symbolColumnMetrics.deleteWidth
            Layout.maximumWidth: symbolColumnMetrics.deleteWidth
            color: Theme.snapshot.colorTextMuted
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            Accessible.name: "删除符号行"
        }
    }
    ListView {
        id: symbolList
        objectName: "filmDesignSymbolList"
        Layout.fillWidth: true
        Layout.minimumWidth: 0
        Layout.minimumHeight: 48
        // Delegates use the tallest themed control, not a fixed 38 px row.
        // Four symbols fit in full at default scale; longer lists still scroll.
        Layout.preferredHeight: Math.min(Math.max(230,
            panel.themeTokens.controlHeightMd * 6), Math.max(48, symbolList.contentHeight))
        clip: true
        spacing: Theme.snapshot.spaceXs
        reuseItems: true
        cacheBuffer: 48
        model: panel.controller.expressionSymbols
        delegate: FilmDesignLayerRow {
            width: symbolList.width
            controller: panel.controller
            side: "symbol"
            symbolRow: true
            columnMetrics: symbolColumnMetrics
            onColumnFocusRequested: function(columnName, rowDelta) {
                const targetIndex = index + rowDelta
                if (targetIndex < 0 || targetIndex >= symbolList.count) return
                symbolList.positionViewAtIndex(targetIndex, ListView.Contain)
                symbolList.forceLayout()
                const target = symbolList.itemAtIndex(targetIndex)
                if (target) target.focusColumnField(columnName)
            }
            onRemoveRequested: function(id) { panel.controller.deleteSymbol(id) }
        }
        footer: ThemeControls.ThemeLabel {
            visible: symbolList.count === 0
            width: symbolList.width
            text: "请添加 A–Z 符号并选择材料与数据集。"
            color: Theme.snapshot.colorTextMuted
            padding: Theme.snapshot.spaceXs
        }
    }
    }
}
