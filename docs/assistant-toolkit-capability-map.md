# Portable assistant toolkit: capability map and migration

This maps the user-supplied `@local/dsh-agentkit` 1.4.3 report to the Python
implementation. The report's 29/30 result belongs to its DSH installation; it is
not a test result for this application. No DSH bundle, Node runtime, npm install,
or separate preset MCP category is required here.

## Settings and authority

In Settings → assistant advanced settings → **助手独立工作空间**, the new section
starts collapsed and uses the existing theme/font controls. Its persisted keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `assistant_toolkit_enabled` | `true` for the private default | Master switch; explicit false stays off |
| `assistant_toolkit_workspace_root` | Per-user `CoatSim/assistant-workspace` | Private persistent directory; explicit custom roots remain supported |
| `assistant_toolkit_capabilities` | `fs, doc, split, img, tasks, preset, memory` | Local-only global capability ceiling |
| `assistant_toolkit_profile_capabilities` | Same local-only defaults | Selected profile capability ceiling |
| `assistant_toolkit_allow_model_context` | `true` for the private default | Own-workspace results may enter current model context; explicit false stays off |

Effective capabilities are the **intersection** of the two independent lists.
Either empty list disables everything. Unlike the source report's behavior,
an empty list never means “use all defaults.” Turning on the master switch does
not populate either list. Selecting a capability is not provider readiness and
is not permission to execute a particular consequential operation.

The new-user default is `$LOCALAPPDATA/CoatSim/assistant-workspace` on Windows,
and `$XDG_DATA_HOME/CoatSim/assistant-workspace` on other systems (fallback
`~/.local/share/CoatSim/assistant-workspace`). Relative environment paths are
ignored. Computing defaults does not create directories or read file contents.
This is a stable per-user location across projects, sessions and installations;
it contains neither the assistant credential store nor its runtime control files.
Only this exact default root can be created lazily, at the first authorized local
mutation. Before that, status reports `not_created`; discussion reads do not
create the directory. Existing user false values, empty capability lists and
explicit empty/invalid roots remain fail-closed. For a custom root, omitted
switch/consent/capability fields retain the old disabled defaults; changing a
path never grants new authority. No old workspace content is moved or copied.

Saving an enabled custom configuration requires a real non-volume-root directory,
outside the installation tree and outside its ancestors. The private default may
not exist yet. Lexical symlink/reparse-point ancestors are rejected before resolution. The runtime independently checks scope and rejects escaped paths.
Invalid enabled roots prevent saving; discarded drafts do not persist. Existing
assistant credentials stay in the existing secure credential store. Unknown
configuration fields, including token/password fields, are not serialized by
this settings schema. External credentials must come from host-owned providers,
not profile/persona JSON or command arguments.

Local mutations additionally require current desktop operation authority in
operate/research mode, including the live request epoch. Inside the private workspace,
ordinary saves and recoverable deletion do not ask a second per-call UI question.
Project/domain writes still use their existing separate tools and authorization.
Even an explicitly selected custom toolkit root cannot use generic file,
document or image mutations to change `.coatproj`/`.cbbs` files or descendants
of a directory containing a project descriptor. Such targets require their
typed project service. Ordinary standalone files and trusted toolkit internal
state keep their existing behavior.
Read-only/worker contexts cannot inherit main-thread
write authority. Tool results require the model-context consent above.
The policy remains enforced when profiles change or a model directly invokes a
tool; profile data cannot grant roots, providers, or authority. A monotonic worker
grant generation changes when workspace enablement, root, either capability list
or model-context permission changes. The token is stamped before scheduling each
request, so preparation waits cannot rebase an old grant. Any change revokes the
old request (including switching away and back); no mid-request permission
expansion occurs. Both reads and writes check this trusted live token; in-flight
read/image results and dynamic memory summaries are withheld after revocation.
Before the next model dispatch the token is checked again. Previously delivered
model context/conversation history cannot be retracted by disabling access.

The main assistant has one separate discussion-mode project exception:
`project_bbs_comment` may append a comment or reply under a live, matching
UI request generation. It remains an effectful tool, not part of `READ_TOOLS`.
The gateway requires an explicit host-only permission and checks the generation
again before dispatch. Analysts, delegated executors and background completion
requests do not receive it. Formal BBS create/update/archive, generic project
management, filesystem and long-term memory writes remain unavailable in
discussion. See [BBS contract](project-bbs-contract.md).

