"""Offline regressions for binding identity at the real runpy worker entry."""
from __future__ import annotations

import asyncio
import io
import runpy
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coat_sim.agent import runtime, worker


def entry_writer(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run main exactly as the launcher does; intercept before starting stdin IO."""
    captured = []

    def capture_host(coroutine: Any) -> None:
        captured.append(coroutine.cr_frame.f_locals['writer'])
        coroutine.close()

    with monkeypatch.context() as patch:
        for name in ('stdin', 'stdout', 'stderr'):
            patch.setattr(sys, name, io.StringIO())
        patch.setattr(asyncio, 'run', capture_host)
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore', message=".*found in sys.modules.*", category=RuntimeWarning)
            runpy.run_module('coat_sim.agent.worker', run_name='__main__', alter_sys=True)
    assert len(captured) == 1
    return captured[0]


def check(binding: Any) -> None:
    asyncio.run(runtime._check_project_binding(binding))


def test_runpy_entry_global_binding_needs_no_open_project(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = entry_writer(monkeypatch)
    binding = writer.project_binding_snapshot(None)

    def no_registry_lookup() -> Any:
        pytest.fail('Intentional global scope must not discover or require an open project.')

    binding._clients = no_registry_lookup
    assert binding.verify()
    check(binding)
    assert isinstance(binding, worker.ProjectMemoryBinding)
    assert binding.memory_project_id == '' and binding.providers() == {}
    assert not writer.configure_project_target(None)
    check(binding)


def test_runpy_entry_keeps_project_revocation_and_scope_epochs(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    writer = entry_writer(monkeypatch)
    initial_global = writer.project_binding_snapshot(None)
    selected = {'client_id': 'owner', 'client_generation': 'owner-1', 'project_id': 'project-a',
                'session_generation': 'session-1', 'root': str(tmp_path),
                'project_file': str(tmp_path / 'a.coatproj')}
    writer.configure_project_target(selected)
    with pytest.raises(ValueError, match='stale_project_binding'):
        check(initial_global)
    observed = [{'client_id': 'owner', 'generation': 'owner-1', 'state': 'alive',
                 'project_sessions': [{'project_id': 'project-a', 'generation': 'session-1',
                                       'root': selected['root'], 'project_file': selected['project_file']}]}]
    binding = writer.project_binding_snapshot(selected)
    binding._clients = lambda: observed
    check(binding)
    saved = list(observed)
    observed.clear()
    with pytest.raises(ValueError, match='stale_project_binding'):
        check(binding)
    observed[:] = saved
    with pytest.raises(ValueError, match='stale_project_binding'):
        check(binding)
    writer.configure_project_target(None)
    fresh_global = writer.project_binding_snapshot(None)
    check(fresh_global)
    writer.configure_project_target(selected)
    writer.configure_project_target(None)
    with pytest.raises(ValueError, match='stale_project_binding'):
        check(fresh_global)
    with pytest.raises(ValueError, match='stale_project_binding'):
        writer.project_binding_snapshot(selected)
    check(writer.project_binding_snapshot(None))


def test_binding_guard_rejects_duck_typed_or_model_provided_binding() -> None:
    for forged in (SimpleNamespace(verify=lambda: True, memory_project_id=''),
                   {'verify': True, 'assistant_project_target': None}):
        with pytest.raises(ValueError, match='stale_project_binding'):
            check(forged)
