"""Shared offline defaults and explicit test-tier assignment.

The tier manifest is intentionally module-based: every item in a module gets
the listed tiers. Any test module not listed in a specialized tier remains in
``quick``. This makes new tests visible by default while allowing the tier
union to cover the complete collected suite.
"""

from __future__ import annotations

import ast
import ipaddress
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

import pytest


_ORIGINAL_SOCKET_METHODS: tuple[object, ...] | None = None
_TEST_QT_APPLICATION: object | None = None
_TEST_CACHE_TEMP = tempfile.TemporaryDirectory(prefix="deposition-sim-pytest-")
_TEST_CACHE_ROOT = Path(_TEST_CACHE_TEMP.name)

QT_MODULES = frozenset({
    "test_film_resource_estimate_ui.py",
    "test_film_optimization_continue_gui.py",
    "test_film_optimization_continue_qml.py",
    "test_film_synthesis_deepseek_closeout_gui.py",
    "test_film_synthesis_gpu_forward_gui.py",
    "test_film_optimization_cancel_spectrum.py",
    "test_film_optimization_cancel_native.py",
    "test_film_optimization_cancel_apply_review.py",
    "test_film_optimization_bounds_edit_review.py",
    "test_film_synthesis_parallel_gui.py",
    "test_film_synthesis_quick_controller.py",
    "test_film_synthesis_gui_review.py",
    "test_film_synthesis_gui_repairs.py",
    "test_film_physical_generator_usability.py",
    "test_film_synthesis_gui_usability_review.py",
    "test_film_synthesis_unknown_k_review.py",
    "test_film_synthesis_queue_gui.py",
    "test_assistant_message_body_qml.py",
    "test_assistant_message_lifecycle_qml.py",
    "test_toolkit_settings_ui.py",
    "test_assistant_compute_ui_authority.py",
    "test_assistant_discussion_ui.py",
    "test_project_editor_snapshot.py",
    "test_project_editor_context.py",
    "test_project_resource_editor_snapshot.py",
    "test_project_compute_editor_snapshot.py",
    "test_project_editor_operations.py",
    "test_project_editor_operations_context.py",
    "test_project_compute_editor_operations.py",
    "test_project_resource_editor_operations.py",

    "test_about_current_workflow.py",
    "test_film_optimization_restore_layout.py",
    "test_launcher_information_global_resources.py",
    "test_responsive_operation_bar.py",

    "test_compute_server_category_ui.py",
    "test_film_manual_draft_sync.py",
    "test_film_optimization_target_sampling.py",
    "test_film_optimization_remote_async_qt.py",
    "test_project_material_catalog.py",
    "test_project_module_selector.py",
    "test_launcher_project_adapter.py",
    "test_launcher_project_manager_ui.py",
    "test_project_compute_controllers.py",
    "test_project_material_qml_binding.py",
    "test_mask_optimization_ui.py",
    "test_application_process_smoke.py",
    "test_project_application_client.py",
    "test_project_application_lifecycle.py",
    "test_project_recovery_coordinator.py",
    "test_assistant_process.py",
    "test_tfcalc_import_quick.py",
    "test_assistant_lifecycle_contract.py",
    "test_film_optimization_run_contract.py",
    "test_settings_coordinator_contract.py",
    "test_numeric_format.py",
    "test_input_focus_policy.py",
    "test_asset_designer_workflow.py",
    "test_assistant_bubble.py",
    "test_assistant_window_list.py",
    "test_compact_window_chrome_ui.py",
    "test_compute_queue_tabs.py",
    "test_compute_start_failures.py",
    "test_cub_interfaces.py",
    "test_designer_typed_save_dialogs.py",
    "test_desktop_compute_queue.py",
    "test_editor_reload_and_live_inputs.py",
    "test_film_design_generator_qt.py",
    "test_film_design_gui.py",
    "test_film_design_layout_geometry.py",
    "test_film_design_reference_qt.py",
    "test_film_floating_ui.py",
    "test_film_plot_settings.py",
    "test_film_plot_layout_tool.py",
    "test_film_quick_controller.py",
    "test_film_simulation_quick.py",
    "test_film_simulation_file_dialog_exports.py",
    "test_local_file_dialog_paths.py",
    "test_film_simulation_quick_ui_refinements.py",
    "test_film_simulation_settings.py",
    "test_film_simulation_toolbar.py",
    "test_sidebar_toggle_controls.py",
    "test_film_spectrum_plot_bounds.py",
    "test_film_structure_queue_ui.py",
    "test_launcher_navigation.py",
    "test_machine_process_preview.py",
    "test_machine_process_reload.py",
    "test_machine_settings_compact_chrome_ui.py",
    "test_main_compact_sidebar_ui.py",
    "test_mask_viewport.py",
    "test_material_library_routing.py",
    "test_material_optical_editor.py",
    "test_material_source_integration.py",
    "test_memory_lifecycle.py",
    "test_notification_sound.py",
    "test_optical_catalog_import.py",
    "test_optical_material_refinements.py",
    "test_process_compact_sidebar_ui.py",
    "test_process_editor_navigation_qt.py",
    "test_process_environment_ui.py",
    "test_process_optimization_modes.py",
    "test_preview_cursor.py",
    "test_queue_results_resources.py",
    "test_queued_pending_mask_design.py",
    "test_remaining_windows_compact_chrome_ui.py",
    "test_researched_material_review.py",
    "test_researched_material_ui.py",
    "test_saved_source_film_stack.py",
    "test_scheduler_main_integration.py",
    "test_settings_credentials.py",
    "test_font_size_defaults.py",
    "test_source_material_editor.py",
    "test_source_overwrite_confirmation.py",
    "test_source_refinements.py",
    "test_source_substrate_compact_chrome_ui.py",
    "test_taskbar_icons.py",
    "test_test_isolation.py",
    "test_utility_windows_compact_chrome_ui.py",
    "test_zns_reference_editor.py",
})

