# Native PyInstaller bundle. Run only through build_dependency_manager_pyinstaller.py.
from pathlib import Path
import sys

root = Path(SPECPATH).resolve().parents[1]
# Private CPython distributions may keep Tcl/Tk outside the system linker path.
native_tk = []
if sys.platform == 'linux':
    lib = Path(sys.base_prefix) / 'lib'
    for pattern in ('libtcl[89]*.so', 'libtk[89]*.so'):
        native_tk.extend((str(path), '.') for path in sorted(lib.glob(pattern)))
a = Analysis(
    [str(root / 'dependency_manager.py')],
    pathex=[str(root)],
    binaries=native_tk,
    datas=[(str(root / 'tools/dependency_manager/runtime_manifest.json'), '.'),
           (str(root / 'tools/dependency_manager/git_runtime_manifest.json'), '.')],
    hiddenimports=['tkinter', 'tkinter.ttk', 'tkinter.filedialog', 'tkinter.messagebox'],
    excludes=['coat_sim', 'PySide6', 'numpy', 'scipy', 'matplotlib', 'torch', 'cupy',
              'pip', 'setuptools', 'pytest', 'cx_Freeze',
              'tools.dependency_manager.freeze_setup',
              'tools.dependency_manager.package_audit'],
    noarchive=False,
)
pyz = PYZ(a.pure)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name='deposition-deps',
          console=True, upx=False)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name='deposition-deps-gui',
          console=False, upx=False)
collect = COLLECT(cli, gui, a.binaries, a.datas, strip=False, upx=False,
                  name='deposition-dependency-manager')
