# Recent-project launcher and independent project runtime

Date: 2026-10-16. The normal launcher is a recent-project picker plus New, Open,
Global Resources, Settings and About/Help. Project sessions now live in independent detached
per-project runtimes. See the [runtime contract](project-runtime-contract.md)
for exact ownership, startup and settlement boundaries.

## Input, output and ownership

New submits an exact new destination directory, display name, optional summary
and notes to an independent runtime; that process performs
`ProjectStore.create(root, name, summary=..., notes=...)`
and then the same descriptor admission as Open. Launcher exit cannot interrupt
the atomic create transaction. A created project is retained if later UI startup fails. Open accepts an existing exact `.coatproj` file,
including arbitrary basenames and case-insensitive suffixes. Directories,
`project.json`, and `.cproject` packages are not opening aliases. The project
format and external-input dependency-copy rules are unchanged.

`ProjectLauncherAdapter` owns recent history and asynchronous picker state only.
It has no host, workspace, owner lease or runtime process lifetime handle.
`ProjectRuntimeOwner` owns the existing `ProjectSessionHost`, lifetime leases,
`ProjectFilmDesktop`, default initialization and recovery/close coordinators in a
separate process. `ProjectSessionHost` remains Qt-free. Saved membership still
belongs to `ProjectStore`, drafts to their film/resource/compute workspaces.
There is no active-project singleton.

The QML `launcherProjects` facade exposes `projectEntries` rows
`{descriptorFile, name, lastOpened, available, active}` plus the validated
descriptor-only `summary`, `notes`, `informationAvailable` projection when readable, `projectBusy` and
`projectError`, and background-only `recentBusy`, notified by `projectsChanged`. `createProject(name, rootPath)`
`createProjectWithInformation(name, rootPath, summary, notes)` and
`openProject(descriptorFile)` acknowledge request admission, not successful
startup. `removeRecentProject(descriptorFile)` remains history-only.
`copyRecentProject(descriptorFile, projectId, destination)`,
`exportRecentProject(descriptorFile, projectId, destination)` and
`trashRecentProject(descriptorFile, projectId)` acknowledge asynchronous file-operation
admission. `projectFileMessage` reports successful completion, separately from
`projectError`. A row's `projectId` is a transient validated-descriptor projection,
never an additional persisted history field. `refreshProjects()` refreshes
history and filesystem availability.

Recent history uses the existing per-user LYX/coat-sim settings, a separate
`recentProjects/v1` group, with cross-process locking and re-read before writes.
Only canonical descriptor path, name and successful-open timestamp persist.
PID, tokens, owner generations, availability and drafts never persist there.
History getters return cached rows; refresh/remove/path availability checks run
on workers with independent QSettings instances. Missing files are visibly
unavailable and remain removable. Removing a current record is not a permanent
block: a later successful open, including one already in flight, may re-add it. A failed open does
not add or promote a record. The exact owner's successful authenticated
activation, following startup ready if a new runtime was needed, is required
before a record is promoted.

Repeated opens resolve ID, canonical root, exact descriptor and session through
the authenticated shared assistant and raise the original film designer.
The six-field activation target, PID creation identity, nonce challenge and
client/session generation checks remain in force. Activation does not accept an
arbitrary window or endpoint. Copied IDs/root conflicts and conflicting
selected descriptors are rejected. A racing opener cannot replace a held lease.
Unavailable/older assistants report explicit errors and are never silently
replaced. Already-running dev.08 sessions are activated where supported;
unsaved drafts are not hot-migrated.

File-dialog URLs cross `LauncherBridge.localFilePath(QUrl)` before entering text
fields. A valid accepted Open selection submits once; cancelling submits none.
New destination pickers still select directories. Minimum-size content remains
vertically scrollable and the attribution footer remains outside the scroll.
The launcher exposes no direct business-module menu.

## Failure, cancellation and asynchronous boundary

One picker request is admitted at a time. Store creation and detached startup
run off the GUI thread. The runtime alone admits its host and constructs Qt
windows. Busy clears after actual success or failure. A successful project
creation is not deleted if its later runtime startup fails.

Runtime stdin is DEVNULL and stdout/stderr go to a file log, independent of the
launcher's lifetime. Startup must report ready/error through the authenticated
one-shot channel after real admission and updated assistant registration. The
requesting client then confirms the actual owner via exact activation. It never
calls terminate/kill on a contender or existing owner. Startup failures close
newly acquired leases with `clean=False`; late admission and package transactions
remain tracked until settled.

Closing the launcher exits only its presentation process, including its own
Settings/About windows. Projects and the global AI assistant continue. A new
launcher reads the same history and activates an existing runtime.

### Last project window and explicit Close Project (2026-10-15)

