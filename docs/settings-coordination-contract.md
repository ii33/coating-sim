# Settings model-probe coordination contract

Date: 2026-10-19. Status: implemented and validated in the progressive
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

## API-key save address comparison (2026-10-19)

`SettingsWindow.qml` compares the editable address after the same whitespace and
trailing-slash normalization used by `SettingsBridge.setAssistantConfig`. A typed
key enables `assistantSaveKeyButton` only for the currently bound bridge address;
a different unsaved provider address still blocks saving. The bridge remains the
sole credential/address state owner, and `saveAssistantApiKey` remains the storage
boundary. Successful save clears the password buffer; failure keeps it for retry.
No new credential lookup, storage format, network request or real key is needed by
the regression test (`tests/test_settings_key_button_ui.py`).


## Offline model defaults and refresh snapshots (2026-10-19)

`provider_catalog.py` and its packaged JSON remain the single offline directory.
`model_configuration_for(api_url, model)` returns exact endpoint/model documented
hard caps, protocol, separately sourced capability facts and review date. Zero caps
mean unknown; capability `unknown` is never promoted to support by an ID being
listed. Sources attached to a provider ID may only establish its endpoint/catalog,
not every capability. Unchanged facts retain their original review dates.

`SettingsBridge.applyAssistantProviderPreset(str) -> bool` applies the selected
address and eligible default model to that window's settings draft. A same-endpoint
selection preserves a nonblank manual model. Each endpoint/model's existing manual
context overrides remain authoritative within documented hard caps. Cross-endpoint
selection picks that provider's default. Defaults are a **project convention**,
not an account-entitlement assertion. No arbitrary provider is chosen on first
launch; the complete directory works without a key, network or discovery cache.

The current transport remains Chat Completions. OpenAI defaults to `gpt-6-luna`;
only an automatic reasoning choice on a new endpoint changes to explicit `none`,
which supports function tools in this transport. Explicit manual reasoning is
retained. Astra/6.1 Sol tool requests still require Responses and retain the
existing visible protocol gate. Unsupported plan/protocol information rows remain
unselectable. Selecting a preset or catalog model synchronizes model details and
context controls immediately; settings reach the application's saved configuration
only through the existing Save action. Closing/cancelling discards the draft.
Credentials stay endpoint-bound in the existing OS store, with no new storage,
export, migration or logging path. A provider change clears only the unsaved
password field. Existing focus, scroll, lazy advanced-section and Theme sizing
rules remain in force.

`SettingsProbeCoordinator` owns each asynchronous run; `ModelDiscoveryBus` owns
credential-bound discovery snapshots. A refresh keeps the last usable snapshot
visible while gathering a separate replacement. A successful list replaces it;
failure/cancellation retains the previous rows, does not advance their successful
timestamp, sets the existing retry interval and shows an error/status message.
Already confirmed candidate rows may be merged on interruption. Endpoint/key
generation checks, cancellation, close and shared endpoint locking still gate
publication. A successful model list verifies listing only; it does not prove
inference, context capacity, tools or reasoning. Server metadata remains attached
to discovery records. `model_metadata_capacity` only reads documented numeric fields
from an exact provider's Models schema: currently DeepSeek `context_window` and
`max_output_tokens`. It ignores ID-only records, unsupported schemas, bool/string,
nonpositive and over-2,000,000 values. These explicit fields are server declarations,
not successful capacity stress tests or capability verification.

Server capacity stays in `ModelDiscoveryBus`'s existing credential-bound model
records; it is not written to `assistant_model_limits` or exported settings.
`reasoning_for` / `reasoning_for_fingerprint` attach the exact selected model's
documented fields to the existing probe packet as `capacity_metadata`. The packet
retains the model-list success timestamp; reads never renew the seven-day TTL.
Capacity-only packets carry `server_capacity_only`, and cannot certify reasoning
parameters or suppress known offline reasoning options. The current settings window
uses its bound credential fingerprint (or the current valid probe capture) to read
this packet. The existing assistant `_worker_config` passes the same probe packet
to runtime; runtime budget resolution uses that snapshot, not ordinary saved
capacity data. The cache reader preserves the `models_endpoint` evidence enum.

The existing successful key-save/delete actions invalidate this endpoint's cache,
including capacities, across settings windows. A future runtime snapshot rechecks
the actual bound key through the existing bus read, so external key replacement
cannot revive another key's cached capacities. Failed key-store operations leave
the current cache intact. Every manual probe adopts its explicit key-store read before model-list, candidate
or reasoning work, and advances the generation so a pending older bootstrap cannot
overwrite it. This is a storage snapshot, not proof of server authentication. A
key change invalidates the shared cache even if the changing window has not yet
opened discovery; it cancels another window's captured old-key probe so a late
reply cannot resurrect invalidated metadata. Assistant context display reads
`ModelDiscoveryBus.reasoning_snapshot_for` from the active in-memory entry only:
it performs neither secure-store reads nor cache-file reads during QML getters.
Only the latest credential identity checked/adopted by the bus may expose this
snapshot; installing disk data alone cannot validate it. A fresh request check
that finds a missing/unreadable or changed key also revokes the old display
identity. Unbound model-list fields are not a capacity fallback.
The existing model/reasoning signals notify context presentation on publication
or invalidation; `snapshotChanged` additionally notifies a fresh identity check.
The latter is emitted only after entry adoption (or identity revocation), so a
direct GUI callback sees the completed memory state. Until a snapshot exists,
offline defaults and explicit user
overrides apply. Worker submission independently calls `reasoning_for` to check
the current credential, then computes capacity from that same returned packet;
it cannot reuse a stale display value after an external key replacement/deletion.
No additional cache or credential owner is introduced. Existing endpoint/key
generation, cancellation and publication checks remain.

`capacity_profile_with_metadata` requires an exact endpoint/model/schema and a
finite, non-future timestamp within seven days; otherwise the independent offline
profile applies. Partial declarations fall back per field; no arithmetic invents
a missing hard limit. Explicit nonzero user budget fields remain in the ordinary
settings and remain subject to active hard-limit validation. Old experimental
`provider_metadata` fields in per-model settings are inert and removed by the
existing normalization/migration path. No key or fingerprint is added to ordinary
settings. Failure, cancellation and ID-only results preserve the usable cache and
offline presets. Explicit reasoning testing retains its existing separate entry.

Automatic discovery only issues GET /models. Manual refresh may use the existing
bounded candidate completion checks when the list endpoint is unsupported; the UI
states their cost and exposes cancellation. Tests use synthetic keys, fake network
responses and temporary files, including real QML preset/catalog/refresh entrypoints.
No real configuration, key store or paid generation is involved.

Current official source review: [DeepSeek exact model schema](https://api-docs.deepseek.com/api/list-models/),
[OpenAI Luna protocol](https://developers.openai.com/api/docs/models/gpt-6-luna),
[Gemini 3.7 retirement](https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash),
[Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai),
[GLM compatibility](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction),
[MiniMax international compatibility](https://platform.minimax.io/docs/api-reference/text-openai-api),
[Mistral Medium](https://docs.mistral.ai/models/mistral-medium-3-5-26-04),
[Grok 4.7](https://docs.x.ai/developers/models/grok-4.7).
Region/plan/aggregator evidence is never transferred to another endpoint. Kimi's
current page could not be independently fetched in this review; no new Kimi
capacity or capability claim is introduced. The complete per-entry provenance is
in `provider_catalog_sources.json`.
