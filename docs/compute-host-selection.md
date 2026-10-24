# Compute host and device selection contract

Date: 2026-10-24. This contract covers the settings page and the three desktop
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
thread. The film optimizer exposes CPU immediately; its explicit bounded Intel
XPU probe and frozen UUID binding are specified in [the desktop XPU contract](film-xpu-desktop.md).
Batch submission requires capability film_xpu_execution=1 and rechecks the queue at submission. Appearance runs in an isolated local process;
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

Local deposition probe success requires both child exit code 0 and a valid
result marker with boolean availability. A success payload printed before a
nonzero exit, uncaught exception or timeout cannot make a device available;
incomplete or invalid JSON is also unavailable. Failed GPU rows keep their
requested backend/device identity and reason rather than becoming CPU rows.
The default 8-second and maximum 15-second probe limits are unchanged. These
protocol regressions use real bounded child processes, not GPU qualification.

The pending isolated appearance runtime and project preflight design is recorded
in [the deployment/preflight plan](appearance-runtime-preflight-plan-20261024.md).
It does not provision an environment or add an interpreter fallback.

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

Intel XPU FP64/complex128 forward evaluation and the narrow explicit desktop route
are available experimentally on sakuya 8086:7D67. CPU stays the default.
See [frozen device and lifecycle contract](film-xpu-desktop.md). Intel GPU appearance rendering is unavailable
because the product has no Intel backend; successful CPU appearance does not
establish GPU support. Exact environment, measured errors and limits are in
[Intel integrated GPU support](intel-integrated-gpu-support.md).

Optional local probe rows retain `available` as runtime availability and add
`runtime_probe`, `support_level` and `support_note` as advisory projections.
Deposition projections do not change its execution semantics. Film XPU adds
a versioned local frozen binding; it does not broaden remote GPU support. Successful optional GPU rows carry an experimental label and keep
their limitations visible after selection. Unavailable rows retain their failure
reason; explicit GPU selections and CPU-only algorithm rejection are unchanged.
The model owns these projections, the QML pop-up only renders them, and existing
generation checks discard stale asynchronous discovery results.


## 2026-10-18：本机与远程统一后台接纳边界

`ComputeTarget.client_for_submit` 只捕获路由和设备，不查询队列。调用方在 Qt
线程取得 `admission_verifier` 的验证及身份检查闭包，冻结输入和作业 ID，先登记
submitting/busy，再派发工作线程。本机 capabilities（包括启动后台服务和批量执行
协议检查）与 submit 均在此工作线程执行；普通膜系优化、角度网格准备后的优化及
沉积采用相同边界。发现缓存不替代接纳时验证。

验证前后、POST 前再次检查捕获的主机、设备、选择代数及项目操作身份；失效或
接纳前取消均不创建作业。POST 已开始后的取消按唯一作业 ID 在接纳成功后异步
发送，失败可按同一 ID 重试，不再次提交。窗口关闭保持响应，已接纳的服务作业
仍由队列拥有，观察者不能替代终态确认。`RemoteCancelEvent(pending_admission=True)`
为本机启用此延迟取消协议，未选择该参数的既有调用保持原行为。

定向验证：`tests/test_film_optimization_remote_async_qt.py`、
`tests/test_remote_ui_async_qt.py` 使用本机和远程假客户端的 capabilities/submit
门控、Qt 心跳及取消/失效断言；无需真实 GPU、服务网络或用户电脑。

本机 POST 回执丢失时，`submit_local_with_receipt` 只查询原 ID，并核对
`input_digest` 与冻结 `JobSpec` 的指纹；匹配后才接纳并释放取消意图，不重复 POST。
无法核实时保留 submission_unknown：沉积保留输入去重键，优化阻止同窗口再次启动，
提示用户先按原 ID 核对。取消重试耗尽通过独立 Qt 信号显示可重试警告，不依赖队列
有新进度；取消意图、取消请求送达和队列取消终态仍分开处理。
