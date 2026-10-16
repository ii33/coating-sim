# Remote compute service API v1

Date: 2026-10-10. This contract describes the separate, headless service entry
point and explicit client connection. The existing local scheduler remains
available. This is one trusted operator's queue; every authenticated client has
the same visibility and cancellation rights. Multi-user authorization is not
implemented. These are project conventions.

## Ownership and boundaries

`remote_server.py` owns HTTP admission and input blobs. `Scheduler` and its SQLite
store own all job state, event sequences, cancellation decisions, workers and
result capabilities. `remote_client.py` owns the explicit service URL and local
downloads. A client cache root is never a service root. The client does not read
server endpoint files, SQLite, history databases or worker paths, and never starts
a local daemon. Closing a client only detaches observation. Cancel is explicit;
the committed cancellation decision and terminal state come from the server.

No GUI or Qt import is needed to start the service. Installation must include
the Python package and computation dependencies; the data root can be empty and
outside the checkout. Workers use the installed package, frozen inputs and this
data root. The transport preserves executor algorithms, coordinates and units.
The related per-layer ray input repair preserves exterior NaNs and validates the
physical domain before interpolation; its numeric contract and conservative
boundary behavior are documented in `algorithms.html#raytrace-valid-domain`.
The source/Python installation is the supported service distribution; the
desktop frozen executable is not the remote service entry point.

The remote scheduler launches `remote_worker.py`. It uses the existing worker
protocol, leasing, cancellation and persistence, with a process-local film
executor adapter that supplies `FilmDesignService` instead of the deposition
`CoatService`. The existing worker entry has no injectable service factory; the
adapter changes this registered function only inside the dedicated remote
worker process. It does not change computation formulas or shared source files.

## Wire contract

All responses except binary artifacts are JSON: `{api_version:1, ok:true,
result:...}` or `{api_version:1, ok:false, error:{code,message}}`. Errors use
stable codes, do not include server paths or tracebacks, and use HTTP 400 for bad
input, 401 for authentication, 404 for missing data, 409 for idempotency conflict,
413 for size/quota, 503 for overload/stopping, and 500 for internal failure.

| Endpoint | Input / output |
|---|---|
| GET `/api/v1/health` | Instance ID, API version, readiness/recovery status |
| GET `/api/v1/capabilities` | Allowed executor/schema pairs and upload limits |
| PUT `/api/v1/blobs/{sha256}` | Raw bytes; verified SHA-256 and byte receipt |
| POST `/api/v1/jobs` | Exact fields `api_version`, `executor_schema_version`, `job_id`, `spec_blob:{sha256,bytes}`; admitted job record |
| POST `/api/v1/assets` | `{api_version:1,kind:"deposition_bundle",blob:{sha256,bytes}}`; verified portable input artifact descriptor |
| POST `/api/v1/rpc` | `{api_version:1,action,arguments}` for existing observation/result/cancel actions |
| GET `/api/v1/artifacts/{id}/metadata` | Descriptor with ID, role, safe name, bytes, SHA-256; no server path |
| GET `/api/v1/artifacts/{id}` | Binary bytes; downloader must verify descriptor length and digest before publication |

### Additive device-binding extension

Device binding is an additive API v1 extension. Existing four-field job
submissions retain their legacy behavior. A device-bound submission uses the
exact six-field envelope `api_version`, `executor_schema_version`,
`device_binding_version`, `device_id`, `job_id`, and `spec_blob`; the canonical
JobSpec carries `inputs.device_binding:{schema_version:1,device_id}`. Clients
must require protocol, API, executor-schema, device-schema, and device-binding
versions to match before selecting a host or device. Stable device IDs and
availability are server-owned capability data, and the request/spec device IDs
must agree.

The first phase is CPU-only: `cpu:default` is the stable CPU device ID. CUDA
availability and execution are unverified and must remain unavailable until a
server-side allocation/execution probe, pinned worker launch, and matching
backend attestation have been demonstrated on real hardware.

The spec blob is canonical UTF-8 JSON of the existing frozen JobSpec, containing
the complete captured design graph. It is a portable input package, not a path
to client files. `prepare_portable_spec` removes audit-only local provenance.
Admission validates versions, exact executor parameter/input shapes, finite JSON,
dependency hashes, logical paths and graph closure using existing validators.
Physical validation remains with the existing typed contracts and worker. Schema
version 1 does not permit arbitrary scripts, URLs, filesystem reads or a new
executor. Probe is available only under an explicit loopback test flag.
Deposition `.coatasset` inputs can be uploaded and registered; ray requests refer
to their registered artifact ID and digest. Registration validates the portable
archive before making it available to workers.

### Asset and field-domain versions

