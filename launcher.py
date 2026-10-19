"""One-click entry point for the unified Qt Quick application."""


def _configure_development_mode(argv, environ, *, release_build=False,
                                frozen=False, compiled=False):
    """Enable bounded developer tools by default for source launches only."""
    disabled = "--no-development" in argv
    argv[:] = [argument for argument in argv if argument not in ("--development", "--no-development")]
    enabled = not disabled and not release_build and not frozen and not compiled
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
    # source-only option is supplied. An explicit --no-development switches off
    # developer tools without changing the release/build security boundary.
    _configure_development_mode(
        sys.argv, os.environ,
        release_build=os.environ.get("COAT_RELEASE_BUILD") == "1",
        frozen=bool(getattr(sys, "frozen", False)),
        compiled="__compiled__" in globals())
    from coat_sim.project_logging import PROJECT_ROOT, install_process_log
    from coat_sim.assistant_diagnostics import source_id, trace
    from coat_sim.version import BUILD_IDENTITY
    install_process_log("desktop")
    trace("desktop_process_start", role="launcher", version=BUILD_IDENTITY.version,
          git_sha=BUILD_IDENTITY.git_sha, source_id=source_id(PROJECT_ROOT))
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
        trace("desktop_process_failed", error=exc, role="launcher")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
