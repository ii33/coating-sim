# Portable toolkit provider boundaries

Version 1, 2026-10-18. These modules do not implement a process launcher, socket
client, package bootstrapper, credential store or persistent remote access setup.
They have no backend or Qt imports. Importing/registering a tool is not readiness.
The existing application's sandbox and confirmation owner remains authoritative.
Registry invocation first validates arguments/capabilities and requires the
context authorizer to return exactly `True` for the original tool and arguments.
Handlers are implementation internals, not an alternate public authorization API.

## Execution and environment

`execution(request, context) -> mapping` is a trusted, explicitly injected provider.
It receives `argv` (never a joined command), local confined `cwd`, `shell=False`,
`timeout_ms` (1–600000), `max_output_bytes` (1–1048576), and UTF-8 `env` overrides.
Return `exit_code: int`, `stdout: str`, `stderr: str`, optional `truncated: bool`
and `timed_out: bool`. The host adapter **must** enforce sandboxing for process
filesystem/network/child-process access, approval refinements, terminate/reap on
timeout or cancellation, bound memory while draining output and suppress secrets.
Pre/post cancellation checks and response truncation cannot interrupt a blocking
provider. No supplied native subprocess fallback bypasses these requirements.
A `run` executable is not restricted to an interpreter; command-name and shell
checks are defensive hygiene, never a substitute for the process sandbox.

`execution_config` selects `python`, `bash`, and/or `pwsh` executable strings.
The host is responsible for resolving these against its actual sanctioned
environment. There is no PATH search or installation side effect here. `env_setup`
runs a probe using that selected Python and reports both selection and observed
`sys.executable`, version and pip availability. It does not write ENV.md.
`pip_install` is an explicit `effect=install` tool, runs selected Python `-m pip`,
accepts named requirements only, and verifies success with the same interpreter's
`-m pip show`. Missing pip fails; no bootstrap or image/SFTP automatic dependency
installation occurs. Optional index URLs require HTTPS and contain no embedded
credentials; no `--trusted-host`, TLS disabling or system proxy changes are added.
`pwsh` probes the selected executable for major version >=7 before each command;
`sh` selects bash without startup profiles. cmd and Windows PowerShell are denied.

Readiness requires callable execution provider; `sh`/`pwsh`/`env` additionally
require the selected interpreter setting. Configuration does not prove that an
executable exists: probe results remain the actual runtime evidence.

## Network

`network(request, context)` receives an HTTPS URL, HEAD method, explicit route,
timeout, `verify_tls=True`, `follow_redirects=False`, and a 4096-byte response
budget. It must enforce host egress policy, TLS validation, cancellation/timeouts,
redirect policy, output limits, and return `reachable: bool` with optional numeric
`status_code` and `latency_ms`. The toolkit discards other response fields.
`network_config.github_proxy_url` can select an explicit credential-free proxy
(e.g. `http://127.0.0.1:10808`) for GitHub and GitHubusercontent subdomains only.
CNB (`cnb.cool` and subdomains) stays direct. No proxy is guessed or enabled by
default. `net_check` reports results only; it never changes process environment,
OS networking, TLS, package sources, or future-call routing.

## Remote filesystem / SFTP

`remote_hosts` maps lowercase aliases to explicit host configurations containing
`hostname`, optional `port`/`username`, required `strict_host_key=True`, required
nonempty `known_hosts` external reference, and a nonempty list of absolute `roots`.
Optional `password_env` is a variable name and `key_ref` is an external key reference;
plaintext passwords/private key material and unknown configuration fields fail.
The host supplies/owns this configuration. No add/remove or credential generation
operation is provided; `sftp_hosts` only lists redacted aliases/readiness and pings.

All remote filesystem tools and `search_remote` use `checked_remote_call`, then
`remote_fs(operation, arguments, context)`. Arguments include lowercase `host`,
absolute normalized `path`, strict-host-key flag and bounded timeout/output values.
The provider reads the already configured host/credential references from context;
the toolkit does not resolve credentials or copy them into request/results.
Unknown aliases and remote-root escapes fail before provider invocation. There is
no local fallback. `ping` validates host configuration but needs no filesystem path.

The provider must verify host keys against `known_hosts` (unknown/mismatched keys
fail, never trust-on-first-use), resolve references without exposing secrets,
confine symlinks and server-side path aliases, honor per-operation encoding and
optimistic-concurrency contracts, and bound remote resources/time/output. It must
not return credentials or transport/configuration dumps. A synchronous callback
cannot be forcibly interrupted here: provider cancellation/timeout support is
required. `search_remote` is explicitly literal, with limits on files, bytes and
matches; unbounded remote regex execution is not exposed.

## Verification

`tests/test_agent_toolkit_execution.py` uses fake adapters exclusively. It covers
absence/default denial/cancellation, argv preservation, cwd confinement, byte
budgets, legacy shell rejection, pwsh probe failure, observed Python identity,
selected-interpreter pip install/verification, missing-pip refusal, TLS and proxy
routing, strict known-host settings, alias/root confinement and redacted listings.
No tests in this file execute a subprocess, network probe or package installation.

MCP-02 is independently covered by `tests/test_external_mcp_project_permission.py`:
project cwd permission is normalized with `is True` once before all admission
checks, including pool synchronization and explicit probe. Malformed truthy
permissions cannot initiate a connection. Positive strict-true normalization is
idempotent; these tests do not launch peers.