ALGORITHM_MODULES = frozenset({
    "test_film_resource_estimation.py",
    "test_film_optimization_continuation.py",
    "test_film_synthesis_warm_start_admission.py",
    "test_film_synthesis_deepseek_closeout.py",
    "test_film_synthesis_gpu_forward.py",
    "test_film_synthesis_cancel_jobs.py",
    "test_film_synthesis_cancel.py",
    "test_film_synthesis_parallel.py",
    "test_film_synthesis_parallel_review.py",
    "test_film_synthesis_parallel_benchmark.py",
    "test_film_synthesis_unknown_k_policy.py",
    "test_film_optimization_derivative_limits.py",
    "test_film_prepared_scoring.py",
    "test_film_backend_selection.py",
    "test_film_gpu_dispatch.py",
    "test_film_optimization_batch_job.py",
    "test_algorithm_paths.py",
    "test_cub_algorithm_review.py",
    "test_cub_interfaces.py",
    "test_cub_material_review.py",
    "test_film_automatic_substrate.py",
    "test_film_batch_de.py",
    "test_film_design.py",
    "test_film_design_reference_recipes.py",
    "test_film_design_service.py",
    "test_film_optics.py",
    "test_film_optics_batch.py",
    "test_film_optimization_cache_chunks.py",
    "test_film_optimization_forward_cache.py",
    "test_film_optimization_needle.py",
    "test_film_optimization_structure.py",
    "test_film_performance.py",
    "test_film_performance_full_result.py",
    "test_film_simulation_quick.py",
    "test_isolated_compute.py",
    "test_mask_designer.py",
    "test_film_structure_queue_ui.py",
    "test_film_structure_benchmarks.py",
    "test_film_structure_fixtures.py",
    "test_film_structure_preview.py",
    "test_finite_periodic_integration.py",
    "test_material_emission_models.py",
    "test_material_physics_reference.py",
    "test_pdf_layer_summary.py",
    "test_process_environment.py",
    "test_process_optimization_modes.py",
    "test_rii_formula.py",
    "test_sampling_diagnostics.py",
    "test_simulator.py",
    "test_zns_reference_snapshots.py",
    "test_zns_reference_integration_review.py",
})