BBS comments may include plain/Markdown source and already-managed attachment
descriptors; accepting a descriptor never authorizes a file import. The separate
`project_bbs_import_attachment` mutation is available only to the foreground
main assistant in operation/research mode. Both its dedicated entry and the
`project_manage(operation="bbs_import_attachment")` alias require that host-only
grant; discussion, analysts, delegated executors and background requests cannot
import. The business service independently authorizes both the project and source
file under existing approved roots, and checks project revision/active owner.
An import returns a descriptor without automatically publishing a post.

## Original 13 report capabilities plus compact memory

“Implemented” below refers to bounded handlers and offline tests. It does not
claim live provider integration, external installation, unrestricted shell
access, or DSH end-to-end compatibility.

| Capability | Report tools | Implementation and readiness boundary |
| --- | --- | --- |
| `fs` | `read`, `write`, `edit`, `list_dir`, `read_many`, `mkdir`, `delete`, `recycle_list`, `restore` | Local workspace-confined text/byte handling, bounded output, encoding/BOM/EOL metadata, digest-based conflict checks and atomic single-file replacement. Remote aliases are handled through an explicit provider; unknown aliases must not silently fall back to unrestricted local paths. Writes need live authority. Directory creation is explicit; file deletion requires the current digest and keeps a local recovery snapshot, restore never overwrites. No recursive directory deletion or permanent purge. |
| `sftp` | `sftp_hosts`, `search_remote` | Typed remote provider contract; host listing and ping are supported, while add/remove remain host-owned configuration. Search is bounded literal search rather than an unbounded regex backend. No default SSH session, credential lookup, asyncssh installation, or network connection. Host must supply a verified remote provider and authorization. |
| `run` | `run`, `terminal_start`, `terminal_poll`, `terminal_cancel` | Explicitly enabled operate-only host pipe terminal; bounded timeout/output and request-scoped cancellation. `isolation=none`: cwd is not chroot. Default off. |
| `sh` | `sh` | Explicit shell operation through the execution provider with verified selected bash; not a string-to-local-shell escape hatch. Unavailable without that provider. |
| `pwsh` | `pwsh` | Explicit PowerShell operation requires verified **PowerShell 7+**. Windows PowerShell 5.1 and cmd are not substitutes. This requirement applies to this tool, not to normal application startup/runtime. |
| `env` | `env_setup`, `pip_install` | Selected-interpreter probe and explicit `python -m pip` installation contract, bounded requirements and verification. Never auto-installs missing dependencies; installation requires its own authorization and provider. |
| `proxy` | `net_check` | Explicit host network provider; bounded direct/proxy results. No startup probe, no process-global proxy mutation, no credentials in URLs. |
| `doc` | `ocr_doc`, `merge_files`, `pack` | Local manifest merge and bounded ZIP/TAR generation use Python, not a required external tar executable. OCR requires a host `ocr` callback (for example a separately configured docling service). No automatic docling/model download. |
| `split` | `split_file` | Bounded byte/line/literal-marker splits with a versioned, hashed manifest and safe relative output names. Manifest integrity is verified on merge. Not an unbounded regular-expression parser. |
| `img` | `read_image`, `image_info`, `crop_image`, `resize_image` | Optional Pillow, bounded single-frame raster codecs, dimensions and pixel count. Missing Pillow gives unavailable; no implicit pip install. Returns typed image/artifact descriptors; host adapter decides how to transport image content. |
| `tasks` | `plan_init`, `task_update`, `task_show`, `stage_set` | Versioned workspace/session scoped JSON ledger, atomic updates, revision conflict checks and bounded context summary. No global mutable singleton task state. Mutations require authorization. |
| `guard` | no callable tool | Mandatory structured execution/path/authorization policy, reported as `mandatory_policy` with `disablable=false`. The capability selection is metadata only and never disables enforcement, even when the toolkit or `guard` checkbox is off. No callable tool or independent permission bypass. |
| `preset` | `preset_create`, `preset_list`, `preset_delete`, `preset_inject`, `preset_eject` | Backend-neutral versioned JSON profile data. Injection records a declarative active profile; it does not patch DSH YAML/bundles, execute persona text, or silently alter trusted desktop gates. |
| `memory` | `memory_list`, `memory_read`, `memory_upsert`, `memory_forget`, `memory_restore` | Fixed host-owned persistent memory ledger, concise important facts only, 300 characters per fact, 40 records total (including recoverable forgotten entries), 1200-character untrusted context summary. Revision-checked writes; global/project separation requires a trusted project binding. See [memory contract](assistant-memory-contract.md). |

