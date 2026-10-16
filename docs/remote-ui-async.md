# Remote desktop admission and cancellation threading

2026-10-13. This UI contract changes no physics, file format, or JobSpec semantics.

The Qt controller owns a run's immutable host, device, frozen input, and job ID.
The scheduler service owns admission and terminal status. Remote capability checks,
blob upload, `POST /jobs`, status reconciliation, observation, cancellation RPC,
and artifact downloads run outside the GUI thread. Local direct queue admission
retains its established synchronous behavior.

The GUI marks a run pending before starting its worker, so repeated clicks cannot
create a second ID. A remote worker submits that exact ID once, then observes it.
If acknowledgement is lost, the remote transport queries the same ID. The UI
worker makes a bounded reconnect check of that same ID and verifies the exact
external spec digest and device in status before observing it. An unresolved
admission remains visibly unknown with its retained ID; the UI never mints a
replacement ID automatically. A pending cancel is an
intent until admission is verified; after that, cancellation RPC runs on a
separate thread. Transient cancel failures permit a later repeat click against
the same ID. A cancel response is not a terminal cancelled result. Closing
the window detaches its observer without silently cancelling service-owned work.

Network failures surface safe error codes and, where admission is unknown, the
retained job ID. Credentials and endpoint URLs stay out of job input, status
messages, logs, and evidence files. A stale asynchronous progress/completion
callback may not overwrite a newer run's UI state.