The current `.coatasset` writer produces package schema 2; its existing reader
accepts supported package schemas 1 and 2. Captured per-step grids use
`layer_field_manifest.schema_version=1`, with mandatory `float64`, `[ny,nx]`
shape, `nm`, substrate-local XY axes in `mm`, ordered growth-increment semantics
and exact step/array bindings. These fields were already produced and validated
before the domain-mask repair. Missing fields in a claimed captured manifest are
invalid input, not a legacy version to guess or upgrade.

The new `valid_mask` and `valid_cell_mask` are optional in-memory grid fields;
they are reconstructed from frozen substrate configuration and are not stored
as new NPZ members. No asset format or manifest version changes. Without a
saved outline, a complete finite rectangular total field remains supported;
NaNs without a trustworthy outline are rejected.

Valid older total-field-only assets remain readable for their existing purposes
and may be registered as bundles. They cannot supply deposition-driven coating
layers: the ray worker rejects them with the capability diagnostic
`deposition_asset_has_no_per_step_fields_recompute_or_use_uniform_film` rather
than splitting the total into invented layers. Such a worker input failure is
currently exposed over the queue as `algorithm_failed`; its precise diagnostic
is retained on the service. Recompute with captured per-step fields or use an
explicit uniform film. A newly generated captured asset was independently read
and its actual schema/manifest saved in the evidence directory.

The service retains immutable admitted specs in the scheduler database; retries
with the same job ID and same frozen spec return that job, while different input
returns `request_id_conflict`. Uploads are idempotent by content digest. A lost
upload or submission response can be retried with the same bytes/ID. Uploads and
downloads restart from byte zero; HTTP range/chunk session resume is not supported.
Event reconnect uses existing `events(after_sequence,wait_s)`; a restart or
expired cursor returns `reset:true` and a snapshot. A new client can reconnect by
explicit URL and job ID. Queued jobs survive service restart; running jobs become
interrupted (or cancelled if cancellation was already accepted), never replayed.
Failed/cancelled jobs may retain a separately verified best candidate.

HTTP request concurrency and read timeouts are bounded. Specs are capped at
20 MiB; blobs at 512 MiB; blob storage has a configured total quota. JSON control
requests are bounded. There is one compute worker. These transfer/concurrency
limits are not CPU, memory, execution-time or disk quotas for computation results.
Blob garbage collection, retention policy, process sandboxing for business
executors, reverse proxy integration and multi-tenant roles remain outside v1.

## Startup and deployment configuration

Use an installed environment with this package and its runtime dependencies:

On an approved service host, copy the source checkout (there is no wheel/package
installer in this repository), create a private Python environment, and install
`requirements-remote-service.txt` for the CPU service. Run from the checkout, or
explicitly put it on `PYTHONPATH`. The service data root must be a separate private
directory. Qt, MCP, the assistant runtime and desktop settings are not service
dependencies. Optional GPU execution needs its existing backend dependencies;
the accepted integration tests here use CPU. The reduced requirements list has
not been tested in a freshly provisioned machine.

```powershell
& .conda/python.exe -m coat_sim.scheduler.remote_server --data-root C:/service-data/coat --host 127.0.0.1 --port 8765 --insecure-loopback
```

This existing `--insecure-loopback` option is for literal IP loopback trials
only. Without an explicit plaintext option, listening requires a TLS
certificate/key and a bearer token supplied through an environment variable.
The client verifies the TLS hostname and CA. Redirects are rejected so
credentials cannot follow a different origin. Origin-bearing browser requests
are rejected. The token is never stored in endpoint/receipt documents. The
configured token grants access to the whole service namespace.

An operator may explicitly select authenticated plaintext HTTP with
`--allow-insecure-http`, or `"allow_insecure_http": true` in the JSON config.
The client must independently select `allow_insecure_http=True`; neither end
infers permission from an HTTP URL. This mode still requires a bearer token
of at least 16 ASCII characters with no whitespace. It sends the token and
job/input/result data without TLS encryption, and the server prints a warning
before listening without printing the token. Use it only on a network the
operator has designated as trusted, or through an encrypted tunnel.

The modes are explicit and mutually exclusive: the new server option cannot
be combined with `--insecure-loopback` or TLS certificate/key configuration.
The new client option cannot be combined with `allow_loopback_http`, an HTTPS
URL or `ca_file`. Invalid combinations fail before creating service data or
starting a listener. The existing loopback developer mode remains restricted
to literal loopback IP addresses; it does not authorize an unauthenticated
external service. HTTPS behavior and certificate verification are unchanged.

