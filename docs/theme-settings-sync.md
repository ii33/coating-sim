# Cross-process theme preference subscription

Date: 2026-10-19. No numeric algorithm, file format or permission change.

## Ownership and input/output

`config/ui_settings.json` remains the sole saved preference source and its existing
atomic writer remains unchanged. Every GUI process calling
`configure_theme_from_settings()` owns one `ThemeSettingsSync` under that process's
`ThemeService`. The launcher, independent project runtime and assistant process
already call this startup entry. Closing the launcher neither stops another
process's subscription nor transfers project/window ownership.

Only theme mode/package, explicit font family/point size and density affect the
subscription's application fingerprint. A changed fingerprint reuses
`configure_theme_from_settings(values)` and `apply_font_settings(values)`. The
existing service replaces snapshots and emits to all engine-owned
`ThemeEngineBridge` instances, updating already-open QML windows. Later engines
receive the current snapshot; a new process loads the same saved preferences.
Newly selected package IDs are reloaded from the existing local package directory
when not yet known by that process. Explicit user font overrides retain their existing precedence. DPI scale remains
restart-bound under the existing Qt startup contract.

## Notifications, errors and lifetime

Native file and directory notifications coalesce for 80 ms. The observer rearms
its file watch after atomic replacement and watches an existing ancestor when the
configuration directory does not yet exist. A 2-second fallback checks file status
only, covering missed/unsupported native events. Content is read only for a changed
file signature (mtime, ctime, size and file identity), with a 4 MiB bound.

Applying never writes settings or sends IPC, avoiding feedback loops. Unrelated
settings changes do not replace theme snapshots or reset fonts. Missing, invalid
or unreadable revisions preserve the last applied theme. Temporary read failures retry up to three times at 150 ms intervals without
blocking the GUI. A malformed JSON revision is attempted once per signature and logs a diagnostic without logging settings
content; `last_error`, `read_count` and `apply_count` expose local diagnostics.
A later valid save clears the error and resumes application. All callbacks run on
the GUI thread. Shutdown stops timers/watches; there are no late worker results,
new permissions or network operations.

## Verification

Tests use temporary preference paths only. Unit cases cover coalescing, atomic
replacement, unrelated changes, malformed/missing files, recovery, stopped owners
and stat-only fallback. Separate representative launcher, two project and
assistant processes use their own QApplication, ThemeService and QML engines;
saves must update existing bindings across all processes. After the launcher
exits, remaining processes continue updating and a newly started process loads the
same saved theme/font overrides. No real user preference or asset is read/written.
