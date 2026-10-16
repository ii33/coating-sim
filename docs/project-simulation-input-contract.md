# Managed simulation inputs and binary dependencies

Date: 2026-10-15. This is a persistence/transfer protocol, not a new numerical
model. Python signatures and contract tests are authoritative.

## Format and stable ownership

The descriptor remains strict `coat_sim.project` version 1 JSON, with the same
seven required manifest fields, optional project `summary`/`notes`, and five fields per document row. Named managed JSON paths follow [the core storage contract](project-core-contract.md); typed input references remain UUID handles. New document kinds are
explicitly recognized; an older reader that does not know them fails with an
unknown-kind error rather than silently omitting dependencies. No directory or
`project.json` opening alias is restored.

- `raytrace_request` (`.cray`): `coat_sim.project_raytrace_request`, version 1,
  `name`, `payload`, optional `description` and `notes`. The payload is the
  existing `coat_sim.geometric_raytrace_ui_document` version-1 input projection:
  `request` plus `deposition_asset` (`null` or `path`/`sha256`). A new workspace
  may have empty samples/surfaces and is explicitly non-executable until filled.
  Populated requests retain the existing geometric parser's validation.
- `appearance_configuration` (`.cappearance`): corresponding project envelope,
  same metadata fields. Payload: `schema=coat_sim.appearance_configuration`,
  `version=1`, `context`, `draft`, `geometry_thickness_mode`, `backend`,
  `precision`. A new unbound workspace has `context=null`, `draft={}`; no fake
  camera, geometry or executable status is manufactured. Bound workspaces retain
  the exact existing frozen context fields and full input draft. Context's
  persisted `asset_root` is `.`; runtime projection resolves the actual owner.
- `binary_asset` (`.casset`): small JSON envelope
  `kind=coat_sim.project_binary_asset`, `version=1`, `role`, `payload_path`,
  `size`, `sha256`. Roles are exactly `deposition_asset` (`.coatasset`) and
  `spd_csv` (`.csv`). The payload path must be
  `assets/<UUID generation>/<this document UUID>.<role suffix>`.

Input payload references use flat `<document UUID>.casset` filenames and the
ordinary manifest dependency graph. A generation path is a byte-storage
location, never the stable resource identity. Copying a project or publishing a
new generation rebinds JSON references by UUID. Cross-project transfer allocates
new UUIDs and copies actual bytes; it does not retain a source-project pathname.
Identical role/size/digest input bytes can reuse an already-owned target input.

No render result, worker state, job directory, progress, UI gesture state,
remote-host selection, credential, or result image is accepted in these input
projections. A deposition `.coatasset` explicitly referenced as an input can
contain the captured per-layer simulation fields required by ray/appearance
calculations. Those declared input bytes follow the dependency closure; this
does not enable indiscriminate packaging of output results.

## Service boundary

`ProjectResourceService.capture_simulation_asset(role, path, *,
expected_sha256=None, cancel_event=None) -> dict` is the explicit file-picker
import boundary. `role` is `deposition_asset` or `spd_csv`; the return contains
`document_id`, owned `path`, `sha256`, `size`, `role`, and `reused`. It validates
and imports one binary input transactionally without creating a configuration
or changing the default. Identical content, size and role reuse the existing
binary UUID and bytes without overwriting any existing metadata or advancing
the project revision. The returned owned path can be used by a draft and by
the existing recovery UUID mapping immediately; the original may then be
deleted before the configuration's first save.

Accepting the file picker means importing the input into the project. After
success it remains a reusable project resource even if configuration editing
is cancelled; the UI must report “input imported, configuration not saved”
rather than imply no import occurred. Cancelling the picker calls no import.
An optional `threading.Event` cancellation token is checked before final
publication: cancellation, close, validation failure or publication failure
cleans private staging and leaves the prior manifest/default. Cancellation
after successful publication does not undo the already imported resource.

`ProjectResourceService.save_simulation_resource(kind, payload, *,
document_id=None, expected_revision=None, name=None, asset_sources=None,
description=None, notes=None) -> Document` captures all supplied input files and
publishes binary metadata plus the simulation owner in one project transaction.
Omitted metadata inherits an existing document's values. Updates require an
explicit/loaded revision and preserve the saved document UUID. `asset_sources`
can explicitly supply paths by `deposition`, `area_spectrum`, or
`environment_spectrum`; it cannot add another role or unrelated files.

`Document.data` is the managed JSON envelope. `Document.runtime["payload"]` is a
detached native projection ready for the existing Qt controllers: actual owned
input paths and the current project root have been resolved. Scalar CSV samples
must exactly match the saved draft's `wavelength_nm`/`value` table. A stale digest,
missing input, mismatched role or table fails visibly. Nothing starts a GUI,
scheduler, renderer or physical calculation.

