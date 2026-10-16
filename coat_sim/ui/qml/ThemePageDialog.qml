import QtQuick
import QtQuick.Controls
import "theme_controls" as ThemeControls

// Page-owned popup base. Keep standalone dialogs on the same live, read-only
// theme snapshot and palette roles as their owning AppWindow.
ThemeControls.ThemeDialogBase {
    palette.light: Theme.snapshot.colorSurface
    palette.link: Theme.snapshot.colorAccent
    palette.linkVisited: Theme.snapshot.colorAccentPressed
    palette.brightText: Theme.snapshot.colorError

    palette.disabled.windowText: Theme.snapshot.colorDisabledText
    palette.disabled.text: Theme.snapshot.colorDisabledText
    palette.disabled.buttonText: Theme.snapshot.colorDisabledText
    palette.disabled.placeholderText: Theme.snapshot.colorDisabledText
    palette.disabled.base: Theme.snapshot.colorDisabledSurface
    palette.disabled.alternateBase: Theme.snapshot.colorDisabledSurface
    palette.disabled.button: Theme.snapshot.colorDisabledSurface
    palette.disabled.window: Theme.snapshot.colorDisabledSurface
}
