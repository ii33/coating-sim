# Compute host and device selection contract

Date: 2026-10-17. This contract covers the settings page and the three desktop
calculation entry points: deposition simulation, film optimization, and appearance
rendering. It does not change physics, units, or frozen input formats.

## Ownership and state

The desktop selection model owns a draft `(host_id, device_id)` for each task.
Each run action has a compact `仿真设备` button. Its pop-up contains linked
host/device selectors and closes with Escape or an outside click; the run
action remains separate. Selections are saved per task under the project root.
The local scheduler or authenticated remote service owns admitted jobs, state,
progress, cancellation, and results. The application settings own only the
configured remote URL, TLS choice and optional CA path; an existing OS credential
store owns any bearer token. A URL, token, CA contents, or credential fingerprint
must never be copied into a frozen job, job metadata, result, or diagnostic log.

Changing host invalidates the device list. A device row is enabled only if the
selected host has a compatible protocol and executor schema, the task and
algorithm are supported, and the specific device is available. Submissions
capture the selected host and device before freezing inputs and use that same
route for submit, observe, cancel, and verified result download. They never
fall back to a different host or to CPU after an explicit GPU selection.

The local default is a CPU backend. CPU logical processor count is a host fact,
not a physical device ID or a request to reserve that many cores. GPU physical
IDs must be kept distinct from CPU worker counts. An installed library or a
marketing GPU name alone is not evidence that a backend can execute this task.

## Discovery and admission

Deposition scheduler discovery and optional GPU probing run away from the GUI
thread. The film optimizer exposes its known CPU-only worker immediately and
checks the queue at submission. Appearance runs in an isolated local process;
its NVIDIA CUDA option stays unavailable until the user requests the existing
isolated runtime probe from either the pop-up or appearance controls. The local scheduler must report
`protocol_version == 1` and advertise the requested executor. The desktop may
derive its CPU backend from the installed local worker contract; any optional
GPU row additionally requires a task-specific probe and a backend-to-device
binding the worker actually honors. The current `cuda:default` and
`directml:default` IDs denote a backend's default device, **not** a specific
physical GPU. A missing, incompatible, busy, or failed
probe leaves the row unavailable with a reason. Selection persistence is only a
preference; every submission rechecks current availability and compatibility.

Remote discovery is opt-in through an explicit configured URL and saved token.
No remote network connection occurs from empty settings or from opening the
settings page. Selecting the configured host initiates an asynchronous
authenticated `GET /api/v1/health` and `GET /api/v1/capabilities`; HTTPS verifies
the certificate and hostname. Plain HTTP requires the separate explicit TLS-off
choice and still requires a bearer token. The client rejects redirects. The
capability response must identify API version 1, executor schema version 1,
supported task kinds, and stable device IDs with backend, availability and
task/algorithm support. Legacy responses that only name executors are not
sufficient to populate selectable remote devices. No external server has been
validated against this extended device contract in the current source ZIP.

For a future compatible remote service, submission must be idempotent by a client-generated stable job ID and frozen input
digest. An HTTP timeout after submission yields an *unknown* admission state;
the desktop retains the ID and queries status before considering a retry. It
must never create another ID silently. For an admitted job, queued, running,
cancel-requested, succeeded, failed, and cancelled are distinct UI states;
progress and ETA are informational. A cancellation request is not a terminal
confirmation. Reconnection reads server state/events by ID, including reset
snapshots; observers do not own the job lifetime. Result artifacts are fetched
by ID and verified against length and SHA-256 before use. Stable service error
codes are retained for diagnosis without leaking credentials or server paths.

## Current compatibility boundary

`scheduler/remote_client.py` implements authenticated HTTP and verified artifact
downloads. This ZIP does not contain the separately described
`scheduler/remote_server.py` or `requirements-remote-service.txt`, so the
remote service cannot be started or integration-tested from this checkout.
The existing local daemon's capabilities payload reports executor names but
does not enumerate physical devices or their availability. Its durable queue
uses a job ID for idempotent submission and `cancel_requested` before a terminal
state. The remote service documentation describes an API v1 with executor
schema pairs but does not establish deployed-device discovery. Until an actual
service demonstrates the extended contract **and** its POST/jobs request and
receipt bind and attest a device ID, remote discovery ends in a visible
`remote_device_binding_unsupported` state and the run action is disabled.
No remote task has been integration-tested in this checkout. Local appearance
uses its separate QProcess and does not claim remote routing.

Changing this UI or routing contract is not a physics algorithm change.

## Integrated Intel GPU acceptance scope (2026-10-17)

The pop-up includes a task-specific, read-only support summary independent of
runtime availability. CPU is supported for the three desktop tasks. Intel
DirectML has passed a bounded flat-deposition numerical comparison; mask-edge
accuracy remains experimental. The current default/AMD-preferred DirectML
binding does not guarantee Intel selection on a multi-GPU host. A successful
initialization probe is not a complete-task acceptance result.

Intel XPU FP64/complex128 probes passed on the tested 8086:7D67 device, but real
film batch evaluation timed out at both 90 and 180 seconds. Desktop film
optimization remains CPU-only. Intel GPU appearance rendering is unavailable
because the product has no Intel backend; successful CPU appearance does not
establish GPU support. Exact environment, measured errors and limits are in
[Intel integrated GPU support](intel-integrated-gpu-support.md).

Optional local probe rows retain `available` as runtime availability and add
`runtime_probe`, `support_level` and `support_note` as advisory projections.
These do not change device IDs, frozen jobs, remote schemas or execution
semantics. Successful optional GPU rows carry an experimental label and keep
their limitations visible after selection. Unavailable rows retain their failure
reason; explicit GPU selections and CPU-only algorithm rejection are unchanged.
The model owns these projections, the QML pop-up only renders them, and existing
generation checks discard stale asynchronous discovery results.