`config/remote-compute-service.example.json` defaults to TLS and contains
placeholders only. The separate
`config/remote-compute-service.insecure-http.example.json` shows the explicit
HTTP choice with mandatory token authentication and no certificate/key fields.
Pass `--config PATH`; CLI options may override its nonsecret values. Omitting
`--allow-insecure-http` preserves the configured boolean. Provision the host,
secret environment, service account and, for HTTPS, certificate through an
approved deployment process before external listening. No deployment,
firewall, ACL, system service or persistent credential is configured here.

```python
from coat_sim.scheduler.remote_client import make_remote_client
client = make_remote_client("https://compute.example.invalid:8765",
                            cache_root="C:/client-cache/coat", token=token,
                            ca_file="C:/approved/ca.pem")
client.ensure_started()  # health check only
job = client.submit(frozen_spec, job_id=stable_id)
client.wait_for_terminal(job["job_id"])
```

For the explicitly selected HTTP mode, the command and client are:

```powershell
# COAT_COMPUTE_TOKEN must already contain the operator-provided token.
& .conda/python.exe -m coat_sim.scheduler.remote_server --data-root C:/service-data/coat --host 0.0.0.0 --port 8765 --allow-insecure-http --token-env COAT_COMPUTE_TOKEN
```

```python
client = make_remote_client("http://compute.example.invalid:8765",
                            cache_root="C:/client-cache/coat", token=token,
                            allow_insecure_http=True)
client.ensure_started()
```

These external host examples are configuration instructions, not evidence of
a deployed listener. The implementation tests bind only loopback and mock
external listener creation.

The current GUI still creates its established local client. This service provides
an explicit Python connection seam; selecting it in Qt pages is a separate UI
integration and must not be inferred from a running service. Save/overwrite
confirmation and import of downloaded assets remain client responsibilities.

### Deployment scripts

`scripts/remote-compute-service.ps1` and `scripts/remote-compute-service.sh`
forward quoted arguments to the shared `scripts/remote_compute_service.py`
controller. Python must already be installed on the host. `install` creates
the private `.remote-service-venv` (or `--venv PATH`) and installs only
`requirements-remote-service.txt` there; it does not install globally or copy
a Conda environment between platforms. The checkout is the explicit working
directory. The service data root must be outside the checkout; a new empty
directory is supported. Tests use a synthetic checkout and separate peer roots.
The accepted source environment uses Python 3.12.14; fresh-machine installation
of the reduced requirements has not been accepted here.

The actions are `--help`, `install`, `validate`, `dry-run`, `start`, `health`
and `stop`. `validate` checks configuration without a listener; `dry-run`
prints a launch plan with the token environment variable name only. Options
may precede or follow the action. Configure certificate/key and health CA
paths yourself for TLS; the scripts do not generate certificates or trust CAs.
Read the token from an already provisioned environment variable before `start`
or `health`. Do not put its value in a command or a generated config file.

For the explicitly selected HTTP template on Windows:

```powershell
.\scripts\remote-compute-service.ps1 install
.\scripts\remote-compute-service.ps1 validate --config config/remote-compute-service.insecure-http.example.json --data-root "D:/Coat compute data"
.\scripts\remote-compute-service.ps1 start --config config/remote-compute-service.insecure-http.example.json --data-root "D:/Coat compute data" --allow-insecure-http
.\scripts\remote-compute-service.ps1 health --config config/remote-compute-service.insecure-http.example.json --data-root "D:/Coat compute data"
.\scripts\remote-compute-service.ps1 stop --config config/remote-compute-service.insecure-http.example.json --data-root "D:/Coat compute data"
```

The corresponding Linux commands are:

```bash
bash scripts/remote-compute-service.sh install
bash scripts/remote-compute-service.sh validate --config config/remote-compute-service.insecure-http.example.json --data-root '/srv/Coat compute data'
bash scripts/remote-compute-service.sh start --config config/remote-compute-service.insecure-http.example.json --data-root '/srv/Coat compute data' --allow-insecure-http
bash scripts/remote-compute-service.sh health --config config/remote-compute-service.insecure-http.example.json --data-root '/srv/Coat compute data'
bash scripts/remote-compute-service.sh stop --config config/remote-compute-service.insecure-http.example.json --data-root '/srv/Coat compute data'
```

These are operator instructions, not performed deployments. To use TLS, select
the TLS template with real approved paths, provide `--ca-file` when needed, and
omit the HTTP option. The independent Python client must still opt into HTTP
separately. HTTP with a CA option or TLS fields is rejected.

