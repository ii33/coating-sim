"""One-click entry point for the unified Qt Quick application."""


def _configure_development_mode(argv, environ, *, release_build=False,
                                frozen=False, compiled=False):
    """Make this launch's explicit CLI flag the only development-mode opt-in."""
    requested = "--development" in argv
    argv[:] = [argument for argument in argv if argument != "--development"]
    enabled = requested and not release_build and not frozen and not compiled
    environ["COAT_DEVELOPMENT"] = "1" if enabled else "0"
    return enabled


def _report_startup_error(message):
    try:
        from coat_sim.ui.ready_channel import report_startup_event
        report_startup_event("error", error=str(message))
    except Exception:
        pass


def main():
    import os
    import sys
    # Frozen/compiled releases retain the immutable backend gate even when the
    # source-only option is supplied. Also clear inherited opt-ins on ordinary
    # launches so saved settings or parent environment cannot enable tools.
    _configure_development_mode(
        sys.argv, os.environ,
        release_build=os.environ.get("COAT_RELEASE_BUILD") == "1",
        frozen=bool(getattr(sys, "frozen", False)),
        compiled="__compiled__" in globals())
    try:
        from coat_sim.ui.launcher import main as run_launcher
    except (ImportError, OSError) as exc:
        _report_startup_error(f"启动器需要 PySide6-Essentials 6.8 或更新版本。详细错误：{exc}")
        print("启动器需要 PySide6-Essentials 6.8 或更新版本。", flush=True)
        print(f"详细错误：{exc}", flush=True)
        return 2
    try:
        return run_launcher()
    except Exception as exc:
        _report_startup_error(f"启动器初始化失败：{exc}")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
