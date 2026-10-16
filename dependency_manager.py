"""Standalone dependency tool entry point; never imports the project runtime."""

from tools.dependency_manager.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
