"""Native cx_Freeze build description; never imported by the maintenance tool."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tools.dependency_manager.package_audit import windows_runtime_sources

    freeze = importlib.import_module("cx_Freeze")
    windows_files = windows_runtime_sources(Path(sys.base_prefix)) if sys.platform == "win32" else []
    options = {
        "packages": ["tools.dependency_manager", "tkinter"],
        "includes": ["tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox"],
        "excludes": [
            "coat_sim", "PySide6", "numpy", "scipy", "matplotlib", "torch", "cupy",
            "cx_Freeze", "patchelf", "setuptools", "pip", "pytest", "unittest",
            "tools.dependency_manager.freeze_setup",
            "tools.dependency_manager.package_audit",
        ],
        "path": [str(ROOT), *sys.path],
        "include_files": [
            (str(ROOT / "tools/dependency_manager/runtime_manifest.json"), "runtime_manifest.json"),
            (str(ROOT / "tools/dependency_manager/git_runtime_manifest.json"), "git_runtime_manifest.json"),
            *((str(path), path.name) for path in windows_files),
        ],
        "bin_includes": [path.name for path in windows_files],
        "bin_path_includes": [str(Path(sys.base_prefix) / "lib")],
        # True triggers freeze-core's opaque aka.ms vc_redist download/repack.
        # Use only hash-verified official CPython DLLs, with a separate release gate.
        "include_msvcr": False,
        "zip_exclude_packages": ["*"],
        "silent_level": 1,
    }
    freeze.setup(
        name="deposition-dependency-manager",
        version="0.1.0",
        description="Independent CPython/Tk maintenance tool",
        options={"build_exe": options, "egg_info": {"egg_base": str(ROOT / "build/dependency_manager")}},
        executables=[
            freeze.Executable(str(ROOT / "dependency_manager.py"), base="console", target_name="deposition-deps"),
            freeze.Executable(str(ROOT / "dependency_manager.py"), base="gui", target_name="deposition-deps-gui"),
        ],
    )


if __name__ == "__main__":
    main()
