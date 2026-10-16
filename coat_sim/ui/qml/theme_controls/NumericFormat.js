.pragma library

// Format numbers for display only. Numeric inputs keep their original source
// value while focused; this helper is used only for the unfocused presentation.
function format(value, locale, decimals) {
    const precision = decimals === undefined ? 3 : decimals
    if (typeof precision !== "number" || !Number.isInteger(precision)
            || precision < 0 || precision > 100)
        throw new RangeError("decimals must be an integer from 0 to 100")

    if (value === null || value === undefined)
        return ""

    const originalText = String(value)
    if (typeof value === "string" && value.trim().length === 0)
        return originalText
    if (typeof value !== "number" && typeof value !== "string")
        return originalText

    const number = Number(value)
    if (Number.isNaN(number))
        return originalText
    if (!Number.isFinite(number))
        return String(number)

    const decimalPoint = locale && locale.decimalPoint
            ? locale.decimalPoint : "."
    if (number !== 0 && Number(number.toFixed(precision)) === 0) {
        return trimFraction(number.toLocaleString(locale, "e", precision),
                            decimalPoint)
    }
    return trimFraction(number.toLocaleString(locale, "f", precision),
                        decimalPoint)
}

function trimFraction(text, decimalPoint) {
    const decimalIndex = text.lastIndexOf(decimalPoint)
    if (decimalIndex < 0)
        return text

    const exponentIndex = text.search(/[eE]/)
    const fractionEnd = exponentIndex >= 0 ? exponentIndex : text.length
    const fraction = text.slice(decimalIndex + decimalPoint.length, fractionEnd)
    const trimmed = fraction.replace(/0+$/, "")
    if (trimmed.length === fraction.length)
        return text
    return text.slice(0, decimalIndex)
            + (trimmed.length ? decimalPoint + trimmed : "")
            + text.slice(fractionEnd)
}
