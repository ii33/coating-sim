"""Desktop entry shared by Windows, macOS and Linux.

Use pythonw on Windows, or a desktop launcher with Terminal=false on Linux.
"""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "tools" / "launch_desktop.pyw"), run_name="__main__")
