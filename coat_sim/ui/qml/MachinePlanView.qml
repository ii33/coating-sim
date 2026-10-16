import QtQuick

// Fixed-machine-frame top view. All coordinates are supplied in mm; this
// component only maps existing geometry to pixels and never changes a design.
Canvas {
    id: view
    property var plan: ({})
    property color ink: "#253b47"
    property color axisColor: "#81939d"
    property color orbitColor: "#4d879e"
    property var sourceColors: ["#df5a5a", "#e49b38", "#42b381", "#568edf",
                                "#a97adf", "#c8679d", "#38a5b5", "#9aab34"]
    property var maskColors: ["#238b6c", "#a65ca7"]
    readonly property real plotMargin: 34
    onPlanChanged: requestPaint()
    onInkChanged: requestPaint()
    onAxisColorChanged: requestPaint()
    onOrbitColorChanged: requestPaint()
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()

    function geometryBounds() {
        const cx = Number(plan.carrier_x_mm || 0)
        const cy = Number(plan.carrier_y_mm || 0)
        const orbit = Math.abs(Number(plan.planet_offset_mm || 0))
        let bounds = [cx - orbit, cy - orbit, cx + orbit, cy + orbit]
        function add(x, y) {
            if (!Number.isFinite(x) || !Number.isFinite(y)) return
            bounds[0] = Math.min(bounds[0], x)
            bounds[1] = Math.min(bounds[1], y)
            bounds[2] = Math.max(bounds[2], x)
            bounds[3] = Math.max(bounds[3], y)
        }
        for (let item of (plan.sources || []).concat(plan.monitors || []))
            add(Number(item.x_mm), Number(item.y_mm))
        for (let mask of (plan.masks || []))
            for (let ring of [mask.outer || []].concat(mask.holes || []))
                for (let point of ring) add(Number(point[0]), Number(point[1]))
        return bounds
    }
    function plotScale() {
        const b = geometryBounds()
        return Math.max(0, Math.min((width - 2 * plotMargin) / Math.max(1, b[2] - b[0]),
                                   (height - 2 * plotMargin) / Math.max(1, b[3] - b[1])))
    }
    function pixelX(x) {
        const b = geometryBounds()
        return width / 2 + (x - (b[0] + b[2]) / 2) * plotScale()
    }
    function pixelY(y) {
        const b = geometryBounds()
        return height / 2 - (y - (b[1] + b[3]) / 2) * plotScale()
    }
    onPaint: {
        const ctx = getContext("2d")
        ctx.clearRect(0, 0, width, height)
        if (plan.message) return
        const bounds = geometryBounds()
        const scale = plotScale()
        if (!Number.isFinite(scale) || scale <= 0) return
        const cx = Number(plan.carrier_x_mm || 0)
        const cy = Number(plan.carrier_y_mm || 0)
        const orbit = Math.abs(Number(plan.planet_offset_mm || 0))
        function X(x) { return width / 2 + (x - (bounds[0] + bounds[2]) / 2) * scale }
        function Y(y) { return height / 2 - (y - (bounds[1] + bounds[3]) / 2) * scale }
        function label(text, x, y, below) {
            const w = ctx.measureText(text).width
            let left = x + 9
            if (left + w > width - 6) left = x - w - 9
            const top = Math.max(15, Math.min(height - 7, y + (below ? 17 : -9)))
            ctx.fillText(text, Math.max(6, Math.min(width - w - 6, left)), top)
        }
        function ring(points) {
            if (points.length < 2) return
            ctx.beginPath()
            ctx.moveTo(X(Number(points[0][0])), Y(Number(points[0][1])))
            for (let i = 1; i < points.length; ++i)
                ctx.lineTo(X(Number(points[i][0])), Y(Number(points[i][1])))
            ctx.closePath()
            ctx.stroke()
        }
        ctx.lineWidth = 1
        ctx.strokeStyle = axisColor
        ctx.beginPath()
        if (X(0) >= 0 && X(0) <= width) { ctx.moveTo(X(0), 0); ctx.lineTo(X(0), height) }
        if (Y(0) >= 0 && Y(0) <= height) { ctx.moveTo(0, Y(0)); ctx.lineTo(width, Y(0)) }
        ctx.stroke()
        ctx.lineWidth = 2
        ctx.strokeStyle = orbitColor
        ctx.beginPath()
        ctx.arc(X(cx), Y(cy), orbit * scale, 0, 2 * Math.PI)
        ctx.stroke()
        ctx.fillStyle = orbitColor
        ctx.beginPath(); ctx.arc(X(cx), Y(cy), 4, 0, 2 * Math.PI); ctx.fill()
        ctx.font = "12px sans-serif"
        label("盘心", X(cx), Y(cy), false)
        const masks = plan.masks || []
        for (let i = 0; i < masks.length; ++i) {
            const mask = masks[i]
            ctx.strokeStyle = maskColors[i % maskColors.length]
            ctx.fillStyle = ctx.strokeStyle
            ctx.lineWidth = 2
            ctx.setLineDash([])
            ring(mask.outer || [])
            ctx.setLineDash([4, 3])
            for (let hole of (mask.holes || [])) ring(hole)
            ctx.setLineDash([])
            if ((mask.outer || []).length) {
                const point = mask.outer[0]
                label("板 " + (mask.display_slot || i + 1), X(Number(point[0])), Y(Number(point[1])), true)
            }
        }
        const sources = plan.sources || []
        for (let i = 0; i < sources.length; ++i) {
            const source = sources[i], x = X(Number(source.x_mm)), y = Y(Number(source.y_mm))
            ctx.fillStyle = sourceColors[i % sourceColors.length]
            ctx.beginPath(); ctx.arc(x, y, 6, 0, 2 * Math.PI); ctx.fill()
            ctx.fillStyle = ink
            label("源 " + (source.slot || i + 1), x, y, false)
        }
        const monitors = plan.monitors || []
        for (let i = 0; i < monitors.length; ++i) {
            const monitor = monitors[i], x = X(Number(monitor.x_mm)), y = Y(Number(monitor.y_mm))
            ctx.fillStyle = "#8865c4"
            ctx.fillRect(x - 4, y - 4, 8, 8)
            ctx.fillStyle = ink
            label("监测点 " + (monitor.slot || i + 1), x, y, true)
        }
        ctx.fillStyle = ink
        ctx.fillText("+Y", 8, 17)
        ctx.fillText("+X", width - 26, height - 8)
        ctx.fillText("XY / mm", 8, height - 8)
    }
}