The last actual project module Close and the shared project-window selector's
Close Project command use `ProjectCloseCoordinator` for the exact session.
Hide/minimize retain ownership. Clean close removes views/session and releases
the lifetime lease. Dirty close presents Return to save, Discard and close,
and Cancel in a runtime-owned Quick window, available without a launcher.
Return to save and Cancel retain every draft and lease; Return to save raises
the relevant editor and never claims to have saved it. Fresh expression,
resource, compute and local-job preflight still applies before discard. Queued
review/session generation checks and accepted-Close-only disposal are retained.

A runtime exits after its project has actually closed and outstanding admission,
package/recovery transactions and deferred lease releases have settled. A release
failure is visible rather than silently reported as successful. Native Qt Quit
uses the same owner preflight; global assistant Exit Application uses the existing
aggregate prepare/cancel/commit protocol. One runtime never broadcasts global
exit or stops another project's jobs. Service-owned computations remain separate.

The project selector also exposes Engineering Management (工程管理…) for recovery
candidates, reviewed archives and saved-input package operations. Import restores
a new independent project and opens it through the same detached runtime path;
it does not admit a second project into the current runtime.

## Verification limit

Offscreen contracts verify state ownership and QML semantics. Isolated real
multi-process smoke verifies launcher exit, owner reuse and runtime close.
Windows native/desktop behavior still requires its separate acceptance matrix.
The existing `context_tokens` KeyError is unrelated and unchanged.

## Source and installed launch entry

The supported entry is `launcher.py`, run with the selected project's own
interpreter: `.venv/Scripts/python.exe launcher.py` on Windows or
`.venv/bin/python launcher.py` on Linux/macOS. The independent installer creates
scripts and shortcuts against that interpreter and entry. Application arguments
are passed directly; source development tools require the explicit
`--development` opt-in. Linux desktop launches inherit the user's actual display
session; `--software` is available for the Qt Quick software renderer.

`launcher.pyw` remains the application-owned no-console adapter used by taskbar
relaunch and the existing desktop host. It delegates to
`tools/launch_desktop.pyw`; neither is an installer. Duplicate shell/PowerShell
wrappers and the retired legacy UI launchers are removed. There is no automatic
environment selection, package installation or system-Python fallback entry.

## Information and global-library window

The read-only recent-project hover details and global resource window
are specified in [Launcher information and global resources](launcher-information-global-resources.md).
Recent settings retain their three-field schema. Current metadata is read by
the history worker without opening project payloads or a writable session.
The global window supports category/name filtering, information, explicit
import/preview/conflict choices, new-package export and the existing material
manager. It does not add a direct menu of global design editors.


## Compact recent rows and explicit removal (2026-10-16)

Each recent row occupies exactly two content lines: a left-aligned, single-line
open-by-name action and right-aligned Copy / Export / Remove actions, followed
by one elided summary line (an empty summary has a placeholder). No path/status
line or information foldout remains. Hover details retain the complete actual
name, summary, notes, exact descriptor and available opening timestamp. New
continues to accept summary and notes. Theme sizes are logical pixels/points;
rows grow only with the actual font metrics, never a fixed 112-pixel minimum.

Copy and Export reuse `export_project` / `import_project`, including bounded
saved-input validation, a fresh copy ID, atomic publication and refusal to
replace an existing target. Runtime files and results are excluded. They use
the existing root/ID lease for the duration of the operation; an already-running
owner is refused with instructions to save current changes and close that
project. The launcher never activates, closes or terminates a project to make an
operation succeed. Copied projects are recorded in recent history, without
opening a runtime. The launcher retains no session or lifetime owner lease.
File dialogs select a destination only; no overwrite occurs even if a native
save picker offers an overwrite prompt. Failures leave the original project
unchanged and report an error. Accepted publication is not cancelled by closing
its input dialog; late results cannot update a stopped launcher.

Remove is the sole row entry (2026-10-26). Its single confirmation defaults to
history-only and offers "同时将整个工程文件夹移入回收站". Cancel/Escape changes neither
history nor files. When checked, a read-only background request resolves the
canonical project folder. The modal names the project, shows that complete folder
path and states "包含文件夹内全部内容". There is no file-subset mode or scope selector.
Confirmation recycles that entire folder, including ordinary attachments, old
revisions, results and any other contents, even other project descriptors.
No contents scan, required empty-layout check or attachment whitelist is performed.

`prepareRecentProjectDeletion(descriptor, id)` publishes `projectDeletionPlan` with
the canonical folder and an identity token; `trashRecentProjectWithPlan` requires
that token and the captured descriptor/ID. The adapter owns admission, busy state
and errors; stopped launchers ignore late results. The service validates folder
and descriptor identities again under the existing root/ID lease. Replacing the
confirmed folder or descriptor requires a new confirmation. The confirmation
covers all contents of the displayed folder at execution time.

