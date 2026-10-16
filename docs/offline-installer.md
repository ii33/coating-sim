# Windows x64 offline installer (maintainer build guide)

> Maintainer build and verification notes for the separate self-extracting setup EXE. End users with the portable runtime ZIP should follow [portable-bundle.md](portable-bundle.md) for first launch, relocation, and recovery.

The release builder creates a per-user, self-extracting setup executable from
an isolated source snapshot and the verified Windows x64 Python environment.
It uses PyInstaller onedir rather than copying the Conda prefix into the
installed location.

The root `sheji/` design directory is local-only. It is ignored by Git, marked
`export-ignore` for source archives (including force-added files), and rejected
by the installer builder if it appears in a selected source snapshot or payload
tree. Its local files are left untouched.

## Build

```powershell
.\.conda\python.exe tools\build_offline_installer.py `
  --build-dir build\offline_installer_windows_x64_release `
  --reuse-pyinstaller-runtime build\offline_installer_windows_x64_retry2\pyinstaller-runtime
```

When `--reuse-pyinstaller-runtime` is omitted, the builder downloads pinned
PyInstaller wheels from `https://pypi.org/simple` into its isolated work
directory. The manifest records the wheel hashes, package versions, source
snapshot, included static data, output size, and SHA-256. The builder refuses
to overwrite an existing build directory or output file.

## Install and uninstall

Run `dist\CoatSim-Offline-Setup.exe` and choose a writable folder. The setup
extracts the complete onedir payload for the current Windows user; the app is
then started from `CoatSim.exe` in that folder. The setup requests no elevation,
changes no PATH, registry, or Windows security settings, and creates no
shortcut. `Uninstall-CoatSim.exe` in the installed folder removes program files
while preserving `data`, `config`, `history`, and `artifacts`. Reinstalling into
the same marked folder overlays program files while preserving existing user
files in those locations.

The small setup and uninstaller stubs use the .NET Framework WinForms and ZIP
components included with standard Windows 10/11 installations. They do not
install a framework or other system component. Both executables are unsigned;
the setup does not bypass SmartScreen.

## Included runtime and limits

The onedir package includes the CPython runtime, PySide6/Qt/QML resources,
project source, icons and fonts, material/optical catalogs, CIE data, and the
runtime dependencies present in the build environment. Only static reference
data is staged from `data/`; databases, logs, caches, credentials, histories,
attachments, and machine-specific settings are excluded.

CPU GUI and simulation workflows run offline. DirectML is an optional backend
and needs DirectX 12-compatible hardware and its normal vendor driver. External
assistant/MCP providers need user credentials and may need a CLI or network.
Optional SDK extras absent from the source environment are listed in the
package readme and manifest. CUDA/CuPy was not present in the build environment.

The current Windows host is used for build and smoke checks. Unless the manifest
says otherwise, no clean Windows VM test was performed; compatibility with a
fresh Windows installation is not claimed.