CONTRACT_MODULES = frozenset({
    "test_film_continuation_editable_snapshot.py",
    "test_film_optimization_continuation.py",
    "test_film_optimization_continue_gui.py",
    "test_film_optimization_continue_qml.py",
    "test_film_synthesis_warm_start_admission.py",
    "test_film_synthesis_deepseek_closeout.py",
    "test_film_synthesis_deepseek_closeout_gui.py",
    "test_film_synthesis_gpu_forward_gui.py",
    "test_film_synthesis_gpu_forward.py",
    "test_film_optimization_cancel_spectrum.py",
    "test_film_optimization_cancel_native.py",
    "test_film_optimization_cancel_apply_review.py",
    "test_film_synthesis_cancel_jobs.py",
    "test_film_optimization_bounds_edit_review.py",
    "test_film_synthesis_cancel.py",
    "test_film_synthesis_parallel.py",
    "test_film_synthesis_parallel_review.py",
    "test_film_synthesis_parallel_benchmark.py",
    "test_film_synthesis_parallel_gui.py",
    "test_film_synthesis_unknown_k_policy.py",
    "test_film_synthesis_quick_controller.py",
    "test_film_synthesis_gui_review.py",
    "test_film_synthesis_gui_repairs.py",
    "test_film_physical_generator_usability.py",
    "test_film_synthesis_gui_usability_review.py",
    "test_film_synthesis_unknown_k_review.py",
    "test_film_synthesis_queue_gui.py",
    "test_film_synthesis_quick_adapter.py",
    "test_film_physical_generator_session.py",
    "test_film_physical_generator_frozen_apply.py",
    "test_assistant_message_body_qml.py",
    "test_assistant_message_lifecycle_qml.py",
    "test_assistant_compute_ui_authority.py",
    "test_assistant_discussion_ui.py",
    "test_project_editor_snapshot.py",
    "test_project_editor_context.py",
    "test_project_resource_editor_snapshot.py",
    "test_project_compute_editor_snapshot.py",
    "test_project_editor_operations.py",
    "test_project_editor_operations_context.py",
    "test_project_compute_editor_operations.py",
    "test_project_resource_editor_operations.py",
    "test_assistant_algorithm_alignment.py",
    "test_assistant_discussion_mode.py",
    "test_discussion_compute_policy.py",

    "test_project_named_resources.py",
    "test_project_film_save_efficiency.py",
    "test_launcher_information_global_resources.py",
    "test_responsive_operation_bar.py",
    "test_film_design_generator_names.py",

    "test_project_descriptor_boundary.py",
    "test_film_manual_draft_sync.py",
    "test_film_optimization_target_sampling.py",
    "test_film_optimization_remote_async_qt.py",
    "test_film_optimization_derivative_limits.py",
    "test_project_module_selector.py",
    "test_launcher_project_adapter.py",
    "test_launcher_project_manager_ui.py",
    "test_project_compute_controllers.py",
    "test_project_material_qml_binding.py",
    "test_mask_optimization_ui.py",
    "test_project_package.py",
    "test_project_material_catalog.py",
    "test_film_simulation_toolbar.py",
    "test_application_process_smoke.py",
    "test_assistant_managed_windows.py",
    "test_project_application_client.py",
    "test_project_application_lifecycle.py",
    "test_project_recovery_coordinator.py",
    "test_project_management_mcp.py",
    "test_assistant_process.py",
    "test_project_management.py",
    "test_project_recovery.py",
    "test_plugins.py",
    "test_tfcalc_import.py",
    "test_tfcalc_import_session.py",
    "test_film_optimization_batch_job.py",
    "test_assistant_lifecycle_contract.py",
    "test_film_optimization_run_contract.py",
    "test_settings_coordinator_contract.py",
    "test_design_test_fixtures.py",
    "test_numeric_format.py",
    "test_ai_appearance_operations_contract.py",
    "test_ai_film_operations_contract.py",
    "test_ai_provider_protocol_contract.py",
    "test_ai_settings_probe_contract.py",
    "test_atomic_io.py",
    "test_appearance_asset_descriptor.py",
    "test_appearance_cancel_publication.py",
    "test_appearance_circle.py",
    "test_appearance_parallel.py",
    "test_offline_budget_entrypoints.py",
    "test_appearance_progress.py",
    "test_appearance_progress_math.py",
    "test_appearance_compute_entrypoints.py",
    "test_appearance_result_contract.py",
    "test_assistant_typed_operations_contract.py",
    "test_capability_effect_stage.py",
    "test_capability_queue_route.py",
    "test_capability_schema_cache.py",
    "test_compute_confirmation_policy.py",
    "test_compute_start_failures.py",
    "test_cub_machine_runtime_regressions.py",
    "test_film_compute_entrypoints.py",
    "test_film_design_generator_session.py",
    "test_film_design_mcp.py",
    "test_film_design_reference_bridge.py",
    "test_film_design_reference_recipes.py",
    "test_film_design_service.py",
    "test_film_design_store.py",
    "test_film_design_templates.py",
    "test_film_expression_store.py",
    "test_film_evaluation_executor.py",
    "test_film_structure_payload_review.py",
    "test_film_structure_request_review.py",
    "test_ideal_air_optical_contract.py",
    "test_immutable_queued_inputs.py",
    "test_input_snapshot.py",
    "test_isolated_compute.py",
    "test_material_asset_snapshots.py",
    "test_mcp_portable_inputs.py",
    "test_pdf_layer_summary.py",
    "test_portable_asset_bundle.py",
    "test_process_editor_navigation_static.py",
    "test_process_environment.py",
    "test_queue_jobs_page.py",
    "test_queue_fifo_review.py",
    "test_queue_submission_isolation.py",
    "test_researched_material_catalog.py",
    "test_scheduler_main_integration.py",
    "test_scheduler_service.py",
    "test_settings_credentials.py",
    "test_settings_model_features.py",
    "test_static_analysis_runner.py",
    "test_test_isolation.py",
    "test_theme_controls_static.py",
    "test_typed_design_extensions.py",
    "test_zns_reference_snapshots.py",
    "test_zns_reference_integration_review.py",
})