Disk roots, the user home, standard Desktop/Documents/Downloads and temporary
roots, system directories, application source/working roots and their containing
ancestors cannot be recycled as project folders. Errors identify the concrete
folder and reason; no partial-file deletion is substituted. Descriptor path
components must not be links. An open owner is reported with instructions to save
and close the project; the launcher never closes it or discards unsaved work.

The service calls the operating system's Recycle Bin once for the selected folder.
History changes only after OS success and confirmation that the source folder is
absent. Failures keep the row and show the affected path and error. There is no
staging, permanent-delete fallback or permission/security setting change. Recovery
is through the operating system's trash UI. If the launcher exits before applying
the result, a stale recent record may remain; this never starts another recycle.
The identity check does not guarantee protection from a hostile noncooperating
process rewriting the filesystem between the final check and the OS move.

Dialog dismissal restores the launching control's focus when it still exists;
row removal cannot restore focus to a destroyed delegate. The history worker
remains the only history writer. Pending operations share the picker's existing
busy admission, duplicate actions are disabled, and failures do not masquerade
as empty history. Tests change only self-owned temporary projects and explicit
trash stubs; Windows native recycle behavior needs its own temporary-project
acceptance and a sharing failure remains a supported safe error.

Native Linux Qt 6.12 recycling was checked with a self-owned temporary standard
project and isolated XDG trash: both its descriptor and a result file were
recoverable at the reported trash path. Use the `QFile` instance overload and
its updated `fileName()`; this PySide6 build's static overload returns a boolean
despite the tuple signature in its stub. The instance call and injected test
mover share an explicit `(success, trash_path)` wrapper contract.


### Hover bounds and rendering evidence

Hover details use plain text throughout, including strings containing markup.
The complete stored name, summary, notes, descriptor and timestamp remain
unchanged. The popup is capped at 600 × 360 logical pixels and further limited
to the intersection of the current window overlay and screen. Long text wraps
inside a clipped vertical ScrollView; moving into that popup keeps it open for
scrolling. A short leave delay lets the pointer cross from the row to the
popup. Leaving both surfaces dismisses it. The Item's explicit root-level
`hostWindow: Window.window` is the sole attached-window projection used for
screen-to-window positioning; delegates only read that root property.

`tests/test_launcher_recent_projects_qml.py` verifies real pointer hover after
settling, complete name/summary/path text, visible painted text pixels in the
captured popup, and dismissal on leave. Separate non-hover layout captures
avoid mixing a popup's opening transition with card-geometry evidence. The
maximum supported font-scale test supplies 20,000-character summary and notes,
checks the popup bounds and confirms that its full content can scroll. Default
and 2× scaling captures show actual text, not an empty popup surface. Earlier
141-case focused launcher/package/runtime coverage passed before this final
hover-only correction; it is not described as a full-suite pass or native
Windows acceptance.

### Windows native recycle acceptance (still pending)

Use only a newly created, uniquely named temporary standard project, with a
self-owned marker in its results directory. Record its exact descriptor,
project ID and directory before testing; do not select a real user project.

1. Open Remove and cancel, including once after selecting the recycle checkbox.
   The record, descriptor and marker must remain unchanged. Reopen Remove: its
   default must again be history-only.
2. Confirm history-only removal. The recent record must disappear while the
   descriptor and marker remain. Reopen that descriptor to restore its recent
   entry; this also starts its ordinary project owner.
3. While that owner remains open, request whole-directory recycling. It must
   fail without closing the owner, moving files or removing the recent record.
   Do not drop the lease, change permissions or force a permanent deletion to
   make this test succeed.
4. Close the temporary project normally and wait for its owner to settle. From
   the launcher, choose recycling again and verify the exact directory shown in
   the confirmation. On OS success, the entire original directory must be
   absent, its recent record removed, and both descriptor and result marker
   recoverable through the operating system's Recycle Bin. Restore only this
   self-owned test directory through the OS when checking recoverability.
5. If the OS refuses the move, keep the descriptor, marker and recent record,
   report the visible error and record this as a safe failure. Never report a
   successful native recycle or release the lease early merely to bypass a
   Windows sharing error. Verify no permanent-delete fallback was attempted.

The controlled-stub tests additionally cover unknown directory ownership,
nested projects, links/reparse points, changed directory contents, stale
project identity and OS recycle failure. Linux Qt 6.12 native success evidence
above does not establish Windows behavior.


### Recent-card action feedback (2026-10-16)

Only the card's Copy, Export and Remove controls use the local
`RecentActionButton` visual. Their idle and disabled surfaces are transparent
with no border. Hover and press animate a theme-accent overlay over 120 ms;
keyboard navigation exposes the normal theme focus ring. Mouse focus alone does
not leave a permanent border. Native Button semantics, text/accessibility,
geometry, enabled guards, confirmation flows and file actions are unchanged.
Other buttons continue to use the shared `ThemeButton` styling.