The desktop supplies the explicitly enabled `run` host terminal; it supplies no generic shell/environment, network, SFTP or OCR
provider. Merely installing a binary does not make those capabilities available.
The current desktop request has no trusted stable active-project memory binding,
so it exposes global important memories only; project memory tools fail closed
until a host supplies `memory_project_id`. Model-selected project names or paths
do not establish that binding.
The status operation reports selected capability gates, missing providers,
workspace readiness and backend package information. Image availability also
depends on Pillow. The settings explanation is intentionally conditional and
never reports an untested external provider as connected.

## Document and image resource limits

The ordinary text `fs` tools retain a 1 MiB text/batch budget. Recoverable deletion
and restoration support regular files up to 32 MiB; this does not expand the text read/write budget. Document split sources,
merged outputs, OCR input and total archive input/output are bounded at 32 MiB.
Every split chunk and manifest remains bounded at 1 MiB, with at most 1,000
chunks. A long line or marker group that exceeds a chunk budget is rejected;
use smaller line groups or byte mode. All chunks and manifest sizes are checked
before the first output write. Each individual file is atomically replaced;
cancellation or I/O failure during a split can leave completed chunks without a
manifest, so the multi-file operation is not a transaction. Merge verifies every
chunk plus the reconstructed source SHA-256 and preserves exact bytes, including
BOM, mixed newlines and leading/trailing blank lines.

Images are bounded at 8 MiB encoded input/output, 4,096 pixels on either axis,
and 4,000,000 total pixels. PNG/JPEG/WebP single-frame inputs are supported;
crop/resize outputs are metadata-stripped PNG. Raster codecs only receive bounded
in-memory streams. Image artifacts contain paths, dimensions, MIME type, byte
size and SHA-256, not inline base64. The model adapter rechecks bytes/hash and
applies a shared 32 MiB per-turn image budget. Archive output includes header
bytes in its 32 MiB cap, uses deterministic ZIP/TAR metadata and accepts only
explicit safe regular-file members; extraction is not offered.

The internal atomic writer accepts a trusted bounded `max_bytes` keyword,
defaulting to 1 MiB with a hard 32 MiB ceiling. This does not expand the public
filesystem-tool schemas or grant filesystem/mutation permission. Missing OCR
providers return explicit unavailable results; OCR text output remains 1 MiB.

## Portable profile and host migration

The portable core is under `coat_sim/agent/toolkit/`; the UI's credential-free
normalizer is `coat_sim/agent/toolkit_config.py`. Profiles and task ledgers are
ordinary versioned JSON data, independent of Pydantic AI, DSH and Qt. Their
workspace/session identity is validated before reading or updating;
copying one ledger into another scope is not an authorization grant.

To move to another host:

1. Reuse the registry's versioned tool schemas, JSON result envelopes and typed
   artifact descriptors; see [the toolkit contract](assistant-toolkit-contract.md).
2. Select an explicit authorized workspace and construct fresh per-request
   context. Intersect trusted global and profile capability lists, retaining the
   empty-means-off behavior.
3. Supply host-owned authorization/cancellation and optional execution, remote,
   network or OCR providers. Do not infer consent from persona/profile content.
4. Adapt schemas and results to the target model SDK. Preserve namespacing and
   collision checks; do not replace unrelated host tools.
5. Run the contract tests and target-host integration tests. Backend package
   discovery is diagnostic, not proof of protocol compatibility. The DSH
   integration is a portable manifest boundary, not a claimed live-tested DSH
   adapter.

There is intentionally no automatic migration of secrets, arbitrary external
paths, old DSH bundle patches, or privileged execution state. The existing
optical-domain tools remain separate from this generic capability layer.

## Offline verification

`tests/test_toolkit_settings.py` covers safe defaults, empty lists, bounded
normalization, independent mutable values, forbidden roots and credential-free
round-trip persistence. `tests/test_toolkit_settings_ui.py` loads the actual QML,
uses the new controls, saves/reopens, checks independently empty profile gates,
rejects invalid roots and verifies discarded drafts do not leak to a new
window. HTTP, model discovery, and credential operations use isolated fixtures;
no live model API, dependency installation or real remote service is required.

The standard registry currently exports 39 versioned tools. Desktop discovery still
uses only toolkit_status, toolkit_catalog and toolkit_action as top-level entrypoints.
`tests/test_assistant_private_workspace.py` exercises persistence, first-write provisioning,
read-only modes, path isolation, live revocation and actual offline model memory integration.