TIER_MANIFEST = {
    "quick": frozenset({
        "test_atomic_io.py",
        "test_film_design_generator_session.py",
        "test_film_design_store.py",
        "test_film_design_templates.py",
        "test_film_expression_store.py",
        "test_process_editor_navigation_static.py",
        "test_queue_jobs_page.py",
        "test_test_entrypoints.py",
        "test_theme_controls_static.py",
    }),
    "algorithm": ALGORITHM_MODULES,
    "contract": CONTRACT_MODULES,
    "qt": QT_MODULES,
}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("deposition-sim safety")
    group.addoption(
        "--run-gpu",
        action="store_true",
        default=False,
        help="allow explicitly marked tests that require real GPU hardware",
    )
    group.addoption(
        "--run-provider",
        action="store_true",
        default=False,
        help="allow explicitly marked tests that call a live model provider",
    )
    group.addoption(
        "--run-remote",
        action="store_true",
        default=False,
        help="allow explicitly marked tests that call a live remote service",
    )
    group.addoption(
        "--collection-report-json",
        action="store",
        default=None,
        metavar="PATH",
        help="write collected nodeids, tier coverage, and AST counts as JSON",
    )


def _loopback_host(host: object) -> bool:
    if host is None:
        return True
    value = str(host).strip().strip("[]").lower().rstrip(".")
    if value in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _is_unix_socket(sock: socket.socket) -> bool:
    """Return whether ``sock`` is AF_UNIX, when the platform exposes it."""
    unix_family = getattr(socket, "AF_UNIX", None)
    return unix_family is not None and sock.family == unix_family


