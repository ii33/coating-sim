# Scoped resource library windows

Date: 2026-10-19. No numerical algorithm or file-format change.

## Inputs, ownership and navigation

The global launcher entry remains project-free. `GlobalResourcesBridge` owns a
`GlobalResourceLibrary`; it cannot write a running project. `project_resources`
is a separate canonical project module, shown as an independent tool leaf in
the existing two-level window menu. No existing film/deposition/simulation group
changes, and the collapsed selector still identifies the current window.

Opening this module explicitly reserves the existing `(binding,module_key)`
route. `ProjectResourcesBridge` borrows a fork of the current binding's
`ProjectResourceWorkspace.service`, including its shared mutation gate; it
never opens another store, acquires another lease, or starts a project runtime.
Each project owns at most one visible resource-library route. Reopening activates
that view. Other projects own different routes, services and catalogs.

`ResourceLibraryWindow` is the common presentation, reached by the compatible
`GlobalResourcesWindow` wrapper or the project desktop. It uses the shared
operation bar, theme tokens, responsive list/information split, scrolling and
ordinary window geometry. Categories contain only kinds returned by the actual
catalog. Material profiles, optical data, film designs, processes, machines,
sources, substrates, masks and actual saved simulation inputs retain their real
types. There is no all-categories entry. The first supported existing category
is selected explicitly, preferring materials; switching category clears
hidden selections and their old preview. The material class further filters
only actual declared film/substrate/medium/other uses, with no mixed all-uses
entry. Multi-role profiles remain reachable in each applicable use. Legacy
profiles retain the existing material library's category/default-film convention;
optical-only profiles lacking declared roles/category use a separate
unspecified-use filter. The browser maps material profiles and optical projections to one Material
category. When both represent the same material ID, the editable profile is
shown once; an optical-only record without a profile remains visible under its
unspecified usage and opens the existing editable projection. Stored types and
dependency identities remain unchanged, without inferred suitability.

## Reads and transfers

Project default/refresh calls `inspect_resources("project")`, so it lists only
saved documents from that current project. Unsaved editor content remains in its
original window. Search and information inspect the loaded category/catalog.
Import explicitly chooses an external resource, exact project/package, or the
global catalog; the page labels that source and always targets the current
project. Export starts from the current project catalog and targets the global
library or a new `.cproject`. Global mode imports only into the global library
and exports only a new `.cproject`. Browsing does not publish anything.

All exchange actions reuse the existing dependency closure, stale-source,
conflict-decision and atomic publication contracts. Different-content conflicts
require explicit choices; there is no delete or implicit overwrite action.
Failure keeps a visible error and does not claim success. Empty, busy and failed
states differ. Inspect/preview are asynchronous and generation-cancellable;
publication cannot be cancelled. Closing cancels reads and releases late tokens.

## Refresh after a successful local commit

A store publication notifies weak, cancellable headless observers only after the
writer lock is released. Notification failures cannot change a committed save's
result. The current-project browser subscribes to its exact existing store and
binding; its queued GUI signal never creates a store or window. Material editors
also report successful global save/import/restore commits explicitly, rather
than inferring success from a dirty-state transition.

Known commits coalesce into a scope-local asynchronous catalog refresh. A commit
arriving during an earlier read is retained and forces a subsequent fresh read.
The previous catalog remains rendered until its replacement succeeds, so an
open optical-data form is not hidden or closed during the read. The refresh
changes only catalog rows: it never reloads the retained material draft, chooses
an editor record, or replaces dirty text. A failed read remains visibly failed
and cannot preview deletion or masquerade as a current assistant catalog. An external import
catalog stays selected until the user returns to the owned library. While the
owned catalog is pending refresh, transfer/deletion preview is unavailable with
a visible updating state; old plans are invalidated. A fresh read never retries
or confirms deletion automatically, and core stale/reference checks remain.
Closing unsubscribes and stops queued refreshes without reviving the view.

## Close and read-only assistant boundaries

Project windows use `ProjectWindowCloseFilter` and the existing whole-project
close coordinator, including retained sibling changes, last-window confirmation
and lease release. Publication rejects native close until an explicit result.
Closing this view releases only its exchange tokens, never a sibling editor.

Six-field project target routing accepts `project_resources` for existing
open/list/read/close operations. Its editor capability matrix is read-only,
with no update/action whitelist. `project_editor_snapshot` exposes only the
already-loaded current-project resource catalog and current category/selection,
with bounded `resources` pages. It performs no file/catalog I/O, creates no
owner and never substitutes a global or other external import catalog. Busy,
unloaded, closed, frozen or external-import views are explicitly unavailable.


## Shared action and type boundary (2026-10-19)

| Entry | Browse/selection | Import | Export |
| --- | --- | --- | --- |
| Global assets | Own library or external file/package/descriptor, one current category | Preview selected external roots into global library | Selected owned roots to a new package |
| Project assets | Current project or explicit global/file/package/descriptor, one current category | Selected external roots into this project | Selected owned roots to global library or a new package |
| Designer | Only its target resource kind(s), including package/descriptor catalog | Same-kind roots into this project | Same-kind roots via existing target choices |

`ResourceLibraryWindow` owns the category and selection; its bridge owns transfer
catalog/preview lifetime, and the retained material controller owns material drafts.
One `ResponsiveOperationBar` contains the category selector, adjacent information,
refresh, import/export menus and remaining actions. It stays on one row and uses
its existing trailing overflow. Embedded material content hides its own resource
bar; material save/information and dedicated file actions remain in the main bar.
The project import/export menus expose global-to-project and project-to-global
copy without changing either library's ownership. Global assets remain project-free.

`ProjectResourceTransferController.transferKinds` is the designer type contract.
Its catalog projection and QML category/row/file filters use that contract. Preview
rejects IDs absent from this projected catalog or of another type, before starting
a publication worker. General library previews additionally reject mixed categories
or IDs outside the current category. Empty selection remains an explicit failure.
Only selected roots are restricted: dependency closure is computed from the original
backend catalog and may include other kinds. Conflict, cancellation, failure, close
and isolation behavior remains as specified above. Material/third-party file actions
remain in their corresponding target editor/menu; they do not expand generic root scope.
