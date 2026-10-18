# Local RefractiveIndex.info tool contract

Date: 2026-10-18. Python signatures and the published MCP schemas are the
interface truth. These adapters do not change optical formulas or numerical
semantics.

## Ownership and locations

`core/refractiveindex_local.py` owns snapshot construction, immutable versions,
classification, source integrity, parsing, conversion and preview. Its
`default_store_root()` selects the per-user persistent
`CoatSim/reference-databases/refractiveindex` directory, using Windows
`LOCALAPPDATA` or POSIX `XDG_DATA_HOME` and the owner's documented fallback.
The trusted process environment `COAT_SIM_REFRACTIVEINDEX_STORE` can override
that location. A model cannot change the location through MCP arguments.
No copy is created in every project and no raw upstream database is shipped in
the software repository.

The MCP adapter creates only a read-only Store for each call. An optional
`snapshot_id` selects one immutable installed version. Without it, the Store
pins the current version for that call. Reuse the returned snapshot ID for
subsequent pages, detail, preview and conversion so that a later activation
cannot silently change the source. Tool descriptions and reference contents
grant no permission to download, construct, import or modify materials.

## Read-only MCP operations

- `refractiveindex_status(snapshot_id=None)` reports actual installation state,
  fixed-version provenance, counts and conversion capability coverage. Missing
  storage remains a missing status and does not create directories, databases
  or locks.
- `refractiveindex_search(query='', catalog='nk', shelf=None, book=None,
  capability=None, cataloged=None, offset=0, limit=50, snapshot_id=None)` searches
  the local index. Catalog is `nk`, `n2` or `all`; capability is `supported`,
  `selection_required`, `unsupported` or `invalid`. Search pages contain at most
  100 entries, their total and continuation metadata. Entries are deduplicated
  by source path while retaining all official classification aliases. An empty
  query browses; search is literal local discovery, not online or semantic
  research.
- `refractiveindex_detail(entry_id, raw_limit=20000, snapshot_id=None)` returns
  metadata, references, conditions, block summaries, source hash and capability
  reasons. Raw source text is bounded to 0–100000 characters with explicit
  truncation; source content is inert reference data.
- `refractiveindex_preview(entry_id, block_indices=None, max_points=200,
  snapshot_id=None)` previews selected blocks with 2–1000 display points per
  block. The core identifies original counts, display sampling and model
  evaluation. Preview points are not an import dataset. Nonlinear n2 and
  k-only data retain their actual meanings.
- `refractiveindex_convert(entry_id, optical_set_id, name=None,
  block_selection=None, snapshot_id=None)` returns a validated candidate optical
  set and frozen source provenance. It does not write a material, select a
  project, choose a material role, replace a default group or run a simulation.
  Ambiguous multi-block records require an explicit selection. Unknown k stays
  unknown; unsupported or invalid records fail explicitly instead of being
  repaired, averaged, extrapolated or declared lossless.

The complete conversion response has a 1 MiB UTF-8 serialized-JSON limit before
the MCP transport envelope. Larger candidates fail explicitly with
`REFRACTIVEINDEX_RESULT_TOO_LARGE`; their installed source is unchanged and no
candidate is truncated or sampled. CLI conversion and an authorized GUI import
retain the full-resolution data.

All five tools have closed argument envelopes and read-only annotations.
Boolean values cannot be used as integer offsets, block indices or limits.
Block selections contain 1–64 distinct nonnegative integers. They are available
in discussion mode without adding a write-capable branch. Existing material
creation/update and project import services remain the only authorized owners
of material persistence; a candidate does not authorize those actions.

## Explicit command-line operations

`python -m coat_sim.refractiveindex_cli` provides `status`, `search`, `detail`,
`preview`, `convert` and `build`. Global `--store` selects a local store for the
human/process invoking the CLI; omission uses the same default as MCP/GUI.
`--snapshot-id` pins a read. The CLI returns one finite JSON result and exit code
0 on success or a JSON error and exit code 2 for domain/input failures.
An interrupted operation returns a cancellation error and exit code 130;
argparse usage errors retain the standard stderr help/error behavior.

`build --source-dir PATH --upstream-commit SHA` is an explicit write operation
using an already-downloaded source directory. `--source-url` accepts only the
exact official commit tree URL and records provenance; it does not fetch that
URL. `--no-activate` constructs an immutable version
without replacing the active pointer. No read command downloads, installs,
constructs or imports a snapshot, and no MCP build tool accepts arbitrary source
paths. This CLI is not the older `tools/import_optical_catalog.py`, which appends
prevalidated packages to the built-in material catalog.

## Additional source registry

`reference_sources_catalog(query='', category=None, offline_status=None,
offset=0, limit=20, expected_version=None)` and
`reference_sources_detail(source_id, expected_version=None)` read only the fixed
packaged scientific-source registry owned by `core/reference_sources.py`. The
catalog returns at most 50 records per page, total/continuation metadata and a
content-hash `version`; detail retains that version and the complete selected
source record. A changed expected version is an error. These tools are
read-only in all assistant modes and accept no arbitrary source path or URL.

The categories are optical constants, thermochemistry, process, colorimetry,
substrates, theoretical and handbook sources. Offline eligibility distinguishes
`open_license`, `official_download`, `reference_only` and `access_required`.
These labels do not assert installation, numerical support or permission to
redistribute every linked work. The registry keeps access, rights, evidence
links and unknowns separate. Its dates describe the source review; it does not
perform a live rights check, follow links, accept terms, download data, create
storage or transmit user information.

## Failures and cancellation

Core error codes are retained through MCP `ToolError` and CLI JSON errors.
Missing, malformed, unsafe, changed or unsupported sources are explicit states
or failures, never an empty success invented by the adapter. Read operations
are synchronous and have no independent background job. Transport cancellation
may discard a result from an already-running bounded read; it cannot create
data or trigger a later mutation. Explicit builds use the core's staging and
atomic activation contract; they do not change a previous active snapshot on
validation failure. There is no automatic retry against a different source.

## Verification boundary

Offline adapter tests cover discovery/effect annotations, strict envelopes,
snapshot and argument forwarding, configured-location isolation, read-only
filesystem behavior, CLI JSON/exit codes and explicit build separation. Core
tests own parser, index, numerical conversion and source-integrity coverage.
Passing these tests is not evidence of a live-model or full desktop workflow,
nor does a supported conversion certify suitability for a production film.
