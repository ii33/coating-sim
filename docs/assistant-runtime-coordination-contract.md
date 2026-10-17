# Assistant runtime coordination contract

Date: 2026-10-12. Status: implementation baseline for the AssistantBridge run boundary.

## Owners and interface

`AssistantBridge` remains the owner of the QML facade, conversation/session selection, transcript projection, and per-session pending-message and steering state. `AssistantRunCoordinator`, a QObject child of the bridge, owns the worker process/tree, retained request identity, active/settled state, serialized command delivery, cancellation timeout, and process/request event gate. The bridge reads the worker and retained request through compatibility accessors; it does not keep second copies. The coordinator must not become a second owner of conversation state or copy session queues into a second mutable store.

`AssistantController` lives in `assistant_window_controller.py` and owns the two independent top-level windows for one engine, their shared QML context, geometry tracking, and open/close routing. Each controller constructs its own bridge as a child; the default bridge class is resolved lazily through the compatibility export in `assistant.py`. The optional bridge factory is an injection seam for tests and embedding code, not a shared controller or bridge singleton.

The bridge accepts messages through its existing `sendMessage`, `steerMessage`, and `sendDecisionSupplement` slots. A worker run command carries the coordinator's active `request_id`, session id, prompt, attachments, and configuration snapshot. The bridge's existing `_on_worker_event(envelope)` Qt slot remains the worker envelope entry point and delegates the envelope to the coordinator. The coordinator rejects a different process, stale request, and run/terminal events after settlement before they can update bridge state. Settlement retains the request ID for `debug_status()` and history, and permits matching late `usage` and main `context` updates plus auxiliary compaction context, including compaction begun while idle. Late primary `usage` and `context` are rejected once a new run starts or an explicit progress reset/stop/close clears the retained request ID. Auxiliary compaction context keeps its separate request identity across primary-run changes and remains valid until compaction completes or an explicit reset/stop/close clears it. An explicit active flag, rather than the retained ID, gates cancellation and run events. A terminal callback conditionally settles only the request it handled before notifying `busyChanged`, so a synchronous Qt slot that starts a new run cannot have that run inactivated by the old callback. A current-process `exited` event remains a process-lifecycle notification after run settlement so process-owned reservations can still be settled; it does not reopen the run or allow duplicate run-terminal events. Accepted payloads reach `_on_accepted_worker_event` for bridge projection. Public QML properties, signals, slots, and event payloads remain the interface truth.

## Queue and steering behavior

Submitting while idle starts one run. A normal submit while busy joins the bridge-owned pending queue, up to the existing `MAX_QUEUED_MESSAGES` bound shared with in-flight steering. Steering is sent to the current worker for the current request and remains associated with the originating session until the worker confirms consumption or the run ends. Stop-pending prevents new steering. Queue drain starts only after a completed run, and only when its session is still selected. Session switching and new-session operations remain blocked while a run is busy.

## Cancellation and terminal events

The first `cancelTask` while a run is active pauses queued work, marks stop-pending, writes one `cancel` command tagged with the active request id, and starts the existing 15-second acknowledgement timeout. A retained but settled request cannot be cancelled. Repeated cancellation while stop-pending must not send duplicate cancel commands or extend the timeout. A matching terminal event settles the run and stops the timeout while retaining its request ID. If no terminal event arrives, the timeout stops the worker and records an interrupted run, explicitly clearing the ID. Worker exit is terminal only for an active run; a later current-process exit still settles its process owner. Stale request events cannot settle or mutate a newer run.

## Close and disposal boundary

`shutdown()` marks the bridge closed before stopping timers, invalidating asynchronous storage/projection generations, stopping the worker, and closing render/storage resources. The bridge's `_closed` flag gates UI projection and storage callbacks; the coordinator's separate `_closed` flag gates worker transport and event acceptance. They have distinct owners and scopes. No worker event, storage callback, timer, or queued Qt callback may write transcript, projection, usage, or QML state after bridge closure. A bridge's session and pending state are independent of every other bridge instance.

## Errors and verification

Worker startup or command-delivery failure leaves no active run and exposes an actionable status; a failed start write is settled without erasing its debug identity. Worker process identity and request identity are both checked; worker exit and cancellation timeout converge on one terminal settlement. Runtime contract tests instantiate a real `AssistantBridge`, use a temporary `AssistantStore` location and fake worker pipes/processes, and stub settings/discovery so they never read credentials, contact providers, or use user storage. They cover duplicate submit/queue behavior, one-shot cancellation and timeout, stale and post-settlement run-event rejection, retained identity and late accounting, explicit reset/stop, reentrant terminal settlement, current-process exit handling, auxiliary compaction context delivery while active or idle, post-disposal write protection, and session independence.

Window and tray behavior remains separate from worker coordination. Desktop window activation is explicit; closing a window requests close directly so QML close veto and dirty-state confirmation can run without first activating the target. Tray menu commands use `DesktopAssistantBridge` methods, preserving queue access, notification sound toggle/volume preview, and assistant visibility controls. UI tests load the actual `ShellMenu.qml` with a fake desktop bridge and trigger the queue, sound, notification, visibility, volume, and preview paths.

## Console-free MCP subprocess streams (2026-10-17)

The worker's stderr is a rotating ProjectLog and intentionally exposes no raw
file descriptor. The built-in FastMCP StdioTransport therefore passes an opened
null-device path for subprocess stderr instead of inheriting sys.stderr. The
MCP module installs its own bounded mcp-ui stderr log before optional imports;
stdout and stdin retain their real pipes for JSON-RPC. Module, absolute-script
and frozen entry routes keep the same contract. No console or unbounded log
handle is needed. Regression coverage must perform the actual local handshake
with both a no-fileno parent stream and absent stderr, not just test GUI readiness.

## Lazy history document creation (2026-10-17)

The history delegate queues `documentForTurn` outside declarative property binding
evaluation. The renderer remains the document owner. A same-key update retains
the displayed document; row reuse, pooling, step output, and destruction invalidate
the pending ticket before any delegate access. A superseded result is not applied.
This avoids native Qt property-cache access violations observed when nonempty
history mixes lazy message bodies with completed model steps. Native regression
loads that history, renders real rich blocks, switches sessions, and reopens chat.

## Managed-window foreground handoff (2026-10-17)

On Windows the assistant grants foreground permission only to the selected,
registered process, synchronously during the menu gesture and before asynchronous
IPC. The cached client object and OS process creation identity must still match;
normal authenticated project/session/instance revalidation remains in the worker.
`AllowSetForegroundWindow` is a transient per-process handoff, not a global policy
or always-on-top change. The target restores and activates its existing window.
An admitted action is not proof of foreground ownership: native regression must
compare `GetForegroundWindow` with the exact target HWND after the real menu click.
