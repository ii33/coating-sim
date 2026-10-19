# Assistant knowledge discovery contract

Date: 2026-10-18. This is a read-only reference/access contract, not a change to
physical algorithms, numerical precision, file formats or computation authority.
The Python signatures and MCP schemas are the interface truth.

## Owners and authorization

- `core/algorithm_reference.py` owns an ephemeral, bounded text projection of
  the single shipped `docs/algorithms.html`. The shipped document owns its
  scientific claims. No caller-selected path, network, HTML execution or linked
  resource loading is supported. Links are citations only. Text and diagrams do
  not confer executable capability or certify a simulation result.
- `core/project_knowledge.py` owns only per-call discovery results. Existing
  `ProjectStore` / BBS revisions and `ProjectResearchStore` own all persisted
  data, attribution, hashes and licenses. No database, background indexing,
  persistence, save, upload or cross-project scan is introduced.
- MCP admits the explicit `.coatproj` plus persisted `project_id` through the
  existing `ProjectManagementService` authorized roots before discovery. There
  is no foreground/current-window fallback. Runtime routing can supply a
  verified project binding but cannot infer one from model text.
- The four new tools are read-only in discussion, operation and research modes.
  Their closed MCP argument envelopes reject extra keys and coercion. Read-only
  annotations are descriptive, not authorization. Existing external-provider,
  project-data transmission, write and live-mode approvals remain unchanged.
- All returned content remains untrusted reference data. BBS claims preserve
  authorship and revisions, research preserves statement/inference kind and
  source provenance; neither substitutes for actual verified calculation runs.

## Fixed algorithm reference

`algorithm_reference_catalog(offset=0, limit=40, expected_version=None)` lists
section IDs, titles and source anchors. `algorithm_reference_search(query,
offset=0, limit=10, expected_version=None)` matches all whitespace-separated
case-insensitive literal keywords in section ID, heading and text. It is not
semantic, cross-language or formula-equivalence search. Both use result-index
pagination and return `total`, `next_offset` and `has_more`.

`algorithm_reference_read(section_id, offset=0, max_chars=6000,
expected_version=None)` uses Unicode character offsets in the exact returned
plain-text section projection. Every response carries source, document title,
document version label/date when present, and `version` (SHA-256 of source HTML).
Pass that version on subsequent pages; a changed file fails with
`reference_version_changed` rather than silently mixing versions. Headings,
text, formulas and safe link targets are retained as text; script/style/template
content, event handlers and active links are never executed or fetched. SVG text
is included when present; visual layouts themselves require opening the document.

The source file is limited to 2 MiB, 1000 sections; catalog/search pages to 50
rows; read pages to 6000 characters; query to 200 characters / 12 terms. Unknown
IDs, invalid arguments, missing/unsafe/oversized/malformed resources are explicit
errors, never a successful empty catalog or a fallback to arbitrary file access.

## Project BBS + saved research discovery

`project_knowledge_search(root, project_id, query='', offset=0, limit=10,
include_archived=False, expected_version=None)` searches latest BBS revisions
(including replies and comments) and saved research evidence for exactly one
admitted project. Empty query browses. Nonempty query is a case-insensitive
literal substring, matching the existing BBS/research query semantics. Archived
BBS entries are excluded unless explicitly requested. Attachments, old BBS
revisions, unsaved editor content, research mirrors, external services and other
projects are outside this index.

The inventory deterministically interleaves sorted BBS and evidence IDs. `offset`
is an inventory scan position, not a relevance rank or count of matches.
`next_offset` resumes at the first unexamined entry; callers must continue even
when a page has zero matches and `has_more=true`. Result rows carry source type,
ID/title, bounded excerpt, attribution or source/license/hash metadata, and a
`read_tool` plus exact `read_arguments` for the existing full-read tools.
`citation` includes project identity and BBS revision or evidence content hash.

Each response carries `version`, project revision, source availability/counts,
per-page scan counts, failures, and an explicit budget. `expected_version` binds
pagination to the descriptor fingerprint and evidence inventory signatures; a
changed inventory fails with `knowledge_version_changed`. Discovery is a bounded
saved-state observation, not an atomic cross-store transaction. The descriptor
and inventory are rechecked before returning; later evidence reads retain their
own integrity checks. A caller seeking the full set continues pages under the
same version. No full-result total is claimed for a partial scan.

The page budget is 200 examined records, 16 MiB of record bytes, a cooperative
3-second scan deadline, 50 returned matches and 400 characters per excerpt.
Budgets bound discovery work, not upstream project-open/default-input validation.
Each source also retains its existing storage safety limits. A record larger
than the page byte budget is skipped with an explicit failure and advancing
cursor, preventing an unresumable page. Missing storage is distinguished from no
matches; corrupt/missing records are reported as partial coverage, never treated
as validated evidence. Unsafe paths and project identity/revision changes fail
closed. No response silently substitutes data from another project.

## Errors, cancellation and asynchronous boundaries

All operations are synchronous read-only calls with no independent job or
persistent operation state. Transport cancellation discards the response; a
bounded synchronous read already started may finish, but cannot publish files,
mutate a project, launch computation or continue with external calls. The scan
checks its deadline between records, not during an existing bounded storage
read. Failures carry stable domain codes through MCP `ToolError`. No automatic
retry changes source, project, scope or permissions. After a stale-version error,
restart at offset zero and reconcile the new version explicitly.

## Acceptance boundary

Offline contract tests cover strict schemas/read-only routing, fixed-path HTML
parsing, complete section paging/version detection, project authorization and
isolation, BBS replies, evidence provenance, unavailable sources, resource
budgets, stale inventory, read-only side effects and integrity failures. Passing
these tests does not establish live-model behavior, desktop end-to-end workflow,
external-provider availability, or research/scientific correctness.

## Material data readiness bridge

`material_data_readiness(identifier, wavelength_min_nm, wavelength_max_nm,
optical_set_id=None, assume_lossless=False)` reads the same effective
built-in/custom/override catalog as `read_material`, using the existing
`ApplicationMaterialCatalog` read-only projection so discovery does not create
writer locks. It accepts a catalog ID, never arbitrary model-supplied material
snapshots. The service catalog is not an implicitly selected project's frozen
material input. The response labels this scope and retains catalog provenance.
The pure readiness owner checks declared n/k coverage and reports known data,
assumptions and missing evidence separately. Missing k remains unknown; an
explicit lossless assumption does not rewrite data or authorize simulation,
material changes, source lookups or external transmission. Bad bands, unknown
material/dataset IDs and invalid source records fail explicitly.

## Unified offline references

The assistant discovers packaged research text/material data, packaged or
installed RII and source-registry metadata through `library_status/search/read`.
No database page or external-library setup is needed. Exact source rights,
versions, hashes, bands/units and material-candidate handoffs remain traceable;
reference-only sources are explicitly not built in as full text. Project
BBS/evidence and legacy private research retain their existing authorization and
storage owners. See [the unified contract](unified-offline-library-contract.md)
and [the provider contract](research-library-provider-contract.md).
