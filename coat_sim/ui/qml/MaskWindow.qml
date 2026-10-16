import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs
import "." as LocalComponents
import "theme_controls" as ThemeControls
import "theme_controls/NumericFormat.js" as NumericFormat

LocalComponents.AppWindow {
    id: win
    property var projectNavigation: null
    property var projectResources: null
    property bool projectPendingTextEdited: false
    property bool projectCloseAuthorized: false
    LocalComponents.ProjectPendingEdits { editorWindow: win }
    property bool viewportInputDiagnostics: false
    property bool pinchTranslationBaselinePending: false
    property string viewportScrollMode: "pan"
    property string maskSidebarPage: "transform"
    property bool maskSidebarExpanded: true
    readonly property real maskSidebarNavigationWidth: Math.max(
        Math.ceil(maskSidebarTransformTextMetrics.advanceWidth)
            + maskSidebarTransformButton.leftPadding + maskSidebarTransformButton.rightPadding,
        Math.ceil(maskSidebarAnalysisTextMetrics.advanceWidth)
            + maskSidebarAnalysisButton.leftPadding + maskSidebarAnalysisButton.rightPadding)
    readonly property real maskSidebarCollapsedWidth:
        maskSidebarNavigationWidth + 2 * win.themeColors.spaceSm
    readonly property real maskSidebarExpandedWidth:
        maskSidebarCollapsedWidth + win.themeColors.spaceSm + 320
    TextMetrics {
        id: maskSidebarTransformTextMetrics
        font: maskSidebarTransformButton.font
        text: maskSidebarTransformButton.text
    }
    TextMetrics {
        id: maskSidebarAnalysisTextMetrics
        font: maskSidebarAnalysisButton.font
        text: maskSidebarAnalysisButton.text
    }
    function toggleMaskSidebar(page) {
        if (page !== "transform" && page !== "analysis") return
        if (maskSidebarExpanded && maskSidebarPage === page) {
            maskSidebarExpanded = false
            return
        }
        maskSidebarPage = page
        maskSidebarExpanded = true
    }
    onClosing: close => {
        if (win.projectCloseAuthorized) {
            win.projectCloseAuthorized = false
            close.accepted = true
            return
        }
        if (win.projectResources !== null) {
            close.accepted = win.projectResources.requestClose()
        }
    }
    function commitPendingEdits() {
        if (contentItem) contentItem.forceActiveFocus()
    }
    component ViewportToolbarButton: ThemeControls.ThemeButton {
        id: control
        flat: true
        hoverEnabled: true
        contentItem: Text {
            objectName: control.objectName + "_content"
            text: control.text
            font: control.font
            color: !control.enabled ? control.themeTokens.colorDisabledText
                  : control.down ? control.themeTokens.colorAccentPressed
                  : control.primary || control.checked || control.activeFocus || control.hovered
                    ? control.themeTokens.colorAccent : control.themeTokens.colorText
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            wrapMode: Text.NoWrap
        }
        background: Rectangle {
            objectName: control.objectName + "_background"
            radius: control.themeTokens.radiusMd
            color: control.tint(control.themeTokens.colorSurface,
                                !control.enabled ? 0.46
                                : control.down ? 0.80
                                : control.hovered || control.primary || control.checked || control.activeFocus
                                    ? 0.76 : 0.60)
            border.width: !control.enabled ? 0
                          : control.activeFocus ? control.themeTokens.focusRingWidth
                          : control.themeTokens.borderWidth
            border.color: !control.enabled ? "transparent"
                          : control.activeFocus ? control.themeTokens.colorFocusRing
                          : control.down ? control.themeTokens.colorAccentPressed
                          : control.primary || control.checked ? control.themeTokens.colorAccent
                          : control.hovered ? control.themeTokens.colorBorderStrong
                          : control.themeTokens.colorBorder
        }
    }
    component MaskSidebarButton: ThemeControls.ThemeButton {
        id: sidebarButton
        contentItem: Text {
            text: sidebarButton.text
            font: sidebarButton.font
            color: !sidebarButton.enabled ? sidebarButton.themeTokens.colorDisabledText
                  : sidebarButton.primary || sidebarButton.checked
                    ? sidebarButton.themeTokens.colorTextOnAccent
                    : sidebarButton.themeTokens.colorText
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideNone
            wrapMode: Text.NoWrap
        }
    }
    Shortcut {
        objectName: "maskViewportDiagnosticsShortcut"
        sequence: "Ctrl+Shift+F12"
        context: Qt.WindowShortcut
        onActivated: {
            win.viewportInputDiagnostics = !win.viewportInputDiagnostics
            console.info("[MaskViewportInput] diagnostics " +
                         (win.viewportInputDiagnostics ? "enabled" : "disabled"))
        }
    }
    Connections {
        target: typeof Theme !== "undefined" ? Theme : null
        function onSnapshotChanged() {
            maskCanvas.requestPaint()
            weightCanvas.requestPaint()
            angularCanvas.requestPaint()
            heatCanvas.requestPaint()
        }
    }
    readonly property var editorBridge: bridge
    titleBase: "\u4fee\u6b63\u677f"
    captionBase: "修正板 · " + (win.projectResources !== null ? win.projectResources.displayName
             : (win.editorBridge.informationController.resourceInformation.name || "未命名"))
             + (win.editorBridge.dirty ? " *" : "")
    projectNameForTitle: projectNavigation ? projectNavigation.projectName : ""
    statusText: editorBridge.status
    statusSeverity: !editorBridge.canSave && editorBridge.validationMessage.length > 0 ? "error"
                    : editorBridge.cadBusy || editorBridge.radialBusy || editorBridge.fieldBusy || editorBridge.pointBusy
                      ? "busy" : "info"
    statusDetail: [editorBridge.status,
                   !editorBridge.canSave && editorBridge.validationMessage.length > 0
                     ? editorBridge.validationMessage : "",
                   win.busySummary()].filter(value => String(value).length > 0).join("\n")
    minimumWidth: 960
    minimumHeight: 640

    property var doc: ({outer: [], holes: []})
    property var machine: ({sources: [], masks: []})
    property var rings: []
    property var weights: ({})
    property var pointWeights: ({})
    property var field: ({})
    property bool angularRadiusPending: false
    property bool hasAnalysisPoint: false
    property real analysisX: 0
    property real analysisY: 0
    property bool autoFitView: true
    property real minimumZoom: 0.0001
    property real maximumZoom: 25
    property real panX: 0
    property real panY: 0
    property real zoomX: 1
    property real zoomY: 1
    property int selectedHole: -1
    property int selectedPoint: -1
    property string selectedPolygon: ""
    property int toolIndex: 0   // 0 select, 1 outer, 2 hole, 3 pan
    property string contourMode: "line"
    property var draftPoints: []
    property string draftPointer: ""
    property int rotationContourIndex: 0
    property real lastMouseX: 0
    property real lastMouseY: 0
    property string filePurpose: ""
    property string cadExportKind: "dxf"
    property string cadExportOptions: "{}"
    property string cadReportText: ""

    function sourceColorForTheme(slot, darkSurface) {
        let index=Math.max(0,Math.floor(Number(slot)-1))
        if (!isFinite(index)) index=0
        let hue=((index*137.508)+17)%360
        let saturation=0.82, lightness=darkSurface ? 0.70 : 0.30
        let chroma=(1-Math.abs(2*lightness-1))*saturation
        let second=chroma*(1-Math.abs((hue/60)%2-1))
        let rgb=hue<60 ? [chroma,second,0] : hue<120 ? [second,chroma,0] :
                hue<180 ? [0,chroma,second] : hue<240 ? [0,second,chroma] :
                hue<300 ? [second,0,chroma] : [chroma,0,second]
        let offset=lightness-chroma/2
        function channel(value) {
            let part=Math.round((value+offset)*255).toString(16)
            return part.length<2 ? "0"+part : part
        }
        return "#"+channel(rgb[0])+channel(rgb[1])+channel(rgb[2])
    }
    function sourceColor(slot) {
        let surface=win.themeColors.colorSurface
        let luminance=0.2126*surface.r+0.7152*surface.g+0.0722*surface.b
        return sourceColorForTheme(slot,luminance<0.38)
    }
    function contourColor(kind) {
        let surface=win.themeColors.colorSurface
        let luminance=0.2126*surface.r+0.7152*surface.g+0.0722*surface.b
        if (kind === "outer") return luminance<0.38 ? "#65d6c3" : "#007e72"
        return luminance<0.38 ? "#ff90ad" : "#a42d5b"
    }

    function readDocument() {
        try { doc = JSON.parse(editorBridge.documentJson) }
        catch (e) { doc = ({outer: [], holes: []}) }
        maskCanvas.requestPaint()
    }
    function readMachine() {
        try { machine = JSON.parse(editorBridge.machineJson) }
        catch (e) { machine = ({sources: [], masks: []}) }
        maskCanvas.requestPaint()
    }
    function readRings() {
        try { rings = JSON.parse(editorBridge.ringsJson) }
        catch (e) { rings = [] }
        if (autoFitView) fitViewTimer.restart()
        maskCanvas.requestPaint()
    }
    function readWeights() {
        try { weights = JSON.parse(editorBridge.weightsJson) }
        catch (e) { weights = ({}) }
        weightCanvas.requestPaint()
        maskCanvas.requestPaint()
    }
    function readPointWeights() {
        try { pointWeights = JSON.parse(editorBridge.pointWeightsJson) }
        catch (e) { pointWeights = ({}) }
        weightCanvas.requestPaint()
    }
    function readField() {
        try { field = JSON.parse(editorBridge.fieldJson) }
        catch (e) { field = ({}) }
        angularCanvas.requestPaint()
        heatCanvas.requestPaint()
    }
    function selectedAngularRadiusMm() {
        return Math.round(angularRadiusSlider.value * 10) / 10
    }
    function returnCarrierRevolutions() {
        let sun = Number(machine.sun_teeth), planet = Number(machine.planet_teeth)
        if (!isFinite(sun) || !isFinite(planet) || sun < 1 || planet < 1 ||
            Math.floor(sun) !== sun || Math.floor(planet) !== planet) return 0
        let a = sun, b = planet
        while (b !== 0) {
            let remainder = a % b
            a = b
            b = remainder
        }
        return planet / a
    }
    function samplingSummary() {
        let parts = []
        for (let entry of [[weights, "径向"], [field, "角向与热力图"],
                           [pointWeights, "选点"]]) {
            let count = Number(entry[0].phase_sample_count)
            let perTurn = Number(entry[0].samples_per_revolution)
            if (isFinite(count) && count > 0 && isFinite(perTurn) && perTurn > 0)
                parts.push(entry[1] + " " + count + " 点（" + perTurn + " 点/圈）")
        }
        return parts.join("；")
    }
    function fieldMatchesSelectedRadius() {
        let radii = field.ring_radii_mm || []
        return radii.length === 1 && isFinite(Number(radii[0])) &&
               Math.abs(Number(radii[0]) - selectedAngularRadiusMm()) < 0.051
    }
    function requestAngularField() {
        angularRecomputeTimer.stop()
        if (!editorBridge.machinePath || !editorBridge.canSave || editorBridge.fieldBusy) return
        editorBridge.computeField(maskSlot.value, sourceSlot.value,
                                  selectedAngularRadiusMm().toFixed(1))
    }
    function busySummary() {
        let parts = []
        if (editorBridge.radialBusy) parts.push("整板径向")
        if (editorBridge.fieldBusy) parts.push("角向与热力图")
        if (editorBridge.pointBusy) parts.push("选点")
        return parts.join("、")
    }
    function angularYRange() {
        let low=Infinity, high=-Infinity
        let curves = fieldMatchesSelectedRadius() ? (field.angular_weights_percent || []) : []
        for(let value of (curves[0] || [])) {
            if(value===null || !isFinite(Number(value))) continue
            low=Math.min(low,Number(value));high=Math.max(high,Number(value))
        }
        if(!isFinite(low)) return [0,100]
        let pad=Math.max(0.5,(high-low)*0.08)
        return [Math.max(0,low-pad),Math.min(100,high+pad)]
    }
    function heatColor(value) {
        let fraction=Math.max(0,Math.min(1,Number(value)/100))
        return "hsl(" + (240*(1-fraction)).toFixed(0) + ", 90%, 48%)"
    }
    function ringColor(index) {
        return sourceColor(index + 1)
    }
    function automaticWeightYRange() {
        let low = Infinity, high = -Infinity
        for (let result of [weights, pointWeights]) {
            for (let value of result.weights_percent || []) {
                if (value === null || value === undefined) continue
                let number = Number(value)
                if (!isFinite(number)) continue
                low = Math.min(low, number)
                high = Math.max(high, number)
            }
        }
        if (!isFinite(low)) return [0, 100]
        let padding = Math.max(0.5, (high - low) * 0.08)
        if (high - low < 0.001) padding = Math.max(1, Math.abs(high) * 0.02)
        let minY = low - padding, maxY = high + padding
        if (low >= 0 && high <= 100) {
            minY = Math.max(0, minY)
            maxY = Math.min(100, maxY)
        }
        return [minY, maxY]
    }
    function plotContentTop() {
        return Math.min(maskCanvas.height-32,viewportToolbar.y+viewportToolbar.height+8)
    }
    function viewportToolbarContains(x,y) {
        return x>=viewportToolbar.x && x<viewportToolbar.x+viewportToolbar.width &&
               y>=viewportToolbar.y && y<viewportToolbar.y+viewportToolbar.height
    }
    function toolbarHasButtonAt(item,x,y) {
        for (let child of item.children) {
            if (!child.visible || x<child.x || y<child.y ||
                    x>=child.x+child.width || y>=child.y+child.height) continue
            if (String(child.objectName || "").indexOf("coatcontrol:")===0) return true
            if (toolbarHasButtonAt(child,x-child.x,y-child.y)) return true
        }
        return false
    }
    function viewportToolbarBlankAt(x,y) {
        return viewportToolbarContains(x,y) &&
               !toolbarHasButtonAt(viewportToolbar,x-viewportToolbar.x,y-viewportToolbar.y)
    }
    function plotCenterY() { return (plotContentTop()+maskCanvas.height)/2 }
    function px(x) { return maskCanvas.width / 2 + panX + Number(x) * 1.5 * zoomX }
    function py(y) { return plotCenterY() + panY - Number(y) * 1.5 * zoomY }
    function mmX(x) { return (x - maskCanvas.width / 2 - panX) / (1.5 * zoomX) }
    function mmY(y) { return -(y - plotCenterY() - panY) / (1.5 * zoomY) }
    function nearest(x, y) {
        let polygons = [{pointer:"/outer", points:doc.outer || []}]
        for (let i=0;i<(doc.holes || []).length;i++) polygons.push({pointer:"/holes/"+i, points:doc.holes[i]})
        let hit = {distance: 12, pointer:"", index:-1}
        for (let polygon of polygons) for (let i=0;i<polygon.points.length;i++) {
            let p=polygon.points[i], d=Math.hypot(px(p[0])-x,py(p[1])-y)
            if (d<hit.distance) hit={distance:d,pointer:polygon.pointer,index:i}
        }
        return hit
    }
    function selectedContourPoints() {
        if (selectedPolygon === "/outer") return doc.outer || []
        let match=/^\/holes\/(\d+)$/.exec(selectedPolygon)
        return match ? ((doc.holes || [])[Number(match[1])] || []) : []
    }
    function canDeleteSelectedPoint() {
        let points=selectedContourPoints()
        if (selectedPoint < 0 || selectedPoint >= points.length || points.length <= 3) return false
        let remaining=points.slice()
        remaining.splice(selectedPoint,1)
        if (remaining.length < 3) return false
        let twiceArea=0
        for (let i=0;i<remaining.length;i++) {
            let a=remaining[i], b=remaining[(i+1)%remaining.length]
            let ax=Number(a[0]), ay=Number(a[1]), bx=Number(b[0]), by=Number(b[1])
            if (!isFinite(ax) || !isFinite(ay) || !isFinite(bx) || !isFinite(by)) return false
            twiceArea += ax*by-bx*ay
        }
        return Math.abs(twiceArea) >= 1e-9
    }
    function deleteSelectedPoint() {
        if (!canDeleteSelectedPoint()) return false
        let pointer=selectedPolygon, index=selectedPoint
        let before=editorBridge.documentJson
        editorBridge.removeItem(pointer,index)
        if (editorBridge.documentJson === before) return false
        selectedPoint=-1; selectedHole=-1; selectedPolygon=""
        maskCanvas.requestPaint()
        return true
    }
    function addPoint(x,y) {
        if (toolIndex !== 1 && toolIndex !== 2) return
        if (draftPointer !== (toolIndex === 1 ? "/outer" : "/holes"))
            startContour(toolIndex === 1 ? "/outer" : "/holes")
        if (contourMode === "line" && draftPoints.length >= 500) return
        let next=draftPoints.slice()
        next.push([Number(mmX(x).toFixed(3)),Number(mmY(y).toFixed(3))])
        draftPoints=next
        ++viewportInteractionRevision
        maskCanvas.requestPaint()
    }
    function startContour(pointer) {
        if (pointer !== "/outer" && pointer !== "/holes") return
        draftPointer=pointer
        draftPoints=[]
        selectedHole=pointer === "/holes" ? (doc.holes || []).length : -1
        selectedPoint=-1
        selectedPolygon=""
        toolIndex=pointer === "/outer" ? 1 : 2
        autoFitView=false
        ++viewportInteractionRevision
        maskCanvas.requestPaint()
    }
    function smoothClosed(points) {
        if (!points || points.length < 3) return []
        let result=[], n=points.length, samplesPerSegment=5
        for (let i=0;i<n;i++) {
            let p0=points[(i+n-1)%n], p1=points[i], p2=points[(i+1)%n], p3=points[(i+2)%n]
            for (let j=0;j<samplesPerSegment;j++) {
                let t=j/samplesPerSegment, t2=t*t, t3=t2*t
                let x=0.5*((2*p1[0])+(-p0[0]+p2[0])*t+(2*p0[0]-5*p1[0]+4*p2[0]-p3[0])*t2+(-p0[0]+3*p1[0]-3*p2[0]+p3[0])*t3)
                let y=0.5*((2*p1[1])+(-p0[1]+p2[1])*t+(2*p0[1]-5*p1[1]+4*p2[1]-p3[1])*t2+(-p0[1]+3*p1[1]-3*p2[1]+p3[1])*t3)
                result.push([Number(x.toFixed(6)),Number(y.toFixed(6))])
            }
        }
        if (result.length > 500) {
            let stride=Math.ceil(result.length/500), bounded=[]
            for (let i=0;i<result.length;i+=stride) bounded.push(result[i])
            result=bounded
        }
        return result
    }
    function displayedDraft() {
        return contourMode === "curve" && draftPoints.length >= 3
            ? smoothClosed(draftPoints) : draftPoints
    }
    function undoDraftPoint() {
        if (!draftPoints.length) return
        let next=draftPoints.slice(); next.pop(); draftPoints=next
        if (!next.length) draftPointer=""
        maskCanvas.requestPaint()
    }
    function closeContour() {
        if (!draftPointer || draftPoints.length < 3) return false
        let points=contourMode === "curve" ? smoothClosed(draftPoints) : draftPoints
        if (points.length > 500) return false
        let payload=JSON.stringify(points)
        if (draftPointer === "/outer") editorBridge.setValue("/outer",payload)
        else editorBridge.appendItem("/holes",payload)
        draftPoints=[]; draftPointer=""; toolIndex=0
        maskCanvas.requestPaint()
        return true
    }
    function contourPointer(index) {
        return index <= 0 ? "/outer" : "/holes/" + (index - 1)
    }
    function contourAt(index) {
        return index <= 0 ? (doc.outer || []) : ((doc.holes || [])[index - 1] || [])
    }
    function numericInputText(field) {
        const value = field.numericValueEdited ? field.numericEditText
                                                : field.numericDisplayValue
        return value === undefined || value === null ? String(field.text || "") : String(value)
    }
    function setRotationCenter() {
        let points=contourAt(rotationContourIndex)
        if (!points.length) return
        let x0=Infinity,y0=Infinity,x1=-Infinity,y1=-Infinity
        for (let point of points) {
            x0=Math.min(x0,Number(point[0])); x1=Math.max(x1,Number(point[0]))
            y0=Math.min(y0,Number(point[1])); y1=Math.max(y1,Number(point[1]))
        }
        rotationCenterX.setNumericSourceValue((x0+x1)/2)
        rotationCenterY.setNumericSourceValue((y0+y1)/2)
    }
    function applyRotation() {
        if (!win.numericInputText(rotationCenterX).trim() || !win.numericInputText(rotationCenterY).trim() || !win.numericInputText(rotationAngle).trim()) return
        let cx=Number(win.numericInputText(rotationCenterX)), cy=Number(win.numericInputText(rotationCenterY)), angle=Number(win.numericInputText(rotationAngle))
        if (!isFinite(cx) || !isFinite(cy) || !isFinite(angle)) return
        editorBridge.rotatePolygon(contourPointer(rotationContourIndex),cx,cy,angle)
    }
    function niceTickStep(raw) {
        if (!isFinite(raw) || raw <= 0) return 1
        let power=Math.pow(10,Math.floor(Math.log10(raw))), scaled=raw/power
        return (scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10)*power
    }
    function rulerXStep() { return niceTickStep(84/(1.5*zoomX)) }
    function rulerYStep() { return niceTickStep(84/(1.5*zoomY)) }
    function rulerXValues() {
        let step=rulerXStep(), low=mmX(54), high=mmX(maskCanvas.width-8), values=[]
        for (let value=Math.ceil(low/step)*step; value<=high+step*1e-9; value+=step) values.push(Number(value.toPrecision(12)))
        return values
    }
    function rulerYValues() {
        let step=rulerYStep(), low=mmY(maskCanvas.height-8), high=mmY(28), values=[]
        for (let value=Math.ceil(low/step)*step; value<=high+step*1e-9; value+=step) values.push(Number(value.toPrecision(12)))
        return values
    }
    function drawRulers(ctx) {
        let left=54, top=26, right=maskCanvas.width, bottom=maskCanvas.height
        ctx.save()
        ctx.fillStyle=win.themeColors.colorSurface
        ctx.fillRect(left,0,Math.max(0,right-left),top)
        ctx.fillRect(0,top,left,Math.max(0,bottom-top))
        ctx.strokeStyle=win.themeColors.colorBorderStrong
        ctx.lineWidth=1
        ctx.beginPath();ctx.moveTo(left,top);ctx.lineTo(right,top);ctx.moveTo(left,top);ctx.lineTo(left,bottom);ctx.stroke()
        ctx.fillStyle=win.themeColors.colorTextMuted
        ctx.font="10px sans-serif"
        for (let value of rulerXValues()) {
            let x=px(value)
            if (x<left || x>right) continue
            ctx.beginPath();ctx.moveTo(x,top-5);ctx.lineTo(x,top);ctx.stroke()
            ctx.textAlign="center";ctx.textBaseline="top"
            ctx.fillText(NumericFormat.format(value, Qt.locale(), 3),x,3)
        }
        for (let value of rulerYValues()) {
            let y=py(value)
            if (y<top || y>bottom) continue
            ctx.beginPath();ctx.moveTo(left-5,y);ctx.lineTo(left,y);ctx.stroke()
            ctx.textAlign="right";ctx.textBaseline="middle"
            ctx.fillText(NumericFormat.format(value, Qt.locale(), 3),left-7,y)
        }
        ctx.restore()
    }
    function drawPolyline(ctx, points, close) {
        if (!points || !points.length) return
        ctx.beginPath();ctx.moveTo(px(points[0][0]),py(points[0][1]))
        for(let i=1;i<points.length;i++)ctx.lineTo(px(points[i][0]),py(points[i][1]))
        if(close && points.length>=3)ctx.closePath()
        ctx.stroke()
    }
    // Bounds are in fixed machine XY millimetres; navigation never edits geometry.
    function geometryBounds() {
        let x0=Infinity, y0=Infinity, x1=-Infinity, y1=-Infinity
        function include(x, y) {
            if (x === null || y === null || x === undefined || y === undefined) return
            x=Number(x); y=Number(y)
            if (!isFinite(x) || !isFinite(y)) return
            x0=Math.min(x0,x); x1=Math.max(x1,x)
            y0=Math.min(y0,y); y1=Math.max(y1,y)
        }
        for (let polygon of [doc.outer || []].concat(doc.holes || []))
            for (let point of polygon) include(point[0],point[1])
        for (let ring of rings) {
            for (let point of ring.points || []) include(point[0],point[1])
            // Exact circle extrema also cover trajectories sampled away from quadrants.
            let center=ring.center_xy, radius=Number(ring.radius_mm)
            if (center && ring.radius_mm !== null && isFinite(radius) && radius >= 0) {
                include(Number(center[0])-radius,Number(center[1])-radius)
                include(Number(center[0])+radius,Number(center[1])+radius)
            }
        }
        return isFinite(x0) ? [x0,y0,x1,y1] : []
    }
    function fitAllGeometry() {
        if (!autoFitView || maskCanvas.width <= 64 || maskCanvas.height <= 64) return
        let bounds=geometryBounds()
        if (!bounds.length) {
            panX=0; panY=0; zoomX=1; zoomY=1
        } else {
            let spanX=Math.max(1,bounds[2]-bounds[0])
            let spanY=Math.max(1,bounds[3]-bounds[1])
            let plotHeight=Math.max(64,maskCanvas.height-plotContentTop())
            let scale=Math.min(25,(maskCanvas.width-64)/(1.5*spanX),
                                  (plotHeight-64)/(1.5*spanY))
            if (!isFinite(scale) || scale <= 0) return
            zoomX=scale; zoomY=scale
            panX=-(bounds[0]+bounds[2])*0.75*scale
            panY=(bounds[1]+bounds[3])*0.75*scale
            minimumZoom=Math.min(0.0001,scale/100)
            maximumZoom=Math.max(25,scale)
        }
        maskCanvas.requestPaint()
    }
    function resetView() {
        autoFitView=true
        // Apply the current bounds before input can race the queued layout fit.
        // Keep the timer as a retry for calls made before the canvas is sized.
        if (maskCanvas.width > 64 && maskCanvas.height > 64) fitAllGeometry()
        fitViewTimer.restart()
    }
    function fitCadView() {
        selectedPoint=-1; selectedHole=-1; selectedPolygon=""
        resetView()
    }
    function panView(dx,dy) {
        if ((!dx && !dy) || !isFinite(dx) || !isFinite(dy)) return
        autoFitView=false
        panX+=dx; panY+=dy
        ++viewportInteractionRevision
        maskCanvas.requestPaint()
    }
    function zoomAt(x,y,factor) {
        if (!isFinite(factor) || factor <= 0 || factor === 1) return
        let worldX=mmX(x), worldY=mmY(y)
        let next=Math.max(minimumZoom,Math.min(maximumZoom,zoomX*factor))
        if (next === zoomX) return
        autoFitView=false
        zoomY*=next/zoomX; zoomX=next
        panX=x-maskCanvas.width/2-worldX*1.5*zoomX
        panY=y-plotCenterY()+worldY*1.5*zoomY
        ++viewportInteractionRevision
        maskCanvas.requestPaint()
    }
    function traceViewportInput(kind, detail) {
        if (viewportInputDiagnostics)
            console.info("[MaskViewportInput] " + kind + " " + JSON.stringify(detail))
    }
    function viewportDeviceType(device) {
        if (!device) return PointerDevice.Unknown
        // Qt's QML PointerDevice docs call this deviceType, while the runtime
        // QWheelEvent handler point may expose QInputDevice's C++ type alias.
        return device.deviceType !== undefined ? device.deviceType : device.type
    }
    function wheelHasHorizontalDelta(wheel) {
        return wheel.pixelDelta.x !== 0 || wheel.angleDelta.x !== 0
    }
    function wheelHasVerticalDelta(wheel) {
        return wheel.pixelDelta.y !== 0 || wheel.angleDelta.y !== 0
    }
    function navigateWheel(wheel,touchpad) {
        let pixel=wheel.pixelDelta, angle=wheel.angleDelta
        let hasPixels=pixel.x !== 0 || pixel.y !== 0
        // Resolve each axis independently: native horizontal angle deltas can
        // accompany pixel-based vertical motion, and vice versa.
        let dx=pixel.x !== 0 ? pixel.x : angle.x/120*40
        let dy=pixel.y !== 0 ? pixel.y : angle.y/120*40
        let verticalSteps=pixel.y !== 0 ? pixel.y/120 : angle.y/120
        let control=(wheel.modifiers & Qt.ControlModifier) !== 0
        let shift=(wheel.modifiers & Qt.ShiftModifier) !== 0
        // Preserve the legacy automatic-mode rule for angle-only horizontal
        // wheels, including diagonal angle vectors.
        let horizontalOnly=hasPixels ? (pixel.x !== 0 && pixel.y === 0)
                                     : angle.x !== 0
        let shouldPan=!control && (viewportScrollMode === "pan" || touchpad || shift || horizontalOnly)
        if (shouldPan) {
            if (shift && !dx) { dx=dy; dy=0 }
            // Qt delivers delta signs with the active scroll-direction setting applied.
            if (dx || dy) panView(dx,dy)
        } else {
            // Proportional steps handle high-resolution wheels without sign-only jumps.
            let steps=verticalSteps
            if (steps)
                zoomAt(wheel.x,wheel.y,Math.pow(1.12,Math.max(-10,Math.min(10,steps))))
        }
        wheel.accepted=true
    }
    function startCadImport() {
        if (editorBridge.dirty) importReplaceConfirm.open()
        else importOptionsDialog.open()
    }
    function optionalDrawingNumber(value) {
        if (value.trim() === "") return null
        let number = Number(value)
        if (!isFinite(number) || number <= 0) throw new Error("板厚、公差与曲线采样判据须为正数；未知时留空")
        return number
    }
    function drawingOptions() {
        return {paper: paperChoice.currentText, title: drawingTitle.text,
                material: drawingMaterial.text, thickness_mm: optionalDrawingNumber(win.numericInputText(drawingThickness)),
                tolerance_mm: optionalDrawingNumber(win.numericInputText(drawingTolerance)),
                curve_tolerance_mm: optionalDrawingNumber(win.numericInputText(drawingPrecision)),
                revision: drawingRevision.text, source: drawingSource.text, notes: drawingNotes.text}
    }

    property int initialReadStage: 0
    property bool initialReadFinished: false
    property int viewportInteractionRevision: 0
    property int startupViewportRevision: 0
    Component.onCompleted: {
        win.readDocument()
        win.startupViewportRevision=win.viewportInteractionRevision
        win.initialReadStage=1
        initialReadTimer.start()
    }
    onVisibleChanged: if (visible && autoFitView) fitViewTimer.restart()
    // Yield between snapshot reads so the window can render and the launcher
    // event loop remains active while a saved mask contains large arrays.
    Timer {
        id: initialReadTimer
        interval: 16
        repeat: true
        onTriggered: {
            switch (win.initialReadStage) {
            case 1: win.readMachine(); break
            case 2: win.readRings(); break
            case 3: win.readWeights(); break
            case 4: win.readPointWeights(); break
            case 5: win.readField(); break
            case 6:
                if (win.autoFitView &&
                        win.viewportInteractionRevision === win.startupViewportRevision)
                    win.resetView()
                win.initialReadFinished=true
                initialReadTimer.stop()
                return
            }
            ++win.initialReadStage
        }
    }
    Timer {
        id: fitViewTimer
        interval: 16
        repeat: false
        onTriggered: {
            win.fitAllGeometry()
            if (win.visible && win.autoFitView &&
                    (maskCanvas.width <= 64 || maskCanvas.height <= 64)) restart()
        }
    }
    Timer {
        id: angularRecomputeTimer
        interval: 200
        repeat: false
        onTriggered: win.requestAngularField()
    }
    Connections {
        target: editorBridge
        function onDocumentChanged() { win.readDocument() }
        function onViewFitRequested() { win.fitCadView() }
        function onMachineChanged() { win.readMachine() }
        function onRingsChanged() { win.readRings() }
        function onWeightsChanged() { win.readWeights() }
        function onPointWeightsChanged() { win.readPointWeights() }
        function onFieldChanged() { win.readField() }
        function onCadFinished(operation) {
            if (editorBridge.cadWarnings.length) {
                win.cadReportText = editorBridge.status + "\n\n" + editorBridge.cadWarnings.join("\n\n")
                cadReportDialog.open()
            }
        }
        function onWorkpieceDiameterChanged() { weightCanvas.requestPaint(); angularCanvas.requestPaint(); heatCanvas.requestPaint() }
    }


    LocalComponents.MaskOptimizationDialog {
        id: maskOptimizationDialog
        parent: Overlay.overlay
        hostWindow: win
        optimizer: editorBridge
        projectResources: win.projectResources
    }
    Connections {
        target: editorBridge
        function onOverwriteRequested(url) { overwriteDialog.targetPath = url.toString(); overwriteDialog.open() }
        function onReloadRequested() { reloadDialog.open() }
        function onDocumentReplaceRequested() { documentReplaceDialog.open() }
    }
    ThemeControls.ThemeDialogBase {
        palette: win.palette
        id: documentReplaceDialog
        title: "替换未保存的设计？"
        modal: true
        anchors.centerIn: parent
        width: Math.min(480, win.width - 40)
        standardButtons: Dialog.Yes | Dialog.Cancel
        onAccepted: editorBridge.confirmDocumentReplace(true)
        onRejected: editorBridge.confirmDocumentReplace(false)
        contentItem: Label { text: "当前修正板设计有未保存修改。继续会放弃这些修改并打开或新建设计。"; wrapMode: Text.Wrap }
    }
    ThemeControls.ThemeDialogBase {
        palette: win.palette
        id: reloadDialog
        objectName: "reloadDialog"
        title: "放弃修改并刷新？"
        modal: true
        anchors.centerIn: parent
        width: Math.min(480, win.width - 40)
        standardButtons: Dialog.Yes | Dialog.No
        onAccepted: editorBridge.confirmReload(true)
        onRejected: editorBridge.confirmReload(false)
        contentItem: Label { text: "刷新会重新读取修正板及已加载的机器文件，并放弃当前修正板的未保存修改。不会写入文件。"; wrapMode: Text.Wrap }
    }
    ThemeControls.ThemeDialogBase {
        palette: win.palette
        id: overwriteDialog
        property string targetPath: ""
        title: "覆盖已有设计？"
        modal: true
        anchors.centerIn: parent
        standardButtons: Dialog.Yes | Dialog.No
        onAccepted: editorBridge.confirmSaveAs(true)
        onRejected: editorBridge.confirmSaveAs(false)
        ThemeControls.ThemeLabel { text: "文件已存在，确认覆盖？\n" + overwriteDialog.targetPath; wrapMode: Text.WrapAnywhere; width: 440 }
    }

    FileDialog {
        id: openDialog
        currentFolder: filePurpose === "machine" ? editorBridge.dependencyDirectory("machine") : editorBridge.defaultDirectory
        title: filePurpose === "machine" ? "加载机器" : "打开修正板"
        fileMode: FileDialog.OpenFile
        nameFilters: filePurpose === "machine" ? ["机器设计 (*.cmac)"] : ["修正板设计 (*.cmsk)"]
        onAccepted: {
            if (filePurpose === "machine") editorBridge.loadMachine(selectedFile.toString())
            else editorBridge.loadDocument(selectedFile.toString())
        }
    }
    FileDialog {
        id: saveAsDialog
        options: FileDialog.DontConfirmOverwrite
        onRejected: editorBridge.confirmSaveAs(false)
        currentFolder: editorBridge.defaultDirectory
        title: "修正板另存为"
        fileMode: FileDialog.SaveFile
        nameFilters: ["修正板设计 (*.cmsk)"]
        defaultSuffix: "cmsk"
        onAccepted: editorBridge.saveDocumentAs(selectedFile.toString())
    }

    ThemeControls.ThemeDialogBase {
        palette: win.palette
        id: importReplaceConfirm
        objectName: "maskImportReplaceConfirm"
        implicitHeight: 190
        title: "替换当前轮廓？"
        modal: true
        anchors.centerIn: parent
        width: Math.min(520, parent.width - 48)
        standardButtons: Dialog.Yes | Dialog.No
        onAccepted: importOptionsDialog.open()
        contentItem: Label {
            text: "当前有未保存修改。导入DXF会替换外轮廓与孔洞，保留板高度、描述和备注；不会自动保存原设计文件。"
            wrapMode: Text.Wrap
        }
    }
    ThemeControls.ThemeDialogBase {
        palette: win.palette
        id: importOptionsDialog
        objectName: "maskImportOptionsDialog"
        title: "二维DXF导入选项"
        modal: true
        anchors.centerIn: parent
        width: Math.min(540, parent.width - 48)
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: cadImportFile.open()
        contentItem: ColumnLayout {
            spacing: 9
            GridLayout {
                Layout.fillWidth: true; columns: 2; columnSpacing: 10
                ThemeControls.ThemeLabel { text: "文件单位" }
                ThemeControls.ThemeComboBox { id: cadUnits; Layout.fillWidth: true; model: ["自动读取$INSUNITS", "毫米 mm", "英寸 inch"] }
                ThemeControls.ThemeLabel { text: "曲线误差 / mm" }
                ThemeControls.ThemeField { id: cadCurveError; numericDisplayValue: "0.05"; onTextEdited: numericDisplayValue = text; text: "0.05"; Layout.fillWidth: true; validator: DoubleValidator { bottom: 0.000001; top: 1000; decimals: 9 } }
                ThemeControls.ThemeLabel { text: "端点连接容差 / mm" }
                ThemeControls.ThemeField { id: cadJoinError; numericDisplayValue: "0.01"; onTextEdited: numericDisplayValue = text; text: "0.01"; Layout.fillWidth: true; validator: DoubleValidator { bottom: 0.000001; top: 1000; decimals: 9 } }
            }
            ThemeControls.ThemeLabel {
                Layout.fillWidth: true
                text: "仅模型空间、XY平面且CAD Z=0；一个外轮廓与最多30孔，每轮廓3–500点。圆弧按弦差采样；椭圆/样条为自适应近似。超点数不会自动简化。单位未声明时须手动选择。"
                wrapMode: Text.Wrap
            }
        }
    }
    FileDialog {
        id: cadImportFile
        objectName: "maskCadImportFileDialog"
        title: "导入修正板二维DXF"
        currentFolder: editorBridge.defaultDirectory
        fileMode: FileDialog.OpenFile
        nameFilters: ["二维DXF (*.dxf)"]
        onAccepted: editorBridge.importCad(selectedFile.toString(), ["auto", "mm", "inch"][cadUnits.currentIndex], win.numericInputText(cadCurveError), win.numericInputText(cadJoinError))
    }
    FileDialog {
        id: cadExportFile
        title: win.cadExportKind === "pdf" ? "导出PDF加工图" : "导出二维DXF"
        currentFolder: editorBridge.defaultDirectory
        fileMode: FileDialog.SaveFile
        nameFilters: win.cadExportKind === "pdf" ? ["PDF加工图 (*.pdf)"] : ["二维DXF (*.dxf)"]
        defaultSuffix: win.cadExportKind
        // The native SaveFile picker asks before replacing an existing destination.
        onAccepted: editorBridge.exportCad(selectedFile.toString(), win.cadExportKind, win.cadExportOptions, true)
    }
    ThemeControls.ThemeDialogBase {
        palette: win.palette
        id: drawingOptionsDialog
        title: "PDF加工图选项"
        modal: true
        anchors.centerIn: parent
        width: Math.min(680, parent.width - 48)
        height: Math.min(610, parent.height - 64)
        standardButtons: Dialog.Ok | Dialog.Cancel
        onOpened: {
            drawingSource.text = editorBridge.cadProvenance.source_path || editorBridge.documentPath || "当前修正板设计"
            drawingPrecision.setNumericSourceValue(editorBridge.cadProvenance.curve_tolerance_mm === undefined ? "" : editorBridge.cadProvenance.curve_tolerance_mm)
        }
        onAccepted: {
            try {
                win.cadExportKind = "pdf"
                win.cadExportOptions = JSON.stringify(win.drawingOptions())
                cadExportFile.open()
            } catch (error) {
                win.cadReportText = String(error)
                cadReportDialog.open()
            }
        }
        contentItem: ScrollView {
            clip: true
            contentWidth: availableWidth
            ColumnLayout {
                width: parent.width
                spacing: 8
                GridLayout {
                    Layout.fillWidth: true; columns: 2; columnSpacing: 10
                    ThemeControls.ThemeLabel { text: "图纸名称" }
                    ThemeControls.ThemeField { id: drawingTitle; text: "修正板二维加工图"; Layout.fillWidth: true; maximumLength: 2000 }
                    ThemeControls.ThemeLabel { text: "纸张（横向）" }
                    ThemeControls.ThemeComboBox { id: paperChoice; model: ["A4", "A3"]; Layout.fillWidth: true }
                    ThemeControls.ThemeLabel { text: "材质" }
                    ThemeControls.ThemeField { id: drawingMaterial; placeholderText: "未指定"; Layout.fillWidth: true; maximumLength: 2000 }
                    ThemeControls.ThemeLabel { text: "板厚 / mm" }
                    ThemeControls.ThemeField { id: drawingThickness; numericDisplayValue: ""; onTextEdited: numericDisplayValue = text; placeholderText: "未知留空"; Layout.fillWidth: true; validator: DoubleValidator { bottom: 0.000001; top: 100000; decimals: 9 } }
                    ThemeControls.ThemeLabel { text: "要求尺寸公差 / ±mm" }
                    ThemeControls.ThemeField { id: drawingTolerance; numericDisplayValue: ""; onTextEdited: numericDisplayValue = text; placeholderText: "未知留空"; Layout.fillWidth: true; validator: DoubleValidator { bottom: 0.000001; top: 1000; decimals: 9 } }
                    ThemeControls.ThemeLabel { text: "原CAD采样判据 / mm" }
                    ThemeControls.ThemeField { id: drawingPrecision; numericDisplayValue: ""; onTextEdited: numericDisplayValue = text; placeholderText: "未知留空；非制造认证"; Layout.fillWidth: true; validator: DoubleValidator { bottom: 0.000001; top: 1000; decimals: 9 } }
                    ThemeControls.ThemeLabel { text: "版本" }
                    ThemeControls.ThemeField { id: drawingRevision; placeholderText: "未指定"; Layout.fillWidth: true; maximumLength: 2000 }
                    ThemeControls.ThemeLabel { text: "来源" }
                    ThemeControls.ThemeField { id: drawingSource; Layout.fillWidth: true; maximumLength: 2000 }
                }
                ThemeControls.ThemeLabel { text: "加工备注" }
                TextArea { id: drawingNotes; Layout.fillWidth: true; Layout.preferredHeight: 90; wrapMode: TextEdit.Wrap; placeholderText: "不改动JSON设计备注" }
                ThemeControls.ThemeLabel {
                    Layout.fillWidth: true
                    text: "图纸包含外廓长宽、孔包围框定位、完整顶点坐标表与参数。尺寸依据当前离散轮廓，图示适配纸张而非1:1；不生成刀路，不认证制造公差。"
                    wrapMode: Text.Wrap
                }
            }
        }
    }
    ThemeControls.ThemeDialogBase {
        palette: win.palette
        id: cadReportDialog
        objectName: "maskCadReportDialog"
        title: "CAD操作说明"
        modal: true
        anchors.centerIn: parent
        width: Math.min(660, parent.width - 48)
        height: Math.min(420, parent.height - 64)
        standardButtons: Dialog.Close
        contentItem: ScrollView {
            clip: true
            contentWidth: availableWidth
            TextArea {
                width: parent.width
                readOnly: true; selectByMouse: true
                text: win.cadReportText
                wrapMode: TextEdit.Wrap
                textFormat: TextEdit.PlainText
            }
        }
    }

    Pane {
        anchors.fill: parent
        padding: 0
        background: null
        palette: win.palette
        font.family: win.themeColors.fontFamily
        font.pointSize: win.themeColors.fontSizeBody
        font.weight: win.themeColors.fontWeightNormal
        LocalComponents.ResponsiveOperationBar {
            id: actions
            objectName: "operationBar"
            Accessible.name: "操作栏"
            height: implicitHeight
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.leftMargin: 16
            anchors.rightMargin: 16
            anchors.topMargin: win.contentTopMargin + (win.themeColors ? win.themeColors.spaceMd : 12)
            spacing: 8
            LocalComponents.ProjectModuleSelector {
                navigation: win.projectNavigation
                currentModuleKey: "mask"
            }
            LocalComponents.ProjectResourceControls {
                projectResources: win.projectResources
                informationProvider: win.projectResources !== null ? win.projectResources : win.editorBridge.informationController
            }
            ThemeControls.ThemeButton {
                objectName: "maskOptimizationButton"
                text: "修正板优化"
                visible: win.projectResources !== null
                enabled: !editorBridge.editingFrozen
                onClicked: maskOptimizationDialog.open()
            }
            ThemeControls.ThemeButton {
                text: "新建"
                visible: win.projectResources === null
                onClicked: editorBridge.newDocument(JSON.stringify({z_mm:300,outer:[],holes:[]}))
            }
            ThemeControls.ThemeButton { text: "打开"; visible: win.projectResources === null; onClicked: { win.filePurpose="mask"; openDialog.open() } }
            ThemeControls.ThemeButton {
                objectName: "reloadButton"
                Accessible.name: "刷新并重新载入文件"
                text: "刷新"
                enabled: editorBridge.canReload
                onClicked: editorBridge.reloadCurrent()
                ToolTip.visible: hovered
                ToolTip.text: "重新读取当前修正板及已加载的机器文件"
            }
            ThemeControls.ThemeButton {
                text: "保存"
                visible: win.projectResources === null
                enabled: editorBridge.canSave
                onClicked: editorBridge.documentPath ? editorBridge.saveDocument() : saveAsDialog.open()
            }
            ThemeControls.ThemeButton { text: "另存为"; visible: win.projectResources === null; enabled: editorBridge.canSave; onClicked: saveAsDialog.open() }
            ThemeControls.ThemeButton { text: "加载机器"; onClicked: { win.filePurpose="machine"; openDialog.open() } }
            ThemeControls.ThemeButton {
                objectName: "machineDependencyActionButton"
                text: "编辑机器"
                visible: editorBridge.machinePath.length > 0
                onClicked: {
                    if (win.projectResources !== null)
                        win.projectResources.openDependencyEditor("machine")
                    else
                        launcher.openWindow("machine", editorBridge.machinePath)
                }
            }
            ThemeControls.ThemeButton {
                id: cadMenuButton
                text: "CAD ▾"
                enabled: !editorBridge.cadBusy
                onClicked: cadMenu.popup()
                Menu {
                    id: cadMenu
                    y: cadMenuButton.height
                    MenuItem { text: "导入二维DXF"; onTriggered: win.startCadImport() }
                    MenuItem {
                        text: "导出二维DXF"; enabled: editorBridge.canSave
                        onTriggered: { win.cadExportKind = "dxf"; win.cadExportOptions = "{}"; cadExportFile.open() }
                    }
                    MenuItem { text: "PDF加工图"; enabled: editorBridge.canSave; onTriggered: drawingOptionsDialog.open() }
                    MenuSeparator {}
                    MenuItem {
                        text: "最近操作说明"; enabled: editorBridge.cadWarnings.length > 0
                        onTriggered: { win.cadReportText = editorBridge.cadWarnings.join("\n\n"); cadReportDialog.open() }
                    }
                }
            }
            BusyIndicator { running: editorBridge.cadBusy; visible: running; Layout.preferredWidth: 24; Layout.preferredHeight: 24 }
            ThemeControls.ThemeLabel {
                objectName: "maskResourceDisplayName"
                property bool operationBarFlexible: true
                visible: win.projectResources === null
                width: Math.min(420, Math.max(180, actions.width * 0.34))
                text: (win.editorBridge.dirty ? "● " : "")
                      + (win.projectResources !== null ? win.projectResources.displayName
                         : (win.editorBridge.informationController.resourceInformation.name || "未命名修正板"))
                textFormat: Text.PlainText
                elide: Text.ElideMiddle
                wrapMode: Text.WrapAnywhere
                color: win.themeColors.colorText
                HoverHandler { id: maskNameHover }
                ToolTip.visible: maskNameHover.hovered && win.editorBridge.documentPath.length > 0
                ToolTip.text: win.editorBridge.documentPath
            }
        }

        SplitView {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: actions.bottom
            anchors.bottom: parent.bottom
            anchors.margins: 12
            orientation: Qt.Horizontal

            ColumnLayout {
                SplitView.fillWidth: true
                SplitView.minimumWidth: 480
                spacing: 6
                Rectangle {
                    id: viewportFrame
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    color: win.themeColors.colorSurface
                    border.color: win.themeColors.colorBorder
                    radius: 6
                    clip: true
                    Canvas {
                        id: maskCanvas
                        objectName: "maskViewportCanvas"
                        anchors.fill: parent
                        onWidthChanged: {
                            if (win.autoFitView) {
                                if (width > 64 && height > 64) win.fitAllGeometry()
                                fitViewTimer.restart()
                            }
                            requestPaint()
                        }
                        onHeightChanged: {
                            if (win.autoFitView) {
                                if (width > 64 && height > 64) win.fitAllGeometry()
                                fitViewTimer.restart()
                            }
                            requestPaint()
                        }
                        onPaint: {
                            let ctx=getContext("2d")
                            ctx.clearRect(0,0,width,height)
                            ctx.font=win.themeColors.fontSizeSmall + "pt \"" + win.themeColors.fontFamily + "\""
                            ctx.lineWidth=1
                            ctx.strokeStyle=win.themeColors.colorBorderStrong
                            ctx.beginPath();ctx.moveTo(0,win.py(0));ctx.lineTo(width,win.py(0));ctx.moveTo(win.px(0),0);ctx.lineTo(win.px(0),height);ctx.stroke()
                            win.drawRulers(ctx)
                            for(let ring of win.rings) {
                                let points=ring.points || []
                                if(!points.length)continue
                                let centerPeak=win.weights.flat_surface === true &&
                                    win.weights.center_is_radial_peak === true &&
                                    Number(win.weights.source_slot) === Number(ring.source_slot)
                                ctx.lineWidth=centerPeak ? 7 : 5.5
                                ctx.strokeStyle=win.themeColors.colorSurface
                                win.drawPolyline(ctx,points,true)
                                ctx.lineWidth=centerPeak ? 4 : 2.5
                                ctx.strokeStyle=win.sourceColor(Number(ring.source_slot||1))
                                win.drawPolyline(ctx,points,true)
                            }
                            let outer=win.doc.outer || [], holes=win.doc.holes || []
                                ctx.strokeStyle=win.contourColor("outer");ctx.lineWidth=2.5
                            win.drawPolyline(ctx,outer,true)
                            for(let i=0;i<outer.length;i++) {
                                    ctx.fillStyle=win.contourColor("outer")
                                    ctx.beginPath();ctx.arc(win.px(outer[i][0]),win.py(outer[i][1]),4,0,Math.PI*2);ctx.fill()
                            }
                            for(let h=0;h<holes.length;h++) {
                                    ctx.strokeStyle=win.contourColor("hole");ctx.lineWidth=2
                                win.drawPolyline(ctx,holes[h],true)
                                for(let i=0;i<holes[h].length;i++) {
                                        ctx.fillStyle=win.contourColor("hole")
                                        ctx.beginPath();ctx.arc(win.px(holes[h][i][0]),win.py(holes[h][i][1]),4,0,Math.PI*2);ctx.fill()
                                }
                            }
                            let draft=win.displayedDraft()
                            if (draft.length) {
                                ctx.strokeStyle=win.contourColor(win.draftPointer==="/outer" ? "outer" : "hole")
                                ctx.lineWidth=2
                                win.drawPolyline(ctx,draft,win.contourMode==="curve" && draft.length>=3)
                                for (let i=0;i<win.draftPoints.length;i++) {
                                    ctx.fillStyle=win.themeColors.colorTextOnAccent
                                    ctx.strokeStyle=win.contourColor(win.draftPointer==="/outer" ? "outer" : "hole")
                                    ctx.lineWidth=1.5
                                    ctx.beginPath();ctx.arc(win.px(win.draftPoints[i][0]),win.py(win.draftPoints[i][1]),4,0,Math.PI*2)
                                    ctx.fill();ctx.stroke()
                                }
                            }
                            if (win.hasAnalysisPoint) {
                                let ax=win.px(win.analysisX), ay=win.py(win.analysisY)
                                ctx.strokeStyle=win.themeColors.colorFocusRing;ctx.lineWidth=2
                                ctx.beginPath();ctx.arc(ax,ay,7,0,Math.PI*2)
                                ctx.moveTo(ax-12,ay);ctx.lineTo(ax+12,ay)
                                ctx.moveTo(ax,ay-12);ctx.lineTo(ax,ay+12);ctx.stroke()
                            }
                            let selectedPoints=win.selectedContourPoints()
                            if (win.selectedPoint>=0 && win.selectedPoint<selectedPoints.length) {
                                let selected=selectedPoints[win.selectedPoint]
                                let selectedKind=win.selectedPolygon==="/outer" ? "outer" : "hole"
                                let sx=win.px(selected[0]), sy=win.py(selected[1])
                                ctx.fillStyle=win.themeColors.colorText
                                ctx.beginPath();ctx.arc(sx,sy,9,0,Math.PI*2);ctx.fill()
                                ctx.fillStyle=win.contourColor(selectedKind)
                                ctx.beginPath();ctx.arc(sx,sy,5.5,0,Math.PI*2);ctx.fill()
                                ctx.fillStyle=win.themeColors.colorText
                                ctx.beginPath();ctx.arc(sx,sy,2,0,Math.PI*2);ctx.fill()
                            }
                            ctx.fillStyle=win.themeColors.colorTextMuted;ctx.fillText("X / mm",width-52,win.py(0)-8);ctx.fillText("Y / mm",win.px(0)+8,18)
                        }
                    }
                    WheelHandler {
                        id: viewportWheel
                        objectName: "maskViewportWheel"
                        target: null
                        acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                        onWheel: function(event) {
                            // The horizontal route owns every frame without a Y component.
                            // This explicit split also applies while this handler is active.
                            if (!win.wheelHasVerticalDelta(event)) {
                                event.accepted=false
                                return
                            }
                            win.traceViewportInput("wheel", {
                                orientation: "vertical",
                                deviceType: String(win.viewportDeviceType(point.device)),
                                deviceTypeProperty: point.device ? String(point.device.deviceType) : "unavailable",
                                typeProperty: point.device ? String(point.device.type) : "unavailable",
                                touchPadConstant: String(PointerDevice.TouchPad),
                                viewportScrollMode: win.viewportScrollMode,
                                pointerType: point.device ? String(point.device.pointerType) : "unknown",
                                maximumPoints: point.device ? point.device.maximumPoints : 0,
                                pixelDelta: [event.pixelDelta.x,event.pixelDelta.y],
                                angleDelta: [event.angleDelta.x,event.angleDelta.y],
                                modifiers: String(event.modifiers),
                                inverted: event.inverted,
                                position: [event.x,event.y]
                            })
                            if (event.y < win.plotContentTop() &&
                                    !win.viewportToolbarBlankAt(event.x,event.y)) {
                                event.accepted=false
                                return
                            }
                            win.navigateWheel(event, !!point.device &&
                                              win.viewportDeviceType(point.device) === PointerDevice.TouchPad)
                        }
                    }
                    WheelHandler {
                        id: viewportHorizontalWheel
                        objectName: "maskViewportHorizontalWheel"
                        target: null
                        orientation: Qt.Horizontal
                        acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
                        onWheel: function(event) {
                            // Diagonal and vertical frames go through the vertical route once.
                            if (!win.wheelHasHorizontalDelta(event) || win.wheelHasVerticalDelta(event)) {
                                event.accepted=false
                                return
                            }
                            if (event.y < win.plotContentTop() &&
                                    !win.viewportToolbarBlankAt(event.x,event.y)) {
                                event.accepted=false
                                return
                            }
                            win.traceViewportInput("wheel", {
                                orientation: "horizontal",
                                deviceType: String(win.viewportDeviceType(point.device)),
                                deviceTypeProperty: point.device ? String(point.device.deviceType) : "unavailable",
                                typeProperty: point.device ? String(point.device.type) : "unavailable",
                                touchPadConstant: String(PointerDevice.TouchPad),
                                viewportScrollMode: win.viewportScrollMode,
                                pointerType: point.device ? String(point.device.pointerType) : "unknown",
                                maximumPoints: point.device ? point.device.maximumPoints : 0,
                                pixelDelta: [event.pixelDelta.x,event.pixelDelta.y],
                                angleDelta: [event.angleDelta.x,event.angleDelta.y],
                                modifiers: String(event.modifiers),
                                inverted: event.inverted,
                                position: [event.x,event.y]
                            })
                            win.navigateWheel(event, !!point.device &&
                                    win.viewportDeviceType(point.device) === PointerDevice.TouchPad)
                        }
                    }
                    PinchHandler {
                        objectName: "maskViewportPinch"
                        target: null
                        acceptedDevices: PointerDevice.TouchPad
                        rotationAxis.enabled: false
                        onScaleChanged: function(delta) {
                            win.traceViewportInput("pinch-scale", {
                                deviceType: String(win.viewportDeviceType(centroid.device)),
                                scaleDelta: delta,
                                activeScale: activeScale,
                                position: [centroid.position.x,centroid.position.y]
                            })
                            if (centroid.position.y < win.plotContentTop() &&
                                    !win.viewportToolbarBlankAt(centroid.position.x,centroid.position.y)) return
                            win.zoomAt(centroid.position.x,centroid.position.y,delta)
                        }
                        onTranslationChanged: function(delta) {
                            if (win.pinchTranslationBaselinePending) {
                                win.pinchTranslationBaselinePending=false
                                win.traceViewportInput("pinch-translation-baseline", {
                                    translationDelta: [delta.x,delta.y],
                                    position: [centroid.position.x,centroid.position.y]
                                })
                                return
                            }
                            win.traceViewportInput("pinch-translation", {
                                deviceType: String(win.viewportDeviceType(centroid.device)),
                                translationDelta: [delta.x,delta.y],
                                position: [centroid.position.x,centroid.position.y]
                            })
                            if (centroid.position.y < win.plotContentTop() &&
                                    !win.viewportToolbarBlankAt(centroid.position.x,centroid.position.y)) return
                            win.panView(delta.x,delta.y)
                        }
                        onActiveChanged: {
                            if (active) {
                                win.autoFitView=false
                                win.pinchTranslationBaselinePending=true
                            } else {
                                win.pinchTranslationBaselinePending=false
                            }
                            win.traceViewportInput("pinch-active", {
                                active: active,
                                deviceType: String(win.viewportDeviceType(centroid.device)),
                                position: [centroid.position.x,centroid.position.y]
                            })
                        }
                        // Native two-finger scroll is delivered as WheelEvent by Qt;
                        // raw multi-point touchpad contacts can also translate here.
                    }
                    MouseArea {
                        objectName: "maskViewportMouse"
                        anchors.fill: parent
                        acceptedButtons: Qt.LeftButton | Qt.MiddleButton
                        hoverEnabled: true
                        preventStealing: win.toolIndex === 3 || (pressedButtons & Qt.MiddleButton) !== 0
                        cursorShape: (win.toolIndex === 3 || (pressedButtons & Qt.MiddleButton) !== 0)
                                     ? (pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor) : Qt.CrossCursor
                        onPressed: function(mouse) {
                            win.lastMouseX=mouse.x;win.lastMouseY=mouse.y
                            if (mouse.y < win.plotContentTop() &&
                                    (!win.viewportToolbarBlankAt(mouse.x,mouse.y) ||
                                     (win.toolIndex !== 3 && mouse.button !== Qt.MiddleButton))) return
                            if(mouse.button===Qt.MiddleButton || win.toolIndex===3) {
                                win.autoFitView=false
                                return
                            }
                            if(win.toolIndex===1||win.toolIndex===2) {
                                win.autoFitView=false
                                win.addPoint(mouse.x,mouse.y)
                            }
                            else if(win.toolIndex===0) {
                                let hit=win.nearest(mouse.x,mouse.y)
                                win.selectedPolygon=hit.pointer;win.selectedPoint=hit.index
                                if(hit.index>=0) {
                                    let polygon=hit.pointer==="/outer" ? win.doc.outer
                                        : win.doc.holes[Number(hit.pointer.split("/")[2])]
                                    win.analysisX=Number(polygon[hit.index][0])
                                    win.analysisY=Number(polygon[hit.index][1])
                                } else {
                                    win.analysisX=Number(win.mmX(mouse.x).toFixed(3))
                                    win.analysisY=Number(win.mmY(mouse.y).toFixed(3))
                                }
                                win.hasAnalysisPoint=true
                                maskCanvas.requestPaint()
                            }
                        }
                        onPositionChanged: function(mouse) {
                            if(!pressed || (win.toolIndex!==3 && !(pressedButtons & Qt.MiddleButton)))return
                            win.panView(mouse.x-win.lastMouseX,mouse.y-win.lastMouseY)
                            win.lastMouseX=mouse.x;win.lastMouseY=mouse.y
                            maskCanvas.requestPaint()
                        }
                    }
                    Item {
                        id: viewportToolbar
                        objectName: "maskViewportFloatingToolbar"
                        z: 4
                        x: 8
                        y: 8
                        width: Math.min(parent.width-16,640)
                        height: toolRows.implicitHeight+16
                        Column {
                            id: toolRows
                            anchors.fill: parent
                            anchors.margins: 8
                            spacing: 4
                            Flow {
                                width: parent.width
                                spacing: 4
                                ViewportToolbarButton { objectName: "coatcontrol:select_tool"; text: "\u9009\u62e9"; primary: win.toolIndex===0; onClicked: win.toolIndex=0 }
                                ViewportToolbarButton { objectName: "coatcontrol:draw_outer"; text: "\u7ed8\u5236\u5916\u8f6e\u5ed3"; primary: win.toolIndex===1; onClicked: win.startContour("/outer") }
                                ViewportToolbarButton { objectName: "coatcontrol:draw_hole"; text: "\u7ed8\u5236\u5b54\u6d1e"; primary: win.toolIndex===2; onClicked: win.startContour("/holes") }
                                ViewportToolbarButton { objectName: "coatcontrol:pan_tool"; text: "\u5e73\u79fb"; primary: win.toolIndex===3; onClicked: win.toolIndex=3 }
                                ViewportToolbarButton { objectName: "coatcontrol:fit_all"; text: "\u9002\u914d\u5168\u90e8"; onClicked: win.resetView() }
                                ViewportToolbarButton {
                                    objectName: "coatcontrol:viewport_scroll_mode"
                                    text: win.viewportScrollMode === "automatic" ? "滚轮：自动" : "滚轮：平移"
                                    primary: win.viewportScrollMode === "pan"
                                    ToolTip.visible: hovered
                                    ToolTip.text: win.viewportScrollMode === "pan"
                                        ? "普通滚轮都平移；Ctrl+滚轮缩放。传统鼠标纵向滚轮也用 Ctrl 缩放，或切换到显式鼠标兼容模式。"
                                        : "显式鼠标兼容模式：Mouse 型纵向滚轮缩放，平台报告为 Mouse 的触控板也会缩放。Ctrl+滚轮始终缩放。"
                                    onClicked: win.viewportScrollMode =
                                        win.viewportScrollMode === "automatic" ? "pan" : "automatic"
                                }
                            }
                            Flow {
                                width: parent.width
                                spacing: 4
                                ViewportToolbarButton { objectName: "coatcontrol:line_mode"; text: "\u76f4\u7ebf"; primary: win.contourMode==="line"; onClicked: { win.contourMode="line"; maskCanvas.requestPaint() } }
                                ViewportToolbarButton { objectName: "coatcontrol:curve_mode"; text: "\u66f2\u7ebf"; primary: win.contourMode==="curve"; onClicked: { win.contourMode="curve"; maskCanvas.requestPaint() } }
                                ViewportToolbarButton { objectName: "coatcontrol:undo_point"; text: "\u64a4\u9500\u4e00\u70b9"; enabled: win.draftPoints.length > 0; onClicked: win.undoDraftPoint() }
                                ViewportToolbarButton { objectName: "coatcontrol:delete_point"; text: "\u5220\u9664\u9009\u4e2d\u70b9"; enabled: win.canDeleteSelectedPoint(); onClicked: win.deleteSelectedPoint() }
                                ViewportToolbarButton { objectName: "coatcontrol:close_contour"; text: "\u95ed\u5408\u8f6e\u5ed3"; enabled: !!win.draftPointer && win.draftPoints.length >= 3; onClicked: win.closeContour() }
                            }
                            ThemeControls.ThemeLabel {
                                objectName: "maskViewportInstructions"
                                text: "\u9ed8\u8ba4\u6eda\u8f6e/\u89e6\u63a7\u677f\u53cc\u6307\u5e73\u79fb\uff1bCtrl+\u6eda\u8f6e\u7f29\u653e\uff0c\u53cc\u6307\u634f\u5408\u7f29\u653e\uff1b\u4e2d\u952e\u6216\u5e73\u79fb\u5de5\u5177\u62d6\u52a8\u3002"
                                width: parent.width
                                wrapMode: Text.WordWrap
                                color: win.themeColors.colorText
                                style: Text.Outline
                                styleColor: Qt.color(win.themeColors.colorSurface)
                            }
                        }
                    }
                }
            }

            Rectangle {
                id: maskEditorSidebar
                objectName: "maskEditorSidebar"
                SplitView.preferredWidth: win.maskSidebarExpanded
                    ? win.maskSidebarExpandedWidth
                    : win.maskSidebarCollapsedWidth
                SplitView.minimumWidth: win.maskSidebarExpanded
                    ? win.maskSidebarCollapsedWidth + 220 + win.themeColors.spaceSm
                    : win.maskSidebarCollapsedWidth
                SplitView.maximumWidth: win.maskSidebarExpanded
                    ? win.maskSidebarCollapsedWidth + 480 + win.themeColors.spaceSm
                    : win.maskSidebarCollapsedWidth
                implicitWidth: win.maskSidebarExpanded
                    ? win.maskSidebarExpandedWidth
                    : win.maskSidebarCollapsedWidth
                implicitHeight: 200
                color: win.themeColors.colorSurface
                border.color: win.themeColors.colorBorder
                radius: win.themeColors.radiusSm

                RowLayout {
                    anchors.fill: parent
                    anchors.margins: win.themeColors.spaceSm
                    spacing: win.maskSidebarExpanded ? win.themeColors.spaceSm : 0

                    ColumnLayout {
                        id: maskSidebarNavigation
                        objectName: "maskSidebarNavigation"
                        Layout.preferredWidth: win.maskSidebarNavigationWidth
                        Layout.minimumWidth: win.maskSidebarNavigationWidth
                        Layout.maximumWidth: win.maskSidebarNavigationWidth
                        Layout.fillHeight: true
                        spacing: win.themeColors.spaceXs
                        MaskSidebarButton {
                            id: maskSidebarTransformButton
                            objectName: "coatcontrol:mask_sidebar_transform"
                            Layout.fillWidth: true
                            implicitWidth: Math.ceil(maskSidebarTransformTextMetrics.advanceWidth)
                                + leftPadding + rightPadding
                            text: "变换操作"
                            primary: win.maskSidebarPage === "transform"
                            checked: win.maskSidebarPage === "transform"
                            Accessible.description: win.maskSidebarExpanded
                                ? "再次选择可收起侧栏内容" : "选择以打开侧栏内容"
                            onClicked: win.toggleMaskSidebar("transform")
                        }
                        MaskSidebarButton {
                            id: maskSidebarAnalysisButton
                            objectName: "coatcontrol:mask_sidebar_analysis"
                            Layout.fillWidth: true
                            implicitWidth: Math.ceil(maskSidebarAnalysisTextMetrics.advanceWidth)
                                + leftPadding + rightPadding
                            text: "遮挡计算"
                            primary: win.maskSidebarPage === "analysis"
                            checked: win.maskSidebarPage === "analysis"
                            Accessible.description: win.maskSidebarExpanded
                                ? "再次选择可收起侧栏内容" : "选择以打开侧栏内容"
                            onClicked: win.toggleMaskSidebar("analysis")
                        }
                        Item { Layout.fillHeight: true }
                    }

                    StackLayout {
                        id: maskSidebarPages
                        objectName: "maskSidebarPages"
                        Layout.fillWidth: win.maskSidebarExpanded
                        Layout.fillHeight: true
                        Layout.minimumWidth: win.maskSidebarExpanded ? 220 : 0
                        Layout.preferredWidth: win.maskSidebarExpanded ? 320 : 0
                        visible: win.maskSidebarExpanded
                        currentIndex: win.maskSidebarPage === "analysis" ? 1 : 0

                        ColumnLayout {
                            objectName: "maskTransformSidebarPage"
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            ScrollView {
                                id: maskTransformSidebarScroll
                                objectName: "maskTransformSidebarScroll"
                                palette: win.palette
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                clip: true
                                contentWidth: availableWidth
                                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                                ColumnLayout {
                                    width: maskTransformSidebarScroll.availableWidth
                                    spacing: 10

                    GroupBox {
                        title: "修正板参数"
                        Layout.fillWidth: true
                        ColumnLayout {
                            anchors.fill: parent
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "高度 Z / mm" }
                                ThemeControls.ThemeField {
                                    objectName: "maskHeightField"
                                    Layout.fillWidth: true
                                    numericDisplayValue: win.doc.z_mm===undefined?"":win.doc.z_mm
                                    onEditingFinished: {
                                        let value=Number(text)
                                        if(numericValueEdited && isFinite(value))editorBridge.setValue("/z_mm",JSON.stringify(value))
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel { text: "外轮廓点："+(win.doc.outer||[]).length+"；孔洞："+(win.doc.holes||[]).length }
                            ThemeControls.ThemeLabel { text: "坐标为修正板平面 XY，单位 mm"; wrapMode: Text.WordWrap }
                        }
                    }

                    GroupBox {
                        title: "轮廓变换"
                        Layout.fillWidth: true
                        ColumnLayout {
                            anchors.fill: parent
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "平移 X" }
                                ThemeControls.ThemeField { id: shiftX; numericDisplayValue: "0"; onTextEdited: numericDisplayValue = text; objectName: "coatcontrol:shift_x_mm"; Layout.minimumWidth: 56; Layout.preferredWidth: 68; Layout.fillWidth: true; text: "0" }
                                ThemeControls.ThemeLabel { text: "Y" }
                                ThemeControls.ThemeField { id: shiftY; numericDisplayValue: "0"; onTextEdited: numericDisplayValue = text; objectName: "coatcontrol:shift_y_mm"; Layout.minimumWidth: 56; Layout.preferredWidth: 68; Layout.fillWidth: true; text: "0" }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "缩放 X" }
                                ThemeControls.ThemeField { id: scaleX; numericDisplayValue: "1"; onTextEdited: numericDisplayValue = text; objectName: "coatcontrol:scale_x"; Layout.minimumWidth: 56; Layout.preferredWidth: 68; Layout.fillWidth: true; text: "1" }
                                ThemeControls.ThemeLabel { text: "Y" }
                                ThemeControls.ThemeField { id: scaleY; numericDisplayValue: "1"; onTextEdited: numericDisplayValue = text; objectName: "coatcontrol:scale_y"; Layout.minimumWidth: 56; Layout.preferredWidth: 68; Layout.fillWidth: true; text: "1" }
                            }
                            ThemeControls.ThemeButton {
                                text: "应用到轮廓和孔洞"
                                Layout.fillWidth: true
                                onClicked: editorBridge.transformMask(Number(win.numericInputText(shiftX)),Number(win.numericInputText(shiftY)),Number(win.numericInputText(scaleX)),Number(win.numericInputText(scaleY)))
                            }
                        }
                    }

                    GroupBox {
                        title: "\u65cb\u8f6c\u8f6e\u5ed3"
                        Layout.fillWidth: true
                        ColumnLayout {
                            anchors.fill: parent
                            ThemeControls.ThemeComboBox {
                                id: rotationContourBox
                                objectName: "coatcontrol:rotation_polygon"
                                Layout.fillWidth: true
                                model: ["\u5916\u8f6e\u5ed3"].concat((win.doc.holes || []).map((_,i)=>"\u5b54\u6d1e " + (i+1)))
                                currentIndex: win.rotationContourIndex
                                onCurrentIndexChanged: { win.rotationContourIndex=currentIndex; win.setRotationCenter() }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "\u65cb\u8f6c\u4e2d\u5fc3 X / mm" }
                                ThemeControls.ThemeField { id: rotationCenterX; numericDisplayValue: "0"; onTextEdited: numericDisplayValue = text; objectName: "coatcontrol:rotation_center_x_mm"; Layout.fillWidth: true; text: "0" }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "Y / mm" }
                                ThemeControls.ThemeField { id: rotationCenterY; numericDisplayValue: "0"; onTextEdited: numericDisplayValue = text; objectName: "coatcontrol:rotation_center_y_mm"; Layout.fillWidth: true; text: "0" }
                                ThemeControls.ThemeLabel { text: "\u89d2\u5ea6 / \u00b0" }
                                ThemeControls.ThemeField { id: rotationAngle; numericDisplayValue: "0"; onTextEdited: numericDisplayValue = text; objectName: "coatcontrol:rotation_angle_deg"; Layout.fillWidth: true; text: "0" }
                            }
                            RowLayout {
                                ThemeControls.ThemeButton { objectName: "coatcontrol:rotation_center_bounds"; text: "\u586b\u5165\u8f6e\u5ed3\u4e2d\u5fc3"; onClicked: win.setRotationCenter() }
                                ThemeControls.ThemeButton { objectName: "coatcontrol:rotate_polygon"; text: "\u65cb\u8f6c\u6240\u9009\u8f6e\u5ed3"; onClicked: win.applyRotation() }
                            }
                            ThemeControls.ThemeLabel { text: "\u6309\u5f53\u524d\u9009\u4e2d\u8f6e\u5ed3\u548c XY \u4e2d\u5fc3\u65cb\u8f6c\uff1b\u5750\u6807\u5355\u4f4d\u4e3a mm"; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                        }
                    }

                                }
                            }

                        }

                        ColumnLayout {
                            objectName: "maskAnalysisSidebarPage"
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            ScrollView {
                                id: maskAnalysisSidebarScroll
                                objectName: "maskAnalysisSidebarScroll"
                                palette: win.palette
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                clip: true
                                contentWidth: availableWidth
                                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                                ColumnLayout {
                                    width: maskAnalysisSidebarScroll.availableWidth
                                    spacing: 10

                    GroupBox {
                        title: "机器与盘心遮挡环"
                        Layout.fillWidth: true
                        ColumnLayout {
                            anchors.fill: parent
                            ThemeControls.ThemeLabel {
                                text: editorBridge.machinePath || "尚未加载机器"
                                Layout.fillWidth: true
                                elide: Text.ElideMiddle
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeButton { text: "更新盘心遮挡环"; enabled: !!editorBridge.machinePath; onClicked: editorBridge.refreshRings() }
                                Item { Layout.fillWidth: true }
                                ThemeControls.ThemeLabel { text: "源数 " + (win.machine.sources||[]).length }
                            }
                            Repeater {
                                model: win.machine.sources || []
                                delegate: RowLayout {
                                    Rectangle {
                                        implicitWidth: 14;implicitHeight:14;radius:7
                                        color: win.sourceColor(index+1)
                                        border.width: 1
                                        border.color: win.themeColors.colorText
                                    }
                                    ThemeControls.ThemeLabel {
                                        Layout.fillWidth: true
                                        text: "源 " +(index+1)+" · "+(modelData.file||"")+" (X="+modelData.x_mm+", Y="+modelData.y_mm+", Z="+(modelData.z_mm===undefined?0:modelData.z_mm)+" mm)"
                                        wrapMode: Text.WrapAnywhere
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel {
                                text: "彩色环是各源中心射向行星盘盘心的板面交点轨迹；粗线表示所选源在本次平面盘径向样本中盘心权重达到最高。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                        }
                    }

                    GroupBox {
                        title: "遮挡权重"
                        Layout.fillWidth: true
                        ColumnLayout {
                            anchors.fill: parent
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "工件盘直径 / mm" }
                                ThemeControls.ThemeField {
                                    id: workpieceDiameter
                                    objectName: "coatcontrol:workpiece_diameter_mm"
                                    Layout.minimumWidth: 56
                                    Layout.preferredWidth: 68
                                    Layout.fillWidth: true
                                    numericDisplayValue: editorBridge.workpieceDiameterMm
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                    onEditingFinished: {
                                        if (numericValueEdited)
                                            editorBridge.setWorkpieceDiameter(Number(win.numericInputText(workpieceDiameter)))
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel {
                                text: "仅控制遮挡分析范围，不写入修正板文件或机器几何。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            ThemeControls.ThemeLabel {
                                visible: !!editorBridge.machinePath && win.returnCarrierRevolutions() > 0
                                text: "完整点回归周期：公转 " + win.returnCarrierRevolutions() +
                                      " 圈（齿数 " + win.machine.sun_teeth + ":" +
                                      win.machine.planet_teeth + "）。拟用相位样本：整板径向和角向热图各 " +
                                      (79 * win.returnCarrierRevolutions()) + " 点，选点 " +
                                      (599 * win.returnCarrierRevolutions()) +
                                      " 点。转速只影响真实历时，不影响遮挡权重。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            ThemeControls.ThemeLabel {
                                visible: win.samplingSummary().length > 0
                                text: "本次相位采样：" + win.samplingSummary()
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "板位次" }
                                ThemeControls.ThemeSpinBox {
                                    id: maskSlot
                                    objectName: "coatcontrol:mask_slot"
                                    Layout.minimumWidth: 56
                                    Layout.preferredWidth: 68
                                    Layout.fillWidth: true
                                    from: 1
                                    to: Math.max(1,(win.machine.masks||[]).length)
                                    value: 1
                                }
                                ThemeControls.ThemeLabel { text: "源位次" }
                                ThemeControls.ThemeSpinBox {
                                    id: sourceSlot
                                    objectName: "coatcontrol:source_slot"
                                    Layout.minimumWidth: 56
                                    Layout.preferredWidth: 68
                                    Layout.fillWidth: true
                                    from: 1
                                    to: Math.max(1,(win.machine.sources||[]).length)
                                    value: 1
                                }
                            }
                            ThemeControls.ThemeButton {
                                text: "计算整板径向、当前半径角向与盘面遮挡"
                                enabled: !!editorBridge.machinePath && editorBridge.canSave &&
                                         !editorBridge.radialBusy && !editorBridge.fieldBusy
                                Layout.fillWidth: true
                                onClicked: {
                                    editorBridge.computeWeights(maskSlot.value,sourceSlot.value)
                                    win.requestAngularField()
                                }
                            }
                            ThemeControls.ThemeLabel {
                                visible: !!editorBridge.machinePath
                                text: {
                                    let revision = editorBridge.estimateRevision
                                    return editorBridge.estimateBoardTime(sourceSlot.value)
                                }
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            ThemeControls.ThemeLabel {
                                visible: win.busySummary().length > 0
                                text: "正在计算：" + win.busySummary()
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "距行星盘心半径" }
                                Item { Layout.fillWidth: true }
                                ThemeControls.ThemeLabel {
                                    text: win.selectedAngularRadiusMm().toFixed(1) + " / " +
                                          (Number(editorBridge.workpieceDiameterMm) / 2).toFixed(1) + " mm"
                                }
                            }
                            Slider {
                                id: angularRadiusSlider
                                objectName: "coatcontrol:angular_radius_mm"
                                Layout.fillWidth: true
                                enabled: !editorBridge.fieldBusy
                                from: 0
                                to: Math.max(0, Number(editorBridge.workpieceDiameterMm) / 2)
                                stepSize: 0.1
                                value: Number(editorBridge.workpieceDiameterMm) / 4
                                onValueChanged: {
                                    if (angularCanvas) angularCanvas.requestPaint()
                                }
                                onMoved: {
                                    if (pressed) {
                                        win.angularRadiusPending = true
                                        angularRecomputeTimer.stop()
                                    } else {
                                        angularRecomputeTimer.restart()
                                    }
                                }
                                onPressedChanged: {
                                    if (!pressed && win.angularRadiusPending) {
                                        win.angularRadiusPending = false
                                        win.requestAngularField()
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel {
                                text: angularRadiusSlider.pressed ? "松开后更新曲线" :
                                      angularRecomputeTimer.running ? "停止调节后更新曲线" :
                                      "滑动选择半径；角向曲线按当前半径计算。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            ThemeControls.ThemeLabel {
                                text: win.hasAnalysisPoint
                                      ? "选点：(" + win.analysisX.toFixed(2) + ", "
                                        + win.analysisY.toFixed(2) + ") mm"
                                      : "在左侧画布的选点模式点击分析位置"
                                Layout.fillWidth: true
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel { text: "选点局部半径 / mm" }
                                ThemeControls.ThemeField {
                                    id: patchRadius
                                        numericDisplayValue: "5"
                                        onTextEdited: numericDisplayValue = text
                                    objectName: "coatcontrol:patch_radius_mm"
                                    Layout.minimumWidth: 56
                                    Layout.preferredWidth: 68
                                    Layout.fillWidth: true
                                    text: "5"
                                    inputMethodHints: Qt.ImhFormattedNumbersOnly
                                }
                            }
                            ThemeControls.ThemeButton {
                                text: "计算选点潜在遮挡权重"
                                enabled: !!editorBridge.machinePath && win.hasAnalysisPoint &&
                                         !editorBridge.pointBusy
                                Layout.fillWidth: true
                                onClicked: editorBridge.computePointWeights(
                                    win.analysisX, win.analysisY,
                                    Number(win.numericInputText(patchRadius)), sourceSlot.value)
                            }
                            ThemeControls.ThemeLabel {
                                visible: !!editorBridge.machinePath
                                text: {
                                    let revision = editorBridge.estimateRevision
                                    return editorBridge.estimatePointTime(sourceSlot.value)
                                }
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            ThemeControls.ThemeLabel {
                                text: "橙色：当前整板实际遮挡；蓝色：选点附近小圆的潜在遮挡。两者均以无板通量为基准。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            ThemeControls.ThemeLabel {
                                visible: win.weights.center_is_radial_peak !== undefined
                                text: {
                                    if (!win.weights.flat_surface)
                                        return "盘心遮挡环的最高权重判定仅适用于平面盘。"
                                    if (Number(win.weights.radial_peak_weight_percent) <= 0)
                                        return "本次径向样本没有有效遮挡；盘心环只显示几何位置。"
                                    if (win.weights.center_is_radial_peak === null)
                                        return "本次径向样本没有有效盘心，无法判定盘心是否达峰值。"
                                    let center=Number(win.weights.center_weight_percent).toFixed(2)
                                    let peak=Number(win.weights.radial_peak_weight_percent).toFixed(2)
                                    let radius=Number(win.weights.radial_peak_radius_mm).toFixed(1)
                                    let prefix="源 " + win.weights.source_slot + " 的盘心遮挡权重 " + center + "%"
                                    return win.weights.center_is_radial_peak ?
                                        prefix + "，达到本次径向采样最高值。" :
                                        prefix + "；本次采样最高为 " + peak + "%（半径 " + radius + " mm）。"
                                }
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            ColumnLayout {
                                Layout.fillWidth: true
                                RowLayout {
                                    Layout.fillWidth: true
                                    CheckBox {
                                        id: autoWeightY
                                        objectName: "coatcontrol:auto_weight_y"
                                        text: "Y 轴自动"
                                        checked: true
                                        onCheckedChanged: {
                                            if (!checked && weightYMin && weightYMax) {
                                                let range = win.automaticWeightYRange()
                                                weightYMin.setNumericSourceValue(range[0])
                                                weightYMax.setNumericSourceValue(range[1])
                                            }
                                            if (weightCanvas) weightCanvas.requestPaint()
                                        }
                                    }
                                    Item { Layout.fillWidth: true }
                                    ThemeControls.ThemeButton {
                                        text: "恢复自动"
                                        enabled: !autoWeightY.checked
                                        onClicked: autoWeightY.checked = true
                                    }
                                }
                                RowLayout {
                                    Layout.fillWidth: true
                                    ThemeControls.ThemeLabel { text: "最小 / %" }
                                    ThemeControls.ThemeField {
                                        id: weightYMin
                                        numericDisplayValue: "0"
                                        objectName: "coatcontrol:weight_y_min"
                                    Layout.minimumWidth: 56
                                    Layout.preferredWidth: 68
                                        Layout.fillWidth: true
                                        text: "0"
                                        enabled: !autoWeightY.checked
                                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                                        onTextEdited: { numericDisplayValue = text; weightCanvas.requestPaint() }
                                    }
                                    ThemeControls.ThemeLabel { text: "最大 / %" }
                                    ThemeControls.ThemeField {
                                        id: weightYMax
                                        numericDisplayValue: "100"
                                        objectName: "coatcontrol:weight_y_max"
                                    Layout.minimumWidth: 56
                                    Layout.preferredWidth: 68
                                        Layout.fillWidth: true
                                        text: "100"
                                        enabled: !autoWeightY.checked
                                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                                        onTextEdited: { numericDisplayValue = text; weightCanvas.requestPaint() }
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel {
                                visible: !autoWeightY.checked &&
                                         (!(win.numericInputText(weightYMin).trim().length && win.numericInputText(weightYMax).trim().length) ||
                                          !isFinite(Number(win.numericInputText(weightYMin))) || !isFinite(Number(win.numericInputText(weightYMax))) ||
                                          Number(win.numericInputText(weightYMax)) <= Number(win.numericInputText(weightYMin)))
                                text: "Y 轴最小值须小于最大值；输入有效范围后更新曲线。"
                                color: win.themeColors.colorError
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            Canvas {
                                id: weightCanvas
                                Layout.fillWidth: true
                                Layout.preferredHeight: 210
                                onWidthChanged: requestPaint()
                                onHeightChanged: requestPaint()
                                onPaint: {
                                    let ctx=getContext("2d")
                                    ctx.clearRect(0,0,width,height)
                                    let boardRadii=win.weights.radii_mm||[]
                                    let pointRadii=win.pointWeights.radii_mm||[]
                                    let radii=boardRadii.concat(pointRadii).map(Number).filter(isFinite)

                                    let axisColor=win.themeColors.colorBorderStrong
                                    let gridColor=win.themeColors.colorBorder
                                    let left=66, right=12, top=12, bottom=30
                                    let plotWidth=Math.max(1,width-left-right)
                                    let plotHeight=Math.max(1,height-top-bottom)
                                    let autoRange=win.automaticWeightYRange()
                                    let manualValid=win.numericInputText(weightYMin).trim().length && win.numericInputText(weightYMax).trim().length &&
                                        isFinite(Number(win.numericInputText(weightYMin))) && isFinite(Number(win.numericInputText(weightYMax))) &&
                                        Number(win.numericInputText(weightYMax))>Number(win.numericInputText(weightYMin))
                                    let range=autoWeightY.checked || !manualValid ? autoRange :
                                              [Number(win.numericInputText(weightYMin)),Number(win.numericInputText(weightYMax))]
                                    let minY=range[0],maxY=range[1]
                                    ctx.font=win.themeColors.fontSizeSmall + "pt \"" + win.themeColors.fontFamily + "\""
                                    ctx.fillStyle=win.themeColors.colorTextMuted
                                    for(let i=0;i<=4;i++) {
                                        let value=minY+(maxY-minY)*i/4
                                        let y=top+plotHeight*(1-i/4)
                                        ctx.strokeStyle=i===0 ? axisColor : gridColor
                                        ctx.lineWidth=1
                                        ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(width-right,y);ctx.stroke()
                                        ctx.textAlign="right"
                                        ctx.fillText(value.toFixed(2)+"%",left-7,y+4)
                                    }
                                    ctx.strokeStyle=axisColor;ctx.lineWidth=1
                                    ctx.beginPath();ctx.moveTo(left,top);ctx.lineTo(left,top+plotHeight);ctx.stroke()
                                    let minR=0,maxR=Number(editorBridge.workpieceDiameterMm)/2
                                    if(maxR<=minR)return
                                    ctx.save()
                                    ctx.beginPath();ctx.rect(left,top,plotWidth,plotHeight);ctx.clip()
                                    function plot(result,color) {
                                        let rr=result.radii_mm||[], values=result.weights_percent||[]
                                        if(rr.length<2||values.length!==rr.length)return
                                        ctx.strokeStyle=color;ctx.lineWidth=2;ctx.beginPath()
                                        let started=false
                                        for(let i=0;i<rr.length;i++) {
                                            if(values[i]===null || !isFinite(Number(values[i])) ||
                                               !isFinite(Number(rr[i]))) { started=false; continue }
                                            let x=left+(Number(rr[i])-minR)/(maxR-minR)*plotWidth
                                            let y=top+(maxY-Number(values[i]))/(maxY-minY)*plotHeight
                                            if(!started)ctx.moveTo(x,y);else ctx.lineTo(x,y)
                                            started=true
                                        }
                                        ctx.stroke()
                                    }
                                    plot(win.weights,win.themeColors.colorWarning)
                                    plot(win.pointWeights,win.themeColors.colorInfo)
                                    ctx.restore()
                                    ctx.fillStyle=win.themeColors.colorTextMuted;ctx.textAlign="left"
                                    ctx.fillText(minR.toFixed(0)+" mm",left,height-8)
                                    ctx.textAlign="right"
                                    ctx.fillText(maxR.toFixed(0)+" mm",width-right,height-8)
                                }
                            }
                            ThemeControls.ThemeLabel {
                                text: "当前半径的角向遮挡权重（0–360°）"
                                Layout.fillWidth: true
                                font.bold: true
                            }
                            ThemeControls.ThemeLabel {
                                text: "横轴：工件盘材料角度 / °；纵轴：无板通量中的遮挡比例 / %"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            Canvas {
                                id: angularCanvas
                                Layout.fillWidth: true
                                Layout.preferredHeight: 240
                                onWidthChanged: requestPaint()
                                onHeightChanged: requestPaint()
                                onPaint: {
                                    let ctx=getContext("2d")
                                    ctx.clearRect(0,0,width,height)

                                    let axis=win.themeColors.colorBorderStrong
                                    let grid=win.themeColors.colorBorder
                                    let left=58,right=14,top=14,bottom=31
                                    let pw=Math.max(1,width-left-right),ph=Math.max(1,height-top-bottom)
                                    let range=win.angularYRange(),lo=range[0],hi=range[1]
                                    if(hi<=lo)hi=lo+1
                                    ctx.font=win.themeColors.fontSizeSmall + "pt \"" + win.themeColors.fontFamily + "\""
                                    for(let i=0;i<=4;i++) {
                                        let y=top+ph*(1-i/4),value=lo+(hi-lo)*i/4
                                        ctx.strokeStyle=i===0?axis:grid
                                        ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(left+pw,y);ctx.stroke()
                                        ctx.fillStyle=win.themeColors.colorTextMuted;ctx.textAlign="right"
                                        ctx.fillText(value.toFixed(1)+"%",left-6,y+4)
                                        let x=left+pw*i/4
                                        ctx.strokeStyle=grid
                                        ctx.beginPath();ctx.moveTo(x,top);ctx.lineTo(x,top+ph);ctx.stroke()
                                        ctx.textAlign="center"
                                        ctx.fillText((90*i)+"°",x,height-8)
                                    }
                                    let angles=win.fieldMatchesSelectedRadius() ?
                                               (win.field.azimuths_deg||[]) : []
                                    let curves=win.field.angular_weights_percent||[]
                                    ctx.save();ctx.beginPath();ctx.rect(left,top,pw,ph);ctx.clip()
                                    let values=curves[0]||[]
                                    ctx.strokeStyle=win.ringColor(0);ctx.lineWidth=2
                                    ctx.beginPath();let started=false
                                    for(let j=0;j<Math.min(angles.length,values.length);j++) {
                                        if(values[j]===null||!isFinite(Number(values[j]))) { started=false;continue }
                                        let x=left+Number(angles[j])/360*pw
                                        let y=top+(hi-Number(values[j]))/(hi-lo)*ph
                                        if(started)ctx.lineTo(x,y);else ctx.moveTo(x,y)
                                        started=true
                                    }
                                    ctx.stroke()
                                    ctx.restore()
                                }
                            }
                            ThemeControls.ThemeLabel {
                                text: win.fieldMatchesSelectedRadius() ?
                                      "曲线半径：" + Number(win.field.ring_radii_mm[0]).toFixed(1) + " mm" :
                                      "当前半径尚无曲线；松开滑块或点击计算。"
                                Layout.fillWidth: true
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                ThemeControls.ThemeLabel {
                                    text: "整盘遮挡权重热力图"
                                    font.bold: true
                                }
                                Item { Layout.fillWidth: true }
                                ThemeControls.ThemeComboBox {
                                    id: heatWeightMode
                                    objectName: "coatcontrol:heat_weight_mode"
                                    model: ["绝对权重", "归一化相对权重"]
                                    onCurrentIndexChanged: {
                                        if (heatCanvas) heatCanvas.requestPaint()
                                    }
                                }
                            }
                            ThemeControls.ThemeLabel {
                                text: heatWeightMode.currentIndex === 0 ?
                                      "绝对权重 W = 该材料点被当前板挡住的源通量 ÷ 无板源通量 × 100%。" :
                                      "相对权重 W_rel = W ÷ 整盘有效点中的最大 W × 100%；最大 W 为 0 时，有效点均为 0%。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            ThemeControls.ThemeLabel {
                                visible: heatWeightMode.currentIndex === 1 &&
                                         win.field.relative_normalization_max_percent !== undefined
                                text: "本次归一化基准：最大绝对权重 " +
                                      Number(win.field.relative_normalization_max_percent || 0).toFixed(2) + "%"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            ThemeControls.ThemeLabel {
                                text: "机器固定源与修正板；图上坐标为工件盘材料位置 / mm"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                            Canvas {
                                id: heatCanvas
                                Layout.fillWidth: true
                                Layout.preferredHeight: 350
                                onWidthChanged: requestPaint()
                                onHeightChanged: requestPaint()
                                onPaint: {
                                    let ctx=getContext("2d")
                                    ctx.clearRect(0,0,width,height)

                                    let axis=win.themeColors.colorBorderStrong
                                    let left=43,top=16,bottom=38
                                    let side=Math.max(1,Math.min(width-left-87,height-top-bottom))
                                    let radius=Number(editorBridge.workpieceDiameterMm)/2
                                    let axisValues=win.field.grid_x_mm||[]
                                    let rows=heatWeightMode.currentIndex === 0 ?
                                             (win.field.absolute_grid_weights_percent ||
                                              win.field.grid_weights_percent || []) :
                                             (win.field.relative_grid_weights_percent || [])
                                    let n=axisValues.length
                                    ctx.save()
                                    ctx.beginPath();ctx.arc(left+side/2,top+side/2,side/2,0,Math.PI*2);ctx.clip()
                                    if(n>1 && rows.length===n) {
                                        let cell=side/(n-1)
                                        for(let j=0;j<n;j++) for(let i=0;i<n;i++) {
                                            let v=(rows[j]||[])[i]
                                            if(v===null||v===undefined||!isFinite(Number(v)))continue
                                            ctx.fillStyle=win.heatColor(v)
                                            ctx.fillRect(left+i*cell-cell/2,
                                                         top+(n-1-j)*cell-cell/2,cell+1,cell+1)
                                        }
                                    }
                                    ctx.restore()
                                    ctx.strokeStyle=axis;ctx.lineWidth=1
                                    ctx.beginPath();ctx.arc(left+side/2,top+side/2,side/2,0,Math.PI*2);ctx.stroke()
                                    ctx.beginPath();ctx.moveTo(left,top+side);ctx.lineTo(left+side,top+side)
                                    ctx.moveTo(left,top);ctx.lineTo(left,top+side);ctx.stroke()
                                    ctx.fillStyle=win.themeColors.colorTextMuted;ctx.font=win.themeColors.fontSizeSmall + "pt \"" + win.themeColors.fontFamily + "\""
                                    ctx.textAlign="left";ctx.fillText((-radius).toFixed(0),left,top+side+15)
                                    ctx.textAlign="center";ctx.fillText("0",left+side/2,top+side+15)
                                    ctx.textAlign="right";ctx.fillText(radius.toFixed(0),left+side,top+side+15)
                                    ctx.textAlign="center";ctx.fillText("X / mm",left+side/2,height-5)
                                    ctx.textAlign="right";ctx.fillText(radius.toFixed(0),left-5,top+4)
                                    ctx.fillText("0",left-5,top+side/2+4)
                                    ctx.fillText((-radius).toFixed(0),left-5,top+side+4)
                                    ctx.save();ctx.translate(11,top+side/2);ctx.rotate(-Math.PI/2)
                                    ctx.textAlign="center";ctx.fillText("Y / mm",0,0);ctx.restore()
                                    let barX=left+side+14,barW=15
                                    let grad=ctx.createLinearGradient(0,top+side,0,top)
                                    grad.addColorStop(0,win.heatColor(0))
                                    grad.addColorStop(0.25,win.heatColor(25))
                                    grad.addColorStop(0.5,win.heatColor(50))
                                    grad.addColorStop(0.75,win.heatColor(75))
                                    grad.addColorStop(1,win.heatColor(100))
                                    ctx.fillStyle=grad;ctx.fillRect(barX,top,barW,side)
                                    ctx.fillStyle=win.themeColors.colorTextMuted;ctx.textAlign="left"
                                    for(let k=0;k<=4;k++)
                                        ctx.fillText((100-25*k)+"%",barX+barW+3,top+side*k/4+4)
                                }
                            }
                            ThemeControls.ThemeLabel {
                                text: "透明区域：盘外或基片无效点；色标 0–100%，单位为%。"
                                Layout.fillWidth: true
                                wrapMode: Text.WordWrap
                            }
                        }
                    }
                                }
                            }
                        }
                    }
                }
            }
        }

    }
}
