# Unified offline assistant library

Date: 2026-10-19. This is discovery and presentation integration only: no physical
algorithms, optical conversion, numerical sampling or material format changes.
Python signatures, MCP schemas and contract tests are the interface truth.

The existing library has one assistant discovery surface: `library_status()`,
`library_search(query, offset=0, limit=6, expected_version=None)` and
`library_read(path, offset=0, max_chars=6000)`. The existing research-library
tools use this same public surface on a fresh installation. There is no RII
database page, toolbar entry, external-library page or research-root input in
Settings. Existing material editing, import services and user research are
retained. Legacy private-root/sidecar configuration is preserved for compatibility
under its existing authorization and revocation boundary; no user files are moved,
deleted, indexed automatically or sent through the public-reference provider.

`core/unified_reference_library.py` owns only an ephemeral projection per call or
request. The packaged research manifest, `builtin_material_profiles()`, immutable
`RefractiveIndexLocalStore` and the fixed reference-source registry remain the
sole data owners. Only packaged material records are included here; custom
materials/overrides remain with the existing authorized material tools. Physical
storage is an implementation detail and requires no new user-selected directory.
No source is downloaded, installed, converted or imported by discovery.

Search admits 1–200 characters / at most 12 whitespace-separated terms, strict
integer offsets 0–1000000 and 1–10 results. Literal source-local matching is
deterministically interleaved between packaged materials, research text, installed
RII records (including n2), and source metadata. This is discovery order, not
semantic or scientific relevance. Existing research scan/time/byte limits remain
in force; errors and partial scans are returned in `failures`/`incomplete`.
`next_offset` advances over examined result slots even if a source read fails;
`total_matches` describes observed source counts, never missing or unscanned data.

`inventory_version` fingerprints the research manifest, packaged material data,
source registry and installed RII snapshot/state. Pass it as `expected_version`
for later search pages; a changed inventory fails with `library_version_changed`.
Rows carry exact `read_tool`/`read_arguments`, availability, original attribution,
license/rights, content hashes, declared optical units/ranges and source-specific
limitations. Text identifiers bind the manifest hash; material identifiers bind
the complete packaged record; registry identifiers bind its content version;
RII identifiers pin the immutable snapshot. A changed active RII pointer never
retargets an already returned read/conversion ID. RII detail verifies original
source hashes and retains block quantities, conditions, range and raw-text
truncation. `refractiveindex_convert` remains the sole existing candidate adapter:
it requires a chosen optical-set ID and explicit block selection when ambiguous,
preserves unknown k, and grants no material-write or simulation authority.

`library_read` accepts only returned identifiers or compatible manifest-relative
text IDs. It returns Unicode character pages of original text or a deterministic
JSON metadata/data projection, at most 6000 characters per page / 2 MiB per JSON
projection. Original full-resolution optical arrays are not sampled by this
service. Raw RII text is the existing bounded detail projection, with explicit
truncation; use the original preview/conversion tools for their defined purposes.
Follow the returned `next_offset` until null; source identity/version stays in
every page. Unknown, malformed, unsafe, corrupt or stale IDs fail explicitly.

Availability distinguishes `packaged_text`, `packaged_material_data`,
`packaged_data`, `installed_data`, and `reference_only_not_builtin`. The last describes a metadata
record, not the full linked source: access eligibility, an official download URL,
and a license never assert that a dataset or publication has been bundled. A
subset appearing elsewhere as a material record does not bundle the full source.
All reference text remains untrusted data, not instructions or scientific
validation. Missing selected RII storage reports `missing` without creating it;
damaged sources produce failures while independently available owners remain
searchable. No fallback changes a returned source identity.

Calls are synchronous bounded reads without jobs or persistence. Transport
cancellation may discard an already-started read but cannot trigger later writes
or downloads. The existing assistant live grant is checked before and after
local tools, including delegated use, and revoked results are withheld. MCP tools
are closed-envelope, explicitly read-only, and available in discussion mode.

## Verified inventory and limits

The 2026-10-19 working tree contains 62 manifest-listed text documents: 42 original
research chapters, 11 source/rights notices, 8 original guides and 1 inclusion
notice. Imported research is pinned to commit
`d1acac3766a3a1f0a14064edcbeadb7ea4244f4e`; the manifest keeps original rights and
does not relicense that research under the software MIT license. There are 30
packaged material profiles after the parallel Air classification update; optical-set totals and exact bands
remain with those source records.

The existing local RII snapshot is now copied byte-for-byte into
`coat_sim/core/resources/refractiveindex/` as a read-only package resource:
`c5c2f188e848453def5970e347399d653df2ffc2-98afca5804b7e692-v1`, with 3720 indexed
entries (3480 nk / 240 n2), from 4192 source files. The existing owner reports
CC0-1.0; upstream commit identity is operator-declared and not independently
verified, while local hashes are checked. Conversion classifications are 3353
supported, 51 requiring selection, 261 unsupported and 55 invalid. 4195 existing files (60,875,297 bytes) were copied and SHA-256 compared;
the separate inclusion notice adds 1077 bytes. Fresh installations use this
snapshot without any path setup. Explicit process configuration or an existing
user snapshot retains priority; neither is overwritten. This does not establish
that every source is numerically supported. A damaged selected source is an error,
not permission to change source. Explicit CLI build defaults to the original
writable per-user store, never the package. Returned packaged snapshot IDs remain
readable after a later user installation through the pinned snapshot resolver.

The source registry contains 17 metadata entries, including CIE, ITO source
datasets, vendor catalogs, thermochemistry, process guides and handbooks. This
integration does not download their full datasets, vendor manuals or protected
publications. Third-party full text, images/binaries, full later research rounds,
publication archives and uninstalled catalog sources remain outside the packaged
text corpus. Metadata-only entries explicitly remain not built in. No new rights
review or external scientific validation is claimed.

Targeted tests cover mixed-owner discovery and pagination, source integrity and
version changes, missing/damaged RII, metadata-only provenance, exact conversion
handoff, strict read-only MCP schemas, assistant defaults/live revocation and
actual QML absence of the removed page/entry/path field. Offline tests do not
establish live-model behavior or live external source availability.


## Existing CIE numeric resources

The unified surface also exposes the six already-shipped CIE datasets from
`data/film_design/cie-illuminants-20261006/data/cie_illuminants.json`: A, D65, D50,
V, XYZ1931 2-degree and E. The existing `MANIFEST_SHA256.json` verifies both the
numeric JSON and the attribution notice before discovery/read; its hash binds
the returned IDs. Full dataset objects retain original citations, license,
wavelength/value units, source domains, discrepancies, changes and conditions.
No interpolation, sampling, evaluation or other numerical changes are introduced.
E remains the project's generated constant table, never an invented official
download. These specific datasets and adaptations retain CC-BY-SA-4.0; complete
CIE/ISO standards are not bundled. Fixed paths are relative to the installed
project, not user-selected paths, and their content is never executed.

## Immutable upstream source snapshot

The packaged RII snapshot retains upstream explorer scripts as provenance data;
the runtime reads catalog/index/numeric records and never imports or invokes
these scripts. Removing them would change the manifest-bound snapshot identity.
Static analysis excludes only individually hash-verified files in this immutable
source subtree, with invalid or changed entries failing closed. Project provider,
store and packaging code remains in the static-analysis source set.
