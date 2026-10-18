# Independent project runtime and recent launcher

Date: 2026-10-16. This is the dev.09 ownership boundary; no project format,
physics algorithm or automatic migration of already-running dev.08 owners changes.

## State owners and exact interfaces

- `ProjectSessionHost` remains Qt-free and reusable by CLI, MCP and services.
  Existing film/resource/compute workspaces, close coordinator and owner leases
  remain authoritative. No launcher active-project singleton is introduced.
- `ProjectRuntimeOwner` (the former in-process launcher adapter) owns the host,
  lifetime lease, staged admission and existing desktop adapters, in a separate
  process for each project. `ProjectRuntime` composes that owner with the reused
  desktop window router, authenticated application client and exit coordinator.
- The ordinary `ProjectLauncherAdapter` owns only picker/error/busy state and
  global recent history. It never acquires a project lease or retains a runtime
  process handle for close/kill. It submits create/open through
  `open_project_runtime(descriptor)`. New submits a create root/name and optional summary/notes spec to
  `create_project_runtime`; the independent runtime owns the actual atomic
  creation before following the same store/lease/default admission path.
- `RecentProjects.entries/record/remove/refresh` stores only canonical exact
  `.coatproj` descriptor, name and successful-open timestamp in per-user settings.
  Availability and bounded descriptor-only name/summary/notes projections are computed on workers. QML reads cached snapshots only; all
  refresh/remove I/O runs on a bounded, coalesced daemon queue, with fresh
  QSettings objects and stale-result guards. Removal changes current history
  only; a later successful open, including an in-flight one, may re-add it. Failed opens
  do not promote history. Arbitrary descriptor basenames remain valid; opening
  directories or legacy names remains unsupported.

## Process, startup and failure contract

Creation is never performed on a launcher daemon thread. A successful atomic
creation is retained if subsequent window startup fails. Runtime processes are detached, with DEVNULL stdin and a file log, never a
parent stdout pipe. Launcher close may exit its own presentation process without
changing any runtime. Startup uses the existing authenticated one-shot ready
channel and then authenticated exact owner discovery/activation; spawning alone
is not success. On Windows, the Popen handle can belong to the venv redirector,
while the real Python runtime has a different PID. The ready channel verifies the
OS named-pipe sender against the endpoint PID and a bounded parent chain rooted
in that retained process handle. Every ancestry edge checks process creation/exit
times; a declared desktop-host handoff also pins the verified host handle. Stale,
reused, unrelated or uninspectable identities fail closed. Windows senders wait
up to five seconds for the verification acknowledgement before exiting, so a
bootstrap cannot exit before its handoff identity is pinned. The channel never
signals a process or changes project ownership. POSIX keeps the exact-PID rule.
The runtime reports readiness only after its real project
admission/activation and updated shared registration settle. Initial views are
hidden and noninteractive before this point, and IPC mutation is fenced. A failed startup reports an error and exits itself.
Successful opens/activations persist recent history on the runtime, independent
of launcher polling. Older owners use best-effort client compatibility recording.
History errors are warnings and cannot turn actual open success into failure.
No history result participates in authenticated activation/readiness. Auxiliary
history drain after successful project close is bounded to six seconds, separate
from mandatory project transaction/lease settlement. A startup timer bounds a newly spawned runtime that cannot register/admit.

Concurrent openers reuse the existing OS owner lease and six-field activation
identity. The unsuccessful contender never steals, deletes or replaces a live
lease: it rediscovers and activates the confirmed existing owner. If that owner
has not become discoverable yet it reports a retryable error. PID creation
identity, nonce, client/session generations, canonical root and exact descriptor
remain required; activation routes only to `film_design`. An older assistant
without activation support is explicitly rejected, never replaced automatically.
Descriptor/selected-dependency validation failures keep their underlying error
code, message and explicit cause chain in the IPC-visible diagnostic. The outer
`project_identity_mismatch` rejection and every identity/ownership check remain
unchanged. A failed integrity, schema or filesystem check never falls back to a
different project or silently becomes successful activation.

## Close, cancellation and bounded lifetime

Native Close of the last project module uses `ProjectCloseCoordinator`, including
fresh provider preflight, local-job blockers and Return to save / Discard / Cancel.
Hide/minimize retain ownership. Runtime-owned Quick confirmation windows work
without a launcher. Multiple projects have isolated runtime presenters; the
existing coordinator's queued reviews are retained. Shared assistant menu Close
Project follows the same owner route. Cancel and Return to save retain drafts
and lease. The normal authorized close flow releases the lease. If writing the
clean journal fails, `ProjectOwnerLease.close()` attempts every owned OS handle
release and marks the lease closed. Only when all releases succeed does it raise
`JournalCloseError`: journal persistence is uncertain, ownership is released.
The runtime consumes that completed Future once and presents a dedicated warning
with an explicit “确认并退出” acknowledgement. Acknowledgement clears only the
in-memory warning; it performs no journal write, reacquisition or lock release.
It preserves any abnormal recovery evidence and cannot affect a new owner that
acquires the project meanwhile. The dialog's native close does not silently
acknowledge this warning. Repairing I/O alone does not rewrite an old generation.
Unknown/OS-release errors remain blocking errors, cannot use this acknowledgement,
and do not repeatedly emit the same timer error message. The runtime owner is the
sole warning-state owner; `releaseWarningPending` and
`acknowledgeReleaseWarning()` are the narrow Quick contract. Pending work still
blocks settlement; acknowledgement never cancels admission/package/recovery work.
The runtime exits after pending admission/package/recovery transactions and
lease releases settle successfully or released-ownership journal warnings have
been explicitly acknowledged. It neither cancels service-owned computation
nor terminates any other process.

The runtime's narrow review host is a transient `ApplicationWindow` using
shared `ThemeButton` / `ThemeLabel` controls, Theme font/palette/spacing tokens
and accessible button names. It is a confirmation/management dialog host, not
a project module; the existing dialog geometry exception applies, so it does
not create a persistent module geometry key or count as a project window.

Global AI assistant lifetime and shared window navigation remain independent.
CLI/MCP/AI headless project management does not require a launcher instance.
No promise is made to transfer dev.08 in-process drafts into dev.09 runtimes;
existing owners are activated through their supported authenticated endpoint.

## Verification

Focused tests cover separation, exact-path success-only recent history, detached
spawn/handshake failure, existing-owner activation, close review preservation and
exit after settled work. Isolated real-process tests use temporary assistant,
settings and project roots. Native Windows acceptance remains a separate gate.
The pre-existing `context_tokens` KeyError is outside this ownership change.

The Windows startup identity regression is in `tests/test_windows_startup_identity.py`:
portable adversarial identity/ancestry checks plus a native no-GUI venv redirector
probe. Win32 evidence comes from [GetNamedPipeClientProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getnamedpipeclientprocessid),
[PROCESSENTRY32W](https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/ns-tlhelp32-processentry32w)
and [GetProcessTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes).
