# Historical archive: dependency-manager native payload and licence evidence

**Archive notice, 2026-10-14:** The current dependency-manager delivery direction
is Python source plus the Windows `dependency_manager.cmd` bootstrap. Executable
wrapping was cancelled; this document does not describe a current build or a
shippable EXE. The component inventories below preserve evidence from specific
past packaging investigations. They are not general redistribution approval,
do not replace the active `release_policy.json`, and do not establish clean
Windows/Tk acceptance. Any future packaged build must pass the existing
platform-specific rights, complete-notice, payload, native-closure and
acceptance gates. The historical record does not change or waive those gates.

The maintenance tool's Python/Tk runtime is separate from the project's target
venv. The material below concerns a native wrapper around the maintenance
manager, not project dependency installation. Project code keeps its MIT
licence; the third-party notices below govern their respective components only.

## Historical packager investigations

The cx_Freeze inventory below records an exact-payload investigation. A later
PyInstaller 6.22.3 candidate is archived as validation-only evidence; it does
not belong to the source-based deliverable. Executable packaging was cancelled
on 2026-10-14. Neither investigation authorizes publication of a binary. New
platform payloads need separate notice closure, rights review and native/GUI
acceptance before any publication.

## Historical fixed Linux profile

`tools/dependency_manager/licenses/linux-x86_64-cp312-profile.json` seals the
reviewed Linux x86-64 / CPython ABI312 / freeze-core0.8.0 native payload. Its
official PyPI wheel URL and SHA256 are fixed. Every ELF path and complete byte
hash is checked, including both entrypoints and CPython extensions. An unknown,
missing or modified ELF is rejected before running the bundled doctor. Retained
Build IDs or matching SONAMEs alone are insufficient. ZIP members are checked
too: GNU readline/GDBM, build-only tools and project/Qt packages cannot leak in.
Other native platform/ABI profiles need their own review; this is not a universal
approval of everything in a freeze-core wheel.

Actual runtime is CPython3.12.15, Tcl8.6.18 and Tk8.6.18, even when the build
interpreter is3.12.14. The complete corresponding CPython notice is archived.
Native component/notice entries are included in `licenses/inventory.json` and
`build-manifest.json`, not merely placed in an unrelated research list.

| Bundled component | Licence / additional material |
| --- | --- |
| CPython and static stdlib | PSF and complete bundled stdlib notices |
| freeze-core startup | MIT; cx_Freeze's PSF-derived build notice retained |
| Tcl8.6.18 | Tcl core, Henry Spencer regex, LibTomMath Unlicense, Unicode data |
| Tcl's generated date parser | Bison3.8.2 GPL3+ **with generated-parser exception** |
| Tk8.6.18 | Tk core and David Koblas giftoppm/GIF notice |
| OpenSSL3.5.9 | Apache2 |
| libffi3.0.13-19.el7 | MIT/Public Domain; official RPM provenance |
| bzip2 / Xft / libpng / liblzma | their versioned permissive/public-domain notices |
| fontconfig2.13.0 | MIT/file-level notices plus Unicode9 CaseFolding notice |
| FreeType2.8 | FTL selected, required FreeType credit, BDF/PCF/hash/Open Group notices |

