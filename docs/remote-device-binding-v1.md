# Remote device-binding extension for Compute Service API v1

2026-10-13. Draft for isolated 007 source, after restoring the server that was absent
from the 006 ZIP. This document specifies the new seam before implementation;
server and desktop code/tests, rather than this prose, decide acceptance.

## Compatibility and ownership

The HTTP envelope and paths remain API v1. Legacy four-field `POST /api/v1/jobs`
remains accepted with its old behavior. The desktop host/device picker never uses
that legacy path. A host is selectable only after authenticated GET health and
GET capabilities return API v1, scheduler JobSpec protocol v1, executor schema v1,
`device_schema_version:1` and `device_binding_version:1`. Device discovery must
list stable IDs, backend, available/reason, per-executor and (where relevant)
algorithm support. Missing versions, unavailable devices, or unknown task kinds
fail closed. The single-worker server advertises its controlled `cpu:default`
backend; CPU logical core count is a host fact, not a physical device ID.
`cuda:default` is a logical default-device row, available only after a
server-side isolated allocation/execution probe and worker launch that pins and
attests the same backend. It does not imply a physical GPU UUID. No unsupported
GPU brand is inferred from installed Python libraries.

The client persists a per-task host/device draft, but snapshots the selected
route before submission. The service rechecks CUDA availability immediately
before admission because an earlier discovery result cannot guarantee launch.
The endpoint, token, CA contents, or credential fingerprint never enters a job,
artifact, exported evidence, or diagnostic log. Opening settings does not contact
the remote host. Selecting it explicitly may probe asynchronously. HTTPS verifies
host/certificate; HTTP requires the explicit TLS-off setting plus bearer token;
redirects are rejected.

## Device-bound admission

The new `POST /api/v1/jobs` form has **exactly** six fields:
`api_version:1`, `executor_schema_version:1`, `device_binding_version:1`,
`device_id`, `job_id`, and `spec_blob:{sha256,bytes}`. The canonical frozen
JobSpec stored in that blob includes `inputs.device_binding` with exact shape
`{schema_version:1,device_id}`. The service requires the envelope and spec IDs
to agree, validates the device against a fresh server-owned capability row,
requires that task/algorithm/backend are supported, and binds the admitted worker
to that device. The device-binding input enters the existing JobSpec fingerprint:
same job ID + same frozen inputs/device returns the existing job, while same ID
+ changed device or bytes returns `request_id_conflict`. Receipt and status
identify the requested device. A final record identifies an executed device
only when independently observed: the current typed film result lacks an actual
backend field, so its execution identity remains unknown rather than inferred
from its request. Appearance CUDA requires `gpu_cuda` with `mixed` or `float32`,
real CUDA variant/diagnostics and a matching bound worker. No CPU fallback
after explicit GPU selection. Errors have stable safe codes, without remote paths.

A lost POST acknowledgement is an unknown admission state, not permission to
mint a new job ID. The client retains the ID, host, device, and input digest,
queries status after reconnect, and does not blindly resubmit while admission
may still be in flight. Cancellation is a request until a terminal cancelled
record arrives. Progress/ETA are informational; event cursor reset requires
snapshot reconciliation. Result downloads verify metadata length and SHA-256
and publish atomically.

## Appearance portability

A desktop local-root-bound, potentially 60 MB appearance frozen envelope must
never be uploaded as a remote spec. The small external appearance JobSpec
contains an exact path-free public render request and a registered input asset
ID/digest. Its input `.coatasset` is uploaded with length/SHA checks and staged
under the service's own `data/` root. The service performs the existing isolated
freeze against that validated local copy, preserving the original physics and
input hashing. Only the resulting server-root-bound frozen envelope enters the
worker. The external spec stays under the existing 20 MiB wire limit; the
internal appearance frozen spec may use its already validated 60 MB limit.
Render outputs use fixed, individually length/SHA-verified artifact descriptors;
CUDA also downloads its input-job.json as a seventh artifact. Each artifact is
bounded to 512 MiB and the complete package to 1 GiB. The client validates
the completion manifest and atomically publishes its local cache before showing
a preview. Remote CUDA remains unavailable on a host until its own real probe
passes, and completion requires independent execution diagnostics.

## Acceptance gates

Run old API v1 loopback regressions unchanged. Add authenticated isolated
loopback tests for all three CPU tasks: capability/version/algorithm/device
negative cases; same-ID same/different input and different device; lost
acknowledgement and restart reconciliation; queued/running/cancelling/terminal
and event reset; cancel acknowledgement vs terminal; result/backend/device
attestation; tampered size/SHA and no publication; invalid/missing token and
TLS mode; no endpoint/token in frozen specs/artifacts/logs. CUDA can only be
marked available by separate real hardware execution evidence. The authorized
Windows 原生环境 acceptance uses a private temporary directory and localhost listener,
with no persistent user credential, firewall change, startup entry or push.
