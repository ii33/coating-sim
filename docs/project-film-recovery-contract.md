# Project film recovery contract

历史开发记录：本页的运行数字与环境仅说明当时的限定验证，不代表当前公开提交通过 CI；原始私有执行记录未随公开源码发布。

`coat_sim.core.project_film_recovery` is the headless boundary for capturing and
explicitly restoring one project-owned film editor draft. Its snapshot contains
the project identity, dirty state, managed film ID and saved revision (when the
editor is attached to one), exact expression text, and a validated frozen film
payload with the complete project-provided material and optical-set closure.
Valid editable drafts may have intentionally blank substrate, layer, medium, or
symbol selectors. Those blanks are preserved without inserting Air or another
default material. A nonempty but unresolved material/data-set reference is an
error. The implementation does not consult global material catalogs.

`snapshot_film_recovery(workspace)` refuses pending confirmations and pending
expression work. `validate_film_recovery(workspace, snapshot)` returns detached
validated data and performs no writes. It rejects malformed/non-finite data,
wrong project identity, incomplete material closure, and a missing or stale
managed source document.

`restore_film_recovery(workspace, snapshot, approved=..., expected_current_...)`
is never part of normal project opening. A caller may set `approved=True` only
after explicit user approval and must pass both live editor revision values
captured when that approval was requested. Session revision is required because
workflow settings can change without advancing the design revision; saved
document ID/revision guards can additionally be passed.
Pending expression work or confirmations block replacement.

Restoration changes only the in-memory editor. It preserves the source identity
as provenance, attaches the recovered data as a dirty pathless draft, and never
reuses the source path. The next ordinary Save creates a new managed film
document, leaving the source bytes untouched. The original snapshot remains
available to other recovery domains; this module owns only film data.