The user explicitly approved the Bison generated-parser exception in this Tcl
component. That scoped approval does not approve arbitrary ordinary GPL runtime
dependencies. The tool is not a parser generator and does not redistribute a
modified parser-generator skeleton; the upstream exception is preserved. See
[GNU Bison's conditions](https://www.gnu.org/software/bison/manual/html_node/Conditions.html)
and the [exact Tcl generated parser](https://github.com/tcltk/tcl/blob/core-8-6-18/generic/tclDate.c).
Patchelf was an already-declared GPL3 **build-only executable** in that
historical profile; it was excluded from the payload. No inference about other
packagers or payloads follows from this record.

### libffi/libuuid evidence and OS boundary

`native-provenance.json` records SHA256 of the official CentOS7.9 RPMs and their
original libraries. GNU Build IDs and exact `.text`/`.rodata` SHA256 values match
the wheel libraries, identifying libffi3.0.13-19.el7 and libuuid2.23.2-65.el7.
Auditwheel/patchelf modify ELF layout/dynamic metadata, so whole-file hashes of
the modified libraries need not equal the RPM originals.

The libuuid RPM says BSD, but its upstream build also compiles
`lib/randutils.c`, whose header says LGPL. Without corresponding downstream
source/patch review, BSD-only metadata is not enough to redistribute that copy.
This is an obligation gap, **not a blanket ban on LGPL**. The reviewed profile
instead verifies the original fontconfig/libuuid bytes, changes only the
generated fontconfig `DT_NEEDED` to `libuuid.so.1`, and removes the vendored
libuuid. No OS library is modified. Neither libuuid software nor an unrelated
util-linux/GPL source tree is shipped.

The reviewed historical bundle required OS libraries. Recorded direct imports were glibc/pthread/dl/util/math, X11/Xrender, expat, zlib
and libuuid. Their transitive system closure is owned by the OS distribution,
not bundled or relicensed here. A missing system libuuid/X11 was an explicit
Linux prerequisite; the independent tool did not need project Python, Qt or
venv. This did not promise a static executable for every Linux image.

Version-specific original notices, source URLs, whole-source hashes and
extraction selectors are in `research-inventory.json`. A combined notice file
contains unmodified upstream copyright/permission blocks, not rewritten terms.
Research candidates excluded from the final profile are not asserted to be
bundled. Unicode9 uses its2016 notice; Tcl's Unicode17 source copyright and the
current official UnicodeV3 grant are both retained, without rewriting their years.

## Windows: verified provenance, remaining release conditions

`windows-cpython-provenance.json` records an actual read-only inspection of:

- official Python3.13.16 amd64 embedded ZIP, SHA256
  `97dae5274cc54867065e8d5a3226e48c35017ed332a0fdb0e27d5b5821961297`,
  corroborated by its [official release page](https://www.python.org/downloads/release/python-31316/)
  and Python Release Managers' [SPDX SBOM](https://www.python.org/ftp/python/3.13.16/python-3.13.16-embed-amd64.zip.spdx.json);
- official freeze-core0.8.0 cp313 win_amd64 wheel from PyPI, SHA256
  `b2776e4dc73b86075c565d9d3508e998c9047eeb83258bcb3b2fc196271dc473`;
- the ZIP's full Windows `LICENSE.txt` and unchanged `vcruntime140.dll` /
  `vcruntime140_1.dll`, with per-file hashes.

The archived build description disabled `include_msvcr`: enabling that option
would have downloaded/repacked a separate Microsoft VC Redistributable from
aka.ms. It allowed **only** the two hash-verified DLLs provided by pinned official
CPython, copied from the normal build interpreter's root. Different DLLs failed
closed. The reviewed build did not install VC redistributables, change PATH or
copy OS UCRT files.

Static inspection covered30 CPython and5 freeze-core PE images; their MSVC
imports require only VCRUNTIME140/140_1, with no MSVCP/CONCRT dependency found.
Windows10/11 provide [OS UCRT](https://learn.microsoft.com/en-us/cpp/windows/universal-crt-deployment?view=msvc-170),
but this does **not** supply VCRUNTIME140. Embedded CPython has no Tk, so this
evidence does not yet establish the final full-Python/Tk application's PE
closure, delay-loaded imports, GUI rendering or clean-VM independence.

Official CPython's Windows notice provides conditional redistribution terms
and requires downstream distributors/external end users to agree to protective
Microsoft-code terms. [Microsoft's separate VC redistributable documentation](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files?view=msvc-170)
also limits that distribution route to licensed VS users. A VS installation
alone is not proof of the applicable rights; no new VS subscription is assumed.
The CPython-DLL route needs a recorded rights review rather than conflation with
copying arbitrary DLLs from Visual Studio. No agreement was accepted on the
user's behalf, and this evidence alone is not a legal guarantee of approval.

### Proposed downstream assent boundary (not implemented or accepted)

A distribution wrapper/installer should display the complete third-party
Windows CPython notice and record explicit assent **before loading these DLLs**.
The wrapper must use OS facilities, not the frozen Python/Tk executables that
already load the DLLs. GUI refusal exits without launch. Unattended CLI must
require an explicit per-installation acceptance flag referencing the precise
notice SHA256; missing/changed notice or refusal must fail closed. No silent
defaults, network transmission, global settings or restriction on project MIT
code. The protected scope is Microsoft Distributable Code only.

This is a proposal, not a delivered installer/EULA workflow. Windows remains
blocked until this mechanism/rights review, final Tk PE closure and clean-VM
acceptance are verified. `release_policy.json` lists each requirement separately;
CI reports BLOCKED and does not equate successful offline tests with a released
independent Windows application.

## Archived proposal: future environment/build tooling

This section records a proposal only. Runtime/development profiles select the
project's target dependencies; optional CPU/GPU/AI/render features remain
separate. Tool-build dependencies must never be installed into the target
project venv. The proposal for a future Build/Artifacts page and platform
artifact manifests was not implemented by the current source-delivery route.
Building, rights review, release approval and publishing remain separate steps.
