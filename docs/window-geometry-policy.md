# Shared top-level window geometry

The launcher applies a shared geometry policy to ordinary business windows
after QML incubation completes and before the window is shown.

## First open and restore

- A module with no saved geometry opens at **1100 × 760 logical pixels**,
  centered on the launcher's screen.
- Qt screen and window geometry are already in device-independent logical
  pixels. The policy does not multiply the default by the device pixel ratio.
- The rectangle and the page's minimum size are clamped to that screen's
  `availableGeometry()`. If the current screen is smaller than a page's QML
  minimum, that minimum is lowered to the available dimension so the window
  remains on screen.
- User moves and resizes are saved per module under
  `ui/window_geometry/v1/<module-key>`. The next instance restores that normal
  windowed rectangle and clamps it to the current screen. Maximized geometry is
  not saved as a normal rectangle.
- First-open defaults apply only when no saved rectangle exists. The launcher
  never reapplies defaults to an already-open window.
- Dialogs, tray surfaces, and assistant floating controls do not use this
  business-window policy.

FilmDesignQuick already owns a `Qt.labs.settings` geometry record. It marks
`pageManagesWindowGeometry: true`, so the launcher preserves that restored
rectangle and only clamps it. Its no-record fallback uses the shared 1100 × 760
logical default.

## Interface for module owners

The launcher invokes this automatically for its QML module windows. A module
that creates a separate top-level window should keep it hidden, call the helper,
then show it:

```python
from coat_sim.ui.window_geometry import WindowGeometryManager

geometry = WindowGeometryManager(parent=window)
geometry.prepare(window, "film_spectrum", screen=screen)
window.show()
```

Inside the shared QML engine, the bridge exposes the same operation:

```qml
Component.onCompleted: {
    launcher.prepareWindowGeometry(window, "film_spectrum", false)
    window.show()
}
```

Pass `true` as the third argument when the page has already restored its own
geometry. The helper keeps that rectangle where possible and clamps it to the
active screen without replacing it with the module default.
