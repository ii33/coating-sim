# Portable assistant toolkit v1

The contract is defined by `coat_sim.agent.toolkit.contracts`, `registry`, and
`policy`. These modules depend only on Python's standard library. They import no
Qt, PydanticAI, DSH, shell, network, or optional document/image libraries. Importing
this package performs no filesystem access, registration or installation.
Existing typed project/domain tools remain owned by their existing implementations;
the generic toolkit does not replace project parsing or domain validations.

## Adapter boundary and state ownership

- `ToolSpec(name, capability, description, parameters, effect='read', version='1')`
  is immutable, including nested parameter schemas. `to_dict()` exports plain JSON.
- `ToolContext(allowed_capabilities, allowed_roots, authorize, cancelled, providers)`
  snapshots capabilities, roots and provider membership. Provider objects retain
  their own lifecycle; contexts never own application settings or UI state.
- `ToolRegistry.register(spec, handler)` rejects collisions. A handler receives
  `(context, arguments)` and returns JSON-compatible data. `specs(context)` exposes
  only enabled capabilities. An empty capability set disables everything.
- `invoke(name, arguments, context)` validates before calling an authorization
  callback, calls that callback separately for every invocation (including reads),
  and executes only for the exact boolean `True`. No callback means deny.
  The callback receives recursively immutable arguments. Capability enablement
  alone never authorizes effects.
- Every result has `{ok: bool, data: JSON|null, error: {code,message}|null}`.
  Stable errors include `unknown_tool`, `capability_disabled`, `invalid_arguments`,
  `authorization_denied`, `cancelled`, `path_denied`, `conflict`, `limit_exceeded`,
  `unknown_host`, `lossy_edit`, `io_error`, and `provider_error`. Unexpected provider
  exception text is not returned (it may contain secrets).

Adapters may translate schemas and envelopes into their native SDK without importing
that SDK into the core. Backend adapters own async scheduling; synchronous core calls
should run in a worker when blocking the UI matters. Cancellation is cooperative,
checked before authorization/execution, between batch items, and before atomic
replacement. Cancellation after a committed mutation cannot undo it. No background
jobs are created by the core.

## Strict input/output boundary

Supported schema keywords are type, properties, required, enum, minimum, maximum,
minLength, maxLength, minItems, maxItems and items. Unknown object fields always fail;
booleans cannot substitute for integers. Strings, arrays, nesting and serialized JSON
have finite limits. Specs should use this supported subset, not silently depend on
additional JSON Schema semantics. Inputs and outputs reject NaN/infinity. JSON
request/result limit is 4 MiB; providers must enforce resource limits before producing
large objects rather than relying solely on final result checks.

## Filesystem capability

`register_fs_tools(registry)` registers read, write, edit, list_dir and read_many.
All have capability `fs`; write/edit declare effect `write`, others `read`.

Local paths require explicit allowed roots; relative paths use the first root.
There is no implicit CWD/home access. Parent traversal, symlinks and Windows reparse
points are refused. Directories are not implicitly created. On POSIX, read and write
pin parent directories using no-follow directory descriptors and refuse special-file
reads. Other platforms check reparse points before operations but these portable
checks are not an OS security sandbox: hosts must prevent hostile concurrent directory
replacement, or provide an OS-isolated filesystem adapter. Directory enumeration
likewise assumes no hostile concurrent directory replacement.

Text files are bounded at 1 MiB, read output at 10,000 lines, batches at 32 paths and
1 MiB total, directory listings at 1,000 entries. A file exceeding a requested byte
budget fails instead of decoding a partial character. Partial line reads return
`truncated`, `total_lines` and a SHA-256 hash for the whole file.

BOM detection supports UTF-8/UTF-16LE/UTF-16BE. No-BOM default is strict UTF-8.
GBK/GB18030/Big5/Latin-1 require explicit encoding, avoiding ambiguous destructive
charset guessing. Reads mark replacement decoding as `lossy`; writes and edits
refuse lossy source decoding. Overwrites preserve source encoding/BOM and uniform
EOL by default. Edit matches are exact; new replacement line breaks use uniform
source EOL. Mixed-EOL source text is otherwise preserved without normalization.

Existing-file write and every edit require `expected_hash`. New-file writes use an
empty expected hash internally. Hashes are checked again immediately before atomic
replacement. This detects ordinary stale editor/model views but is not an atomic
cross-process compare-and-swap: hosts sharing files with other writers must serialize
mutations for full transaction semantics. Temp files are created in the same directory,
flushed and replaced atomically, with cleanup on failure. File modes are preserved;
ownership/ACL/xattr and directory-fsync durability guarantees are not provided.

`alias:/path` uses explicit remote routing. On Windows, native absolute drive paths
such as `C:/workspace/file` and `C:\workspace\file` take precedence over one-letter
aliases; native backslashes are accepted only on Windows. UNC/device paths,
drive-relative paths and traversal remain denied. Unknown aliases fail `unknown_host`;
a configured verified host without its transport fails `provider_unavailable`.
Neither case creates a local alias-looking directory. The optional injected
`providers['remote_fs']` callable has signature `(operation, arguments, context)`;
arguments include lower-case `host` and absolute remote `path`. It must enforce its
own explicit host allowlist, confinement, authorization refinements, cancellation,
resource limits and preservation semantics. The core never opens a socket or loads
credentials. Provider absence does not trigger installation or local fallback.

## Document/image artifact budgets

Domain handlers use explicit bounded byte reads: documents up to 32 MiB,
images up to 8 MiB, split chunks/manifests up to 1 MiB and 1,000 parts.
`fs.atomic_write(..., max_bytes=...)` is a trusted internal override with a
hard 32 MiB ceiling; its default and all ordinary filesystem schemas remain
1 MiB. Existing-target hash rereads use the selected writer budget. Reads check
regular-file size before allocation and still use a bounded read to handle
concurrent growth. Archive and raster output buffers enforce their bounds while
encoding. See [capability limits](assistant-toolkit-capability-map.md#document-and-image-resource-limits)
for image dimensions, archive overhead, exact merge and partial-split behavior.

## Verification

`tests/test_agent_toolkit_core.py` covers immutable schemas, disabled capabilities,
authorization defaults, strict types/extras, stale hashes, path escape and symlink
refusal, unknown hosts, cancellation/budgets, and byte-preserving BOM/charset edits.
These are filesystem and serialization semantics, not new optical algorithms.