def _install_default_network_guard() -> None:
    """Block outbound Python sockets while preserving local TCP and AF_UNIX."""
    global _ORIGINAL_SOCKET_METHODS
    if getattr(socket, "_deposition_test_guard", False):
        return

    original_getaddrinfo = socket.getaddrinfo
    original_gethostbyname = socket.gethostbyname
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto
    original_create_connection = socket.create_connection
    _ORIGINAL_SOCKET_METHODS = (
        original_getaddrinfo,
        original_gethostbyname,
        original_connect,
        original_connect_ex,
        original_sendto,
        original_create_connection,
    )
    socket._deposition_test_guard_originals = _ORIGINAL_SOCKET_METHODS

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _loopback_host(host):
            raise OSError(f"test network guard blocked name lookup for {host!r}")
        return original_getaddrinfo(host, *args, **kwargs)

    def guarded_gethostbyname(host):
        if not _loopback_host(host):
            raise OSError(f"test network guard blocked name lookup for {host!r}")
        return original_gethostbyname(host)

    def guarded_connect(sock, address):
        if not _is_unix_socket(sock):
            host = address[0] if isinstance(address, tuple) and address else address
            if not _loopback_host(host):
                raise OSError(f"test network guard blocked connection to {host!r}")
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if not _is_unix_socket(sock):
            host = address[0] if isinstance(address, tuple) and address else address
            if not _loopback_host(host):
                raise OSError(f"test network guard blocked connection to {host!r}")
        return original_connect_ex(sock, address)

    def guarded_sendto(sock, data, *args):
        if not _is_unix_socket(sock):
            address = args[-1] if args else None
            host = address[0] if isinstance(address, tuple) and address else address
            if not _loopback_host(host):
                raise OSError(f"test network guard blocked datagram to {host!r}")
        return original_sendto(sock, data, *args)

    def guarded_create_connection(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else address
        if not _loopback_host(host):
            raise OSError(f"test network guard blocked connection to {host!r}")
        return original_create_connection(address, *args, **kwargs)

    socket.getaddrinfo = guarded_getaddrinfo
    socket.gethostbyname = guarded_gethostbyname
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.socket.sendto = guarded_sendto
    socket.create_connection = guarded_create_connection
    socket._deposition_test_guard = True


def _remove_default_network_guard_for_test_body() -> None:
    global _ORIGINAL_SOCKET_METHODS
    if not getattr(socket, "_deposition_test_guard", False):
        return
    original_methods = _ORIGINAL_SOCKET_METHODS or getattr(
        socket, "_deposition_test_guard_originals", None
    )
    if original_methods is None:
        raise RuntimeError("test network guard cannot restore original socket methods")
    (getaddrinfo, gethostbyname, connect, connect_ex, sendto, create_connection) = original_methods
    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    socket.create_connection = create_connection
    socket._deposition_test_guard = False


def _ensure_test_qt_application() -> None:
    """Retain one widget-capable application before test-module imports.

    Qt exposes the same singleton through all application subclasses and
    cannot upgrade a QCoreApplication/QGuiApplication to QApplication later.
    The project GUI suites share a process, so the strongest class must win
    independently of collection order. Core-only subsets can still collect
    when the optional GUI dependency is absent.
    """
    global _TEST_QT_APPLICATION
    try:
        from PySide6.QtWidgets import QApplication
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6":
            return
        raise

    # Establish private QSettings paths before constructing the application.
    from tests import qt_test_guard  # noqa: F401

    application = QApplication.instance()
    if application is not None and not isinstance(application, QApplication):
        raise pytest.UsageError(
            "Qt tests require QApplication before collection; an existing "
            f"{type(application).__name__} cannot be safely upgraded"
        )
    _TEST_QT_APPLICATION = application or QApplication([])
    from coat_sim.ui.fonts import bundled_font_family

    if not bundled_font_family():
        raise pytest.UsageError("Qt tests require the bundled Noto CJK application font")
    from coat_sim.ui.input_focus import install_input_focus_policy

    install_input_focus_policy()


def pytest_configure(config: pytest.Config) -> None:
    # Establish deterministic headless/cache settings before importing tests.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    # Match configure_qt_dpi_scale() before any QML Controls are imported.
    if not os.environ.get("QT_QUICK_CONTROLS_STYLE"):
        os.environ["QT_QUICK_CONTROLS_STYLE"] = "Fusion"
    os.environ.setdefault("QSG_RHI_BACKEND", "software")
    mpl_config = _TEST_CACHE_ROOT / "matplotlib"
    xdg_cache = _TEST_CACHE_ROOT / "xdg"
    mpl_config.mkdir(parents=True, exist_ok=True)
    xdg_cache.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(mpl_config)
    os.environ["XDG_CACHE_HOME"] = str(xdg_cache)

    # Collection and module imports always stay offline. Live access is opened
    # only for a matching marked test body and is restored immediately after.
    _install_default_network_guard()
    _ensure_test_qt_application()


@pytest.fixture(autouse=True)
def _restore_qt_settings_isolation():
    """Keep Qt's process-global settings paths isolated between test bodies."""
    guard = (sys.modules.get("tests.qt_test_guard")
             or sys.modules.get("qt_test_guard"))
    if guard is None:
        yield
        return

    guard.restore_qt_test_settings()
    try:
        yield
    finally:
        guard.restore_qt_test_settings()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    root = Path(config.rootpath)
    tests_root = root / "tests"
    test_paths = {
        path.resolve()
        for pattern in ("test_*.py", "*_test.py")
        for path in tests_root.rglob(pattern)
    }
    test_files = {path.name for path in test_paths}
    requested_paths = [Path(arg) for arg in config.args if arg]
    full_tests_directory_requested = not requested_paths or any(
        path.resolve() == tests_root.resolve() for path in requested_paths
    )
    manifest_files = set().union(*TIER_MANIFEST.values())
    unknown_manifest_files = manifest_files - test_files
    if unknown_manifest_files:
        raise pytest.UsageError(
            "test tier manifest points to missing test files: "
            + ", ".join(sorted(unknown_manifest_files))
        )

    entries: list[dict[str, object]] = []
    for item in items:
        item_path = Path(str(item.path)).resolve()
        if item_path not in test_paths:
            raise pytest.UsageError(
                f"collected test module is outside the configured test tree: {item_path}"
            )
        filename = Path(str(item.path)).name
        tiers = {tier for tier, modules in TIER_MANIFEST.items() if filename in modules}
        if not tiers:
            tiers.add("quick")
        for tier in tiers:
            item.add_marker(getattr(pytest.mark, tier))
        if not any(item.get_closest_marker(tier) is not None for tier in TIER_MANIFEST):
            raise pytest.UsageError(f"collected test item has no tier: {item.nodeid}")
        entries.append({
            "nodeid": item.nodeid,
            "module": item_path.relative_to(tests_root.resolve()).as_posix(),
            "tiers": sorted(tiers),
            "parameterized": hasattr(item, "callspec"),
            "kind": "unittest" if "unittest" in type(item).__module__ else "pytest",
        })

    nodeids = [str(entry["nodeid"]) for entry in entries]
    if len(nodeids) != len(set(nodeids)):
        raise pytest.UsageError("duplicate pytest nodeids were collected")

    config._deposition_test_collection = {
        "test_paths": test_paths,
        "entries": entries,
        "full_tests_directory_requested": full_tests_directory_requested,
    }


def pytest_collection_finish(session: pytest.Session) -> None:
    report_path = session.config.getoption("--collection-report-json")
    collection = getattr(session.config, "_deposition_test_collection", None)
    if collection is None:
        if report_path:
            raise pytest.UsageError("collection audit requested but no test items were recorded")
        return
    entries = collection["entries"]
    test_paths = collection["test_paths"]
    full_tests_directory_requested = collection["full_tests_directory_requested"]
    tests_root = Path(session.config.rootpath) / "tests"
    collected_modules = {str(entry["module"]) for entry in entries}
    disk_modules = {
        path.resolve().relative_to(tests_root.resolve()).as_posix()
        for path in test_paths
    }
    missing_modules = sorted(disk_modules - collected_modules)
    terminal_reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    collection_errors = len(terminal_reporter.stats.get("error", [])) if terminal_reporter else 0
    if not report_path:
        if full_tests_directory_requested and missing_modules and not collection_errors:
            raise pytest.UsageError(
                "test modules contain no collected items: " + ", ".join(missing_modules)
            )
        return

    definition_counts = {
        "module_level_functions": 0,
        "class_methods": 0,
        "nested_functions": 0,
    }
    unittest_hooks = []

    class DefinitionCounter(ast.NodeVisitor):
        def __init__(self) -> None:
            self.class_depth = 0
            self.function_depth = 0

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.class_depth += 1
            self.generic_visit(node)
            self.class_depth -= 1

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            if node.name.startswith("test_"):
                if self.function_depth:
                    definition_counts["nested_functions"] += 1
                elif self.class_depth:
                    definition_counts["class_methods"] += 1
                else:
                    definition_counts["module_level_functions"] += 1
            self.function_depth += 1
            self.generic_visit(node)
            self.function_depth -= 1

    for path in sorted(test_paths):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "load_tests":
                bridge_count = sum(
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name.startswith("test_")
                    for child in tree.body
                )
                unittest_hooks.append({
                    "module": path.resolve().relative_to(tests_root.resolve()).as_posix(),
                    "top_level_test_functions": bridge_count,
                })
        DefinitionCounter().visit(tree)

    nodeids = [str(entry["nodeid"]) for entry in entries]
    tier_nodeids = {
        tier: {
            str(entry["nodeid"])
            for entry in entries
            if tier in entry["tiers"]
        }
        for tier in TIER_MANIFEST
    }
    tier_union = set().union(*tier_nodeids.values()) if tier_nodeids else set()
    report = {
        "collected_item_count": len(entries),
        "collected_module_count": len(collected_modules),
        "test_module_file_count": len(disk_modules),
        "modules_without_collected_items": sorted(disk_modules - collected_modules),
        "collection_error_count": collection_errors,
        "module_level_test_function_definitions": definition_counts["module_level_functions"],
        "class_test_method_definitions": definition_counts["class_methods"],
        "nested_test_function_definitions": definition_counts["nested_functions"],
        "parameterized_item_count": sum(bool(entry["parameterized"]) for entry in entries),
        "pytest_item_count": sum(entry["kind"] == "pytest" for entry in entries),
        "unittest_item_count": sum(entry["kind"] == "unittest" for entry in entries),
        "unittest_load_tests_hooks": unittest_hooks,
        "tier_item_counts": {tier: len(ids) for tier, ids in tier_nodeids.items()},
        "tier_union_covers_collected_nodeids": tier_union == set(nodeids),
        "duplicate_nodeid_count": len(nodeids) - len(set(nodeids)),
        "nodeids": nodeids,
        "items": entries,
    }
    destination = Path(report_path)
    if not destination.is_absolute():
        destination = Path(session.config.rootpath) / destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if full_tests_directory_requested and missing_modules and not collection_errors:
        raise pytest.UsageError(
            "test modules contain no collected items: " + ", ".join(missing_modules)
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    """Permit marked live requests only inside explicitly enabled test bodies."""
    allow_live_network = (
        (item.get_closest_marker("live_provider") is not None
         and item.config.getoption("--run-provider"))
        or (item.get_closest_marker("live_remote") is not None
            and item.config.getoption("--run-remote"))
    )
    if allow_live_network:
        _remove_default_network_guard_for_test_body()
    try:
        yield
    finally:
        if allow_live_network:
            _install_default_network_guard()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    opt_ins = {
        "real_gpu": "--run-gpu",
        "live_provider": "--run-provider",
        "live_remote": "--run-remote",
    }
    for marker, option in opt_ins.items():
        if item.get_closest_marker(marker) is not None and not item.config.getoption(option):
            pytest.skip(f"requires explicit opt-in: {marker}")
