# Settings model-probe coordination contract

Date: 2026-10-12. Status: implemented and validated in the progressive
refactor branch.

## Boundary

`SettingsBridge` remains the QML facade and settings draft owner. It keeps the
QML properties and slots, settings load/save/reset behavior, theme integration,
credential lifecycle entry points, and MCP lifecycle. A composed
`SettingsProbeCoordinator` owned by the bridge coordinates one window's model
and reasoning probe. The coordinator is a QObject child of that bridge. The
bridge delegates the probe-facing QML values and commands to it without keeping
a second copy of probe run state.

`ModelDiscoveryBus` remains the only application-wide owner of model and
reasoning cache data, endpoint claims, and `QLockFile` locks. A coordinator
obtains the existing shared bus through the bridge; it must not create a bus,
cache, or endpoint claim of its own. Credential snapshots and their generation
remain owned by the bridge because settings bootstrap and saved-key UI state
are bridge responsibilities.

## Inputs and outputs

The probe accepts a requested kind (`models` or manually requested
`reasoning`), the bridge's current endpoint/model draft, and a credential
snapshot approved for that endpoint. Manual model discovery reads the saved
endpoint-bound key through the existing credential helper. Automatic discovery
uses only the bridge's current, generation-checked credential snapshot.

The coordinator emits/serves the current busy flag, message, kind, result rows,
usage summary, and discovered model rows consumed by the existing QML
properties. The read-only `assistantNativeReasoningOptions` QStringList also
combines native and `thinking:` options from the shared bus for the current
endpoint/model and, while assistant settings are requested, the current
credential fingerprint. It publishes confirmed or incomplete capability
results through the shared `ModelDiscoveryBus`. It does not persist settings
drafts.

## State ownership

The coordinator is the sole owner of one active probe run: request identity,
cancel event, task queue, result and usage accumulation, candidate/reasoning
progress, reply identity, request and overall timers, QNetworkAccessManager,
budget futures/polling and budget reservation bookkeeping. Its QNAM and QTimers
are QObject children, and all reply handling and delivery to the bridge happen
on the GUI thread.

The bridge is the sole owner of settings drafts, key status and credential
generation. `ModelDiscoveryBus` is the sole owner of shared cache and endpoint
claim state. Values may be passed across these boundaries, but run state is
never mirrored in two owners.

## Errors and limits

Invalid endpoint, missing/unreadable remote credentials, a denied shared
endpoint claim, budget-store failure, timeout, authentication/rate-limit
failure, and remote protocol failure preserve the current user-entered model
and return a bounded status message. Credential or provider exception details
and secret values must not enter QML status text or logs. Automatic discovery
may only issue the existing model-list GET. Manual candidate behavior and
request/output limits remain governed by the existing model-probe helpers.

Reservation bookkeeping is one-shot: an unsent reservation is abandoned
without charge; a reservation attached to a sent request follows the existing
reported-usage or estimated-settlement rule. A stale callback cannot start a
request, publish into a newer run, or settle a reservation twice.

## Async and cancellation boundary

Credential reads and budget storage continue on the existing background
executors. Their completions are polled and handled on the GUI thread. A
credential result is accepted only for the bridge's current generation and
endpoint. A budget result is accepted only for its captured probe request ID
and a live, uncancelled run. A network result is accepted only when its reply
object is still the coordinator's active reply.

Repeated start requests while a probe or budget operation is active do not
create a second run. Cancel, endpoint/model invalidation, window close, and
QObject destruction stop timers, mark cancellation, discard late results,
abort an active reply, release the shared endpoint claim, and settle any
reservation exactly once. Destruction cleanup must not emit Qt signals or read
Qt properties from an object being destroyed. Closing the settings window also
invalidates pending credential UI delivery; reopening starts a fresh bootstrap
generation.

## Settings draft behavior

Settings edits remain in `SettingsBridge._settings` until a successful save.
A save validation or I/O error returns false and leaves the current draft
available for correction. Restore defaults replaces the in-memory draft and
does not write the file until save succeeds. Cancelling a model probe cancels
that operation and does not change the settings draft. The extraction must
preserve these behaviors and the existing QML properties, slots, signals, and
status wording.

## Compatibility and verification

Existing direct callers and tests use private probe methods on
`SettingsBridge`. During extraction, retain narrow forwarding methods where
needed or migrate a specific test to the real bridge/coordinator contract with
equivalent assertions. Do not delete behavioral assertions or skip tests.

The characterization tests live in `tests/test_settings_coordinator_contract.py`.
The required pre-extraction comparison is the settings credential, model
features, automatic probe, assistant typed-operation, and coordinator contract
modules. All test doubles use fictional credentials, fake network replies, and
temporary settings/cache paths; tests must never contact a provider, key store,
GPU, or paid endpoint.

## Font family draft and theme precedence (2026-10-14)

The persisted settings field `font_family` is an optional trimmed family name
(up to 128 characters). Old settings without it and an explicitly blank value
inherit the selected theme package's `fontFamily` override, or the bundled
Noto Sans CJK SC default for the built-in theme. A nonblank settings value takes
precedence over a package override. The existing nine-point default and saved
custom point size remain unchanged.

`SettingsBridge.fontFamily` / `setFontFamily(str)` own the settings draft. The
appearance field follows the existing save/cancel/reset lifecycle; a successful
save configures `ThemeService` and applies the resulting family to the Qt
application font. `ThemeService.configure(font_family=...)` atomically updates
its snapshot and engine notifications. Clearing the value restores inheritance;
it does not write Noto over an explicitly configured theme package family.

The resolved snapshot and application QFont both use the same family. Qt's
normal platform font substitution applies when a requested family is absent;
this setting does not download or install fonts. Focused compatibility and
precedence coverage is in `tests/test_font_size_defaults.py`. The real QML
focused-field save, disk persistence, bridge reload, and compact-window coverage
is in `tests/test_machine_settings_compact_chrome_ui.py`; the family field uses
an independent full-width row, without a width-dependent Grid column span.