A successful save also returns `Document.runtime["revision"]`, the document
SHA-256 returned by that exact publication while its writer lock is still
held. The owner constructs the receipt and paths from that publication's
detached inputs, since forked editors need not share a service lock. The editor
uses this receipt rather than rereading a potentially newer row after saving.
The service's loaded revision advances to the same receipt.
Content deduplication verifies an existing owned payload before reusing it;
matching metadata alone cannot substitute corrupt bytes for the selected input.

`list_documents` for these kinds and `resource_information` read only the small
JSON envelope. Picker/metadata refresh does not hash/copy gigabytes of input.
Actual open validates owned bytes and their role-specific parser; a per-store
stat/digest-envelope cache avoids rehashing an unchanged input on every refresh.

## Binary safety and configurable I/O budgets

Every path component is checked for links/reparse points; only ordinary files
are read. Size, SHA-256, identity and before/after file signatures are checked.
Copies stream in 1-MiB chunks into an exclusive private file, then validate that
same copy. Deposition input uses the existing safe `.coatasset` reader, including
its nested entry/expanded-size/digest/numeric-array rules. CSV uses the shared
pure-core scalar parser: at least two finite, increasing wavelengths and finite
nonnegative values. This is the existing scalar-SPD interpretation, not a new
radiometric inference or calibration.

Role read maxima remain 512 MiB for `.coatasset` and 2,000,000 bytes for scalar
CSV. JSON remains 32 MiB/file and 256 MiB/project, with the existing 1000 managed
JSON-document ceiling. No new fixed 128-input or model-component count ceiling
is introduced. `AssetReadLimits(max_total_bytes=...)` explicitly configures the
binary input/spool safety-I/O budget (default 1 GiB); `ProjectStore.create/open`
and `read_project_package/import_project` accept it. Errors report actual and
configured byte counts. These limits protect reading/disk use, not physical
accuracy, masks, rays, sampling or calculation workload.

## Atomic publication, browsing, and temporary owners

Saving stages private inputs, rechecks the bound owner, project revision and
mutation gate, and publishes immutable owned payloads before atomically replacing
the descriptor. Existing destinations are never silently overwritten. Failure
cleans this transaction's new payloads and leaves the previous manifest/default.
Large file reads, streaming hashes and role-specific parsing occur outside the
session-host/resource lock. Same-filesystem private staging registers a verified
receipt (digest, size and file signature); final publication verifies that
receipt and consumes it using an exclusive no-replace rename. The final lock
still validates JSON graph/descriptor CAS and performs filesystem publication,
but does not rehash all old or newly staged binary payloads. Package/global
writers similarly stage their complete outputs before entering the owner scope
for final publication. The configurable budget accounts for both selected plan
spool bytes and new target staging bytes when both coexist.
Project target staging reserves its bytes under the same exchange-owner lock
before copying, releases that lock during I/O/publication, and returns the
reservation on every exit. Concurrent applies and previews include all active
staging reservations in their budget checks.
Unchanged owned inputs are checked by identity/size during unrelated JSON saves;
actual use and export still verify their digest. Historical generations remain
retained, as before.

A resource catalog owns JSON/metadata and exact file/member capabilities; it
does not copy every source input merely to list categories. A transfer preview
copies only the selected roots' binary closure into a plan-owned temporary
spool and validates it. The selected byte budget is checked before allocation;
all pending and published plan spools share an exchange-owner cumulative
reservation under its token lock. Concurrent previews cannot each allocate the
full budget independently. Idempotent cleanup returns the reservation on every
failure, cancellation, release, eviction, success and final owner close.
Source signatures are rechecked before commit; a changed descriptor, managed
JSON, asset or package produces `stale_source`.

Cancel/release/success cleans idle/completed plan spools. Owner close cleans idle
plans; an already reserved apply keeps its bytes until its completion/failure,
then releases them. A late read cannot publish a token into a closed owner.
An in-flight plan cannot be applied twice or silently canceled. The Qt facade
owns worker generations and close behavior; these core methods are synchronous.

## Exact packages and global exchange

`read_project_package` is a read-only package snapshot boundary. It creates no
project and extracts no directory. Expected members are exactly:

1. Canonical `project.coatproj`
2. Every manifest-designated managed JSON object
3. Exactly the binary payloads declared by validated `.casset` envelopes

JSON paths/digests, role suffixes, payload UUIDs, declared/actual sizes and hashes
must all agree. Encrypted, special/link, directory, duplicate/case-duplicate,
unsupported-compression, unsafe, unexpected, missing and oversized members are
rejected. No arbitrary file whitelist expansion exists. Binary members are
stream-hashed without extracting all of them. Their nested role-specific parser
runs on selected private bytes before any destination publication; whole-project
import validates every copied input. A fresh destination is published by the
existing exclusive rename only after complete validation.
The read/import/catalog boundaries report `invalid_package` for a directory or
other non-file archive source; linked/reparse-point paths retain `unsafe_path`.

