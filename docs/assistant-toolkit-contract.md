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

`register_fs_tools(registry)` registers read, write, edit, list_dir, read_many,
mkdir, delete, recycle_list and restore. All have capability `fs`;
write/edit/mkdir/delete/restore declare effect `write`, others `read`.

Local paths require explicit allowed roots; relative paths use the first root.
There is no implicit CWD/home access. Parent traversal, symlinks and Windows reparse
points are refused. Text writes do not implicitly create directories;
`mkdir(path, parents=false)` creates a directory explicitly, optionally creating
parents, with at most 32 path components below the permitted root. Existing
directories are accepted; files and linked/reparse directories are refused.
On POSIX, read and write
pin parent directories using no-follow directory descriptors and refuse special-file
reads. Other platforms check reparse points before operations but these portable
checks are not an OS security sandbox: hosts must prevent hostile concurrent directory
replacement, or provide an OS-isolated filesystem adapter. Directory enumeration
likewise assumes no hostile concurrent directory replacement.

Public mutations also use `policy.user_write_path` and the standard-library-only
`coat_sim.managed_paths` boundary. A `.coatproj` or `.cbbs` target, or any target
under an ancestor containing a project descriptor, must go through the typed
project service even if a user explicitly configured a broad toolkit root.
Descriptor presence is sufficient; malformed descriptor contents do not disable
the guard. Public atomic writes (including document/image outputs), mkdir,
delete and restore recheck this boundary before committing. Generic reads keep
their existing root permission; trusted `internal=True` toolkit state and recycle
writes retain their separate reserved-namespace path. This is not a replacement
for OS isolation against hostile concurrent native filesystem changes.

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
ownership/ACL/xattr and directory-fsync durability guarantees for ordinary text
writes are not provided. Generic filesystem mutations recheck the live host grant
at the commit boundary in addition to registry admission and cancellation checks.

### Reserved state and recoverable deletion

`.toolkit` directories and names beginning `.toolkit-` are reserved,
case-insensitively, anywhere below an allowed root. Generic filesystem reads,
writes, edits, directory creation and deletion cannot target them; listings hide
them. The shared `read_bytes` and `atomic_write` default checks also cover document
and image input/output, so those tools cannot bypass the reservation. Only trusted
implementation code may pass `internal=True`, for state stores and recovery;
no model-visible schema exposes this flag. This is an application policy boundary,
not protection from other host processes, manually created hardlink aliases or
an independently authorized execution provider.

`delete(path, expected_hash)` accepts one regular local file up to 32 MiB and
requires its exact lowercase SHA-256. It refuses directories, special files,
remote paths, traversal, linked/reparse paths and reserved state. Recovery
operations are scoped to the first permitted workspace root. There is no recursive
delete or permanent-purge tool. Larger files and directory cleanup require another
explicit host workflow; the generic text-read limit remains 1 MiB.

Before removing the source, deletion saves an independent byte copy and a bounded
JSON manifest under `.toolkit/recycle/<32-hex-id>/`, using exclusively allocated
entry directories. The manifest owns `version=1`, `recycle_id`, workspace-relative
`original_path`, `bytes`, `sha256`, permission `mode` (0777 bits only), and UTC
`created_at`. After saving, it rereads the source and compares its hash and file
identity, then rechecks live authorization immediately before unlinking. A stale
source or revoked grant leaves the source untouched; a prepared snapshot may
remain and can be restored separately. No failure cleanup purges a recovery copy.

Snapshot-to-snapshot identity checks retain exact device, full inode/file ID, size,
nanosecond modification time and change time. At the final path-stat versus
descriptor-stat comparison, Windows uses `st_birthtime_ns` instead of
`st_ctime_ns`: CPython can report creation time for path stat but change time for
fstat. Missing Windows birthtime fails closed. Device, full file ID, size and
nanosecond modification time still must match, and the source hash reread remains
mandatory. POSIX keeps its exact change-time comparison at every stage.

Payload and manifest files are flushed before source removal. On POSIX, their
entry directory and the recovery/workspace ancestor directories are also fsynced;
an actual directory-sync failure prevents source removal. Windows has no portable
stdlib directory-fsync guarantee, reports `directory_synced=false`, and promises
process-interruption recoverability, not arbitrary power-loss durability.
All platforms still require host isolation from hostile concurrent writers;
the final identity check and unlink are not an OS-level compare-and-swap.

`recycle_list(max_entries=200)` lists at most 1,000 snapshot manifests, including
invalid/incomplete entries as unavailable. Listing checks metadata, regular-file
type and size; full content SHA-256 verification happens on restore. Entries from
interrupted deletes may coexist with the original source. `truncated` explicitly
reports a bounded scan; no background scan or purge is scheduled.

`restore(recycle_id, path?)` defaults to the recorded original path, or accepts an
alternate path within the same workspace. Parents must already exist. It validates
the manifest and content hash, prepares a separate copy, and publishes without
overwriting any existing destination (including a destination created during
preparation). POSIX uses an exclusive hard link from a same-directory temporary;
Windows uses its no-replacement rename. Unsupported filesystem operations fail
safely with the snapshot retained. Restore never consumes the snapshot, so editing
a restored file cannot corrupt later recovery. Cancellation or permission changes
before publication leave the recovery copy intact. Ordinary file modes are
restored, but timestamps, ownership, ACLs and extended attributes are not.

`alias:/path` uses explicit remote routing. On Windows, native absolute drive paths
such as `C:/workspace/file` and `C:\workspace\file` take precedence over one-letter
aliases; native backslashes are accepted only on Windows. UNC/device paths,
drive-relative paths and traversal remain denied. Windows alternate data streams,
trailing-dot/space aliases and tilde-bearing components (including DOS short-name
aliases) are conservatively denied; use explicit long paths without `~`.
Unknown aliases fail `unknown_host`;
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
`tests/test_toolkit_recycle.py` additionally covers recovery round trips, retained
independent backups, collisions, malformed/tampered snapshots, bounded listings,
internal-state bypass attempts through other artifact tools, directory creation,
Windows lexical aliases, source changes, cancellation/live grant revocation, and
POSIX directory-sync ordering/failures before source removal. It also covers
edit/delete/restore, synthetic Windows cross-API timestamp differences, strict
same-API change-time checks, and replacement/same-size content conflicts. Windows
filesystem behavior still needs a native Windows execution check; Linux tests do
not establish native NTFS/reparse or power-loss behavior.