`start` manages a background child, reports success only after authenticated
API-v1 ready health with the matching PID/instance, and records its exact
command, executable, creation identity and absolute data root. A repeated
matching start returns the existing instance; a live unknown or mismatched
record is refused. `health` and `stop` use the same controller record and
service endpoint. Choose a different `--state-dir` for each managed instance;
the default record/log directory is `.remote-compute-service-state` in the
checkout. It stores no token. The foreground Python command shown above is
also available and uses Ctrl+C to shut down.
`install`, `start`, `stop` and `health` share an exclusive lock keyed by the
canonical absolute venv path, including Windows case normalization. The lock and
registry files are siblings of the venv, so taking the lock does not create it.
Different checkouts, data roots and state directories using that same venv share
the lock. Lifecycle actions then take the state directory's `operation.lock`,
always in that order. A changed or abandoned lock requires manual review and is
never removed by a later invocation.

The controller also owns a bounded, versioned registry of managed state-record
paths beside the venv. `start` syncs its reservation file before creating the child;
verified `stop` removes only that reservation. Installation refuses to run venv
creation or pip while any reservation remains, or when the registry is corrupt,
mismatched or incomplete. This check needs no operating-system process query.
A fresh nonexistent venv can initialize complete tracking. An existing venv
without a registry is refused for `install`; select a fresh `--venv` or review
the old environment manually. Starting a legacy venv can establish incomplete
tracking and a reservation, but cannot claim that other old instances are known.
New instance records include their canonical venv path; selecting a different
venv for those records is refused.

Failed startup cleans up only its own child and releases its reservation only
after that child is known to have stopped. Unknown ownership preserves the
reservation and any instance record for review. Multiple managed instances may
share one venv; stopping one cannot unblock installation while another remains.
These records cover controller-managed instances; do not launch a separate
foreground or manual process using a managed venv while installing into it.

The reservation, child creation and first instance-record write form one failure
boundary. If child creation fails before returning a child, the controller
releases its reservation. An error after creation must retain the exact child
object until cleanup finishes. If the first record write fails, cleanup uses
only that owned child or a verified creation identity. Cleanup requires the
child to confirm exit. The primary and recovery
records are removed before releasing the reservation; failed record removal
retains the reservation and recovery information. Failed or uncertain
cleanup keeps the reservation and saves a bounded recovery record beside the
venv registry, keyed by the original state path. It contains PID, command, root,
venv and any captured identity, with no environment-variable values. Lifecycle
actions can discover that record when the primary record is absent; identity
and endpoint verification still apply. A reservation without a readable record
blocks another start. If both primary and recovery storage fail, the controller
reports that identity persistence failed, including PID and recovery path for
manual review, and keeps installation blocked.

`stop` verifies the recorded command/data root, process creation identity and
endpoint PID/instance before signalling only that process. Windows requires
its CIM process identity query to succeed; unavailable or denied queries fail
closed. Windows uses a creation-time-checked process handle for abrupt
termination, so running jobs require the established interrupted-job recovery
on the next start. Linux requires pidfd support and sends SIGTERM through the
verified process handle, escalating to SIGKILL after the bounded wait. Neither
script stops by process name or manages an OS service, firewall, ACL or secret.
Unavailable, denied or malformed Linux identity data is an error rather than
evidence that a recorded process has stopped.
Local TLS health checks verify the configured listen IP; `0.0.0.0` is probed
as `127.0.0.1`. The supplied certificate must cover that health address. This
controller does not offer a separate health SNI/hostname override.

The current environment denied the Windows CIM identity query and the Bash
executable launch. A separate Windows PowerShell `-File` wrapper invocation
also failed with `AuthorizationManager` / `UnauthorizedAccess`; no execution
policy was changed or bypassed. Windows managed start/stop, this wrapper
invocation and Bash syntax execution have therefore not been accepted here;
no Linux deployment has been run. See the
evidence report for the completed parameter/mock checks and remaining bounds.

## Verification

Tests use synthetic inputs, loopback only, separate service/client roots, and
private temporary directories. `tests/test_remote_server.py` launches a real
server child and real workers, destroys the original captured design files, and
downloads an asset from service A before uploading it to independent service B.
The ray job must use the same deposition step and center thickness and agree
with the existing thin-film solver. The cancelled-candidate test calls the real
film worker executor/checkpoint writer and verifies replay and durable recovery;
it uses an in-process cancellation hook, not a timed network cancellation of a
running optimizer. Queued/running network cancellation is separately exercised
with the loopback probe executor.

Run the remote contract/client/server, HTTP mode and domain test modules with
pytest and `-p no:cacheprovider`, a unique
repository-local `TEMP`/`TMP`, and a unique `--basetemp`. Run
`test_geometric_raytrace.py`, `test_ray_trace_cli.py`, `test_ray_trace_job.py` and
`test_ray_trace_queue_dispatch.py` for the related core/entry contracts. No test
needs real settings, materials, external accounts or model network access.
See `remote-compute-service-evidence-20261010.md` for actual pass/failure results
and preserved raw logs; this document alone is not a test result.