Selective project/package transfer supports mixed root kinds and preserves the
target default and unrelated resources. Global exchange remains one root kind
per operation. Newly defined application library roots are `data/raytrace` and
`data/appearance`; files plus declared inputs publish in a new exclusive exchange
subdirectory. Global browsing stays within explicitly defined libraries and
does not search the user's disk. Film and expression-template SQLite libraries
remain distinct from these saved simulation inputs.

## Project draft ownership, close, and abnormal recovery

`ProjectComputeWorkspace.register_input_draft_provider(module_key, provider)`
registers one Qt-independent structural provider per `ray_trace`/`appearance`
module. The provider exposes `snapshot_input_draft`, `restore_input_draft`, and
`save_input_draft`, plus `input_draft_operation_id` (the last operation ID even
when idle); the compute workspace retains detached drafts when a view
unregisters. A provider must unregister before destruction. A dispatched write
remains registered until its matching request-ID completion, including when its
presentation has detached. A boolean acceptance result is never save completion.
Close preflight includes both optical drafts and refuses unfinished input
operations. Failed saves preserve the draft and the prior committed resource.

Compute version-1 checkpoints may additionally contain `optical_inputs`. This
optional strict list has module/kind, managed ID/revision, metadata, exact input
text, independent unconfirmed Information text, replacement token, and checksum.
Only existing owned binary references are serialized; external paths fail
explicitly and must first use the resource service's input capture operation.
No output, job, remote-host or credential fields are admitted. Old compute
checkpoints keep their existing interpretation.

Recovery validates the complete list before modifying any owner. Each restored
optical input becomes an unsaved copy with no managed write identity. Information
text remains in its independent edit buffer with a fresh identity token; cancel
discards only that buffer and leaves the recovered copy unsaved. Asynchronous
restore is pending until the exact request completes. Failure retains the
recovery candidate and the prior draft. A closed view leaves detached state,
never a callback into a destroyed Qt object.
The restore receipt also carries a transient operation token. Completion display
checks that token and the original review identity, so a later window restore or
newly selected recovery candidate cannot be mistaken for the earlier result.

Multi-module saves report each independently committed result. A later module's
failure does not undo another module's saved generation or claim that no write
occurred. The project stays open with failed/newer drafts dirty. Multi-module
recovery changes only in-memory drafts: if preparation fails after another
module adopted its copy, rollback is allowed only for that exact provider and
completed request, while its complete post-adoption snapshot is unchanged and
no newer operation has occurred. A later edit, Information buffer, owner change
or operation is retained and reported as partial recovery. Providers must call
`input_draft_restore_is_current(module_key, provider, request_id)` immediately
before adoption, so a late rollback cannot overwrite edits made after dispatch.
New provider registration waits for an active multi-module recovery to settle;
lazy window restoration cannot join another batch's rollback. No persistent
save is rolled back.

## Coverage and remaining platform verification

Tests cover empty real resources, metadata, owned byte copies, CSV table checks,
source independence, project/package/global selective round trips, unselected
asset exclusion, stream quotas, malformed envelopes and hostile package members,
source/target staleness, rollback, private spool cleanup, owner close during
reserved apply, and metadata/picker reads without input rehashing. Linux cloud
verification is not a substitute for native Windows/macOS filesystem/UI testing.

## Measured large-input check

A 2026-10-15 CPython 3.12/Linux check used a valid 38.08-MiB `.coatasset`
containing a 2300×2300 float64 random thickness field and a complete portable
input snapshot, without padding. Fixture creation is excluded from timings.
Save took 310.6 ms, `.cproject` inspection 60.9 ms, selected-closure preview
148.1 ms, and apply 143.1 ms. Maximum measured worker host scope was 1.20 ms
for save and 1.62 ms for apply. A same-owner `list_documents` heartbeat stayed
below 3.44 ms; this is a core concurrency probe, not native Qt frame timing.
Sampled processing RSS peaked at 84.33 MiB; process high-water RSS including
fixture creation was 141.04 MiB. No plan spool or project staging directory
remained. These are one-shot local observations, not storage/platform guarantees.
Deterministic slow-I/O barriers additionally verify list/close during save and
transfer staging, late-close rejection, source-change rejection, and no binary
hash/copy call from final publication.

A second check with the same 38.08-MiB numerical fixture exercised explicit
file-picker import, deleted the original, and then saved its first referring
configuration. Capture took 184.25 ms (maximum host scope 1.26 ms); first save
after deletion took 139.63 ms (host scope 1.37 ms). Selected package preview
took 160.88 ms and apply 140.67 ms; the same-owner list probe peaked at 3.53 ms
and processing RSS at 84.22 MiB. Spool/staging cleanup again left no leftovers.
