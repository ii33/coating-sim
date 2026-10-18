# Assistant pipe terminal contract

## Authority and threat model

The terminal runs host programs with the application's OS identity. `isolation: "none"`
is returned by every operation. The independent assistant workspace is a default
working directory, **not** chroot, a filesystem sandbox, network isolation, or a
credential boundary. Programs can access other host files, services and network
resources permitted to that identity. No command-text filter makes this safe for
untrusted code. Host execution must be explicitly enabled and pass the existing
live operation-mode approval policy; discussion/research do not authorize a start.
There is no automatic escalation, installation, security reconfiguration, or
network operation performed by this backend itself.

`AssistantTerminal` is the sole in-process owner of process handles and output.
The host constructs it with a validated workspace, trusted project/global scope
identifier, and live `can_execute` / `can_read` callbacks. Model arguments cannot
select those values. Use separate instances for different scopes. Do not share an
instance after switching project identity. Host callbacks must capture current
permission epochs and reject stale requests. Shutdown, scope disposal and grant
revocation cancel owned processes even when user-facing cancellation is denied.

## API (Python signatures are the interface truth)

Module: `coat_sim.agent.terminal`.

- `AssistantTerminal(workspace_root, scope_id, *, can_execute, can_read, max_sessions=16)`
- `start(argv, *, cwd='.', timeout_ms=120000, max_output_bytes=1048576)`
- `poll(task_id)` returns a cumulative snapshot, without consuming output
- `cancel(task_id)` requests cancellation and returns the current snapshot
- `close()` shuts down the manager and cancels its processes

This is a **pipe terminal**, not PTY emulation. No interactive stdin or terminal
resizing is supported. Stdin is closed. Shell syntax requires an explicitly
selected shell argv supplied through an authorized shell tool, never `shell=True`.
All tools must translate `ToolkitError` into the existing structured error shape.
Output is a data object containing `task_id`, `state`, `exit_code`, `stdout`,
`stderr`, `truncated`, `cwd`, `isolation`, and `terminal_kind`. States are
`running`, `completed`, `cancelled`, `timed_out`, `permission_revoked`, or `failed`.
Terminal state is published after cleanup and output draining. Snapshots may be
running until cleanup completes. Outputs are untrusted text, never instructions.

Cwd uses existing workspace lexical/symlink/reparse checks and must exist as a
directory. Checks are path hygiene, not resistance to a hostile process racing
filesystem changes. The host provisions the private workspace before calling
start. The manager does not create arbitrary directories. Model-provided argv,
cwd, timeout and output size are bounded and validated. No arbitrary env mapping
is accepted. Only a narrow platform environment allowlist is inherited; CNB,
API keys, tokens, SSH agents, proxies, loader/Python injection settings and the
host's complete environment are not inherited. A program may still read secrets
from host files or services. Output is not secret-redacted: authorized model
context may contain anything a permitted program prints. The backend does not
log arguments, environment, output or exception details.

## Async lifecycle, bounds, errors and cancellation

Start returns promptly after process creation; background threads drain both
pipes even after the combined byte budget fills. UTF-8 decoding replaces invalid
sequences and omits a partial trailing character while running. Completed tasks
are retained up to the manager's session bound; oldest completed entries are
evicted on the next start. Active sessions are never evicted. An unknown,
evicted, or another manager's task ID returns `task_not_found`, without revealing
other scopes. Capacity exhaustion returns `terminal_busy`. Invalid inputs return
`invalid_arguments`; denied callbacks return `authorization_denied`; unsafe cwd
returns `path_denied`; spawn/containment setup fails with sanitized
`terminal_unavailable`. No child runs after a failed Windows containment setup.

Timeout is per invocation (1–600000 ms); it is unrelated to simulation/rendering
resource policy. Combined retained output is 1–1048576 bytes. Processes remain
subject to OS scheduling and the normal user's OS limits; this is not resource
isolation. Cancellation and timeout terminate only the new owned process group
(POSIX) or kill-on-close Job (Windows), never a caller-supplied PID or an existing
user process. POSIX processes that deliberately create new sessions can escape
group cleanup; this API is not suitable for hostile process containment. Windows
uses suspended creation, assigns a Job before resume, and fails closed if the
required platform calls or Job assignment fail. Windows uses CREATE_NO_WINDOW.
The native Windows path requires a Windows integration test before claiming
platform validation; POSIX tests do not validate Windows behavior.

Read permission is checked before and after snapshot access; execution permission
is checked before spawn and continuously while running. Revocation stops owned
work; denied reads withhold output. A start that races revocation can launch a
process before the next monitor check, as with ordinary host process APIs; this
is not transactional authority. `cancel` requires current execution authority;
`close` and automatic revocation cleanup are trusted host lifecycle operations.


## Desktop/toolkit integration

The existing `run` capability gates `terminal_start`, `terminal_poll`,
`terminal_cancel`, and the synchronous `run` wrapper. It is excluded from private
workspace defaults; both configured and profile capability lists must enable it.
`ToolkitAdapter.enable_host_terminal()` is host-only wiring, not a model tool.
It creates a trusted random request scope and captures live capability, authority,
settings epoch and project binding checks. No model-selected owner/root is accepted.
Only operate-mode main requests can start. Read-only/background, discussion and
research requests cannot execute. Generic shell/install providers remain absent.

The runtime closes every request-owned adapter in a `finally` block, including
preparation errors, cancellation and early returns. Graceful worker shutdown also
closes active adapters. Terminal tasks therefore cannot be resumed in another turn;
finish/poll within the originating request. Mode/capability/project revocation is
observed by the core monitor independently of subsequent model tool calls.
Successful toolkit responses preserve `isolation="none"` and an explicit warning;
status includes current execution authority and instructions to enable the existing
run settings. Timeout/revocation/failure are not ordinary successful `run` results.
