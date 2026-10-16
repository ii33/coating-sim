"""Offline regression for the mask-worker best-candidate checkpoint import."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coat_sim.core.api import CoatService
from coat_sim.core.simulator import Settings

pytestmark = pytest.mark.contract


@dataclass
class _SimulationField:
    shape: tuple[int, int] = (2, 3)


def test_mask_worker_writes_best_candidate_checkpoint_with_real_capability_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path.resolve()
    checkpoint = root / "artifacts/runtime/scheduler/jobs/job-import-regression/best-candidate.json"
    process_path = {
        "process_path": "synthetic:process.json",
        "files": [{"kind": "process", "path": "synthetic:process.json"}],
    }
    original_profile = {"outer": [[0, 0], [1, 0], [0, 1]]}
    candidate_profile = {"outer": [[0, 0], [2, 0], [0, 2]]}
    runtime_data = {
        "machine": {"masks": [{"profile": original_profile}]},
        "profile": {"steps": []},
    }
    document = SimpleNamespace(path=str(root / "process.json"), issues=[], runtime=runtime_data,
                               data={"steps": []})
    service = object.__new__(CoatService)
    service.root = root
    service.restrict_to_root = True
    service._settings = lambda value: value
    service.load = lambda _kind, _path: document
    service.mask_optimization_options = lambda *_args, **_kwargs: {
        "mode": "auto_design",
        "variable_mode": "auto_design",
        "objective": {"kind": "uniformity"},
        "source_groups": [{"source_slot": 1, "mask_slots": [0]}],
        "mask_bounds": [{"slot": 0}],
        "mask_slots": [0],
        "new_mask_slots": [],
        "max_evaluations": 1,
        "search_grid_size": 2,
        "backend": "CPU",
        "fast_cycle": False,
        "fast_cycle_residual_percent": 5.0,
        "constraints": {},
    }
    service.prepare_simulation_asset = lambda **kwargs: {
        "temporary_bundle_path": str(root / "bundles" / f"{kwargs['run_id']}.coatasset"),
        "directory": str(root / "bundles"),
        "result_file": str(root / "results" / f"{kwargs['run_id']}.npz"),
        "files": [],
        "asset": {"run_id": kwargs["run_id"]},
    }
    service.load_simulation_asset = lambda _path: {
        "run_id": Path(_path).stem,
        "input_snapshot": {"files": [{"kind": "process", "path": "synthetic:process.json"}]},
    }

    simulation = {"field_nm": _SimulationField()}
    optimized = SimpleNamespace(
        report={
            "search_evaluations": 1,
            "selected_simulation_elapsed_s": 0.01,
            "original_simulation_elapsed_s": 0.01,
            "search_best_masks": [{"slot": 0, "profile": candidate_profile}],
            "selected_search": {},
            "original_boards_search": {},
            "baseline_without_selected_boards": None,
            "verification_elapsed_s": 0.01,
            "verification_evaluations": 1,
            "verification_simulations": 1,
            "constraints": {},
        },
        original_simulation=simulation,
        candidate_masks=[{"slot": 0, "profile": candidate_profile}],
        simulation=simulation,
    )

    import coat_sim.core.input_snapshot as input_snapshot
    import coat_sim.core.mask_optimization as mask_optimization
    import coat_sim.core.mask_optimization_workflow as mask_workflow
    import coat_sim.core.process_simulator as process_simulator
    import coat_sim.core.result_summary as result_summary
    import coat_sim.core.simulation_assets as simulation_assets

    monkeypatch.setattr(mask_optimization, "optimize_correction_boards", lambda *_args, **_kwargs: optimized)
    monkeypatch.setattr(mask_optimization, "_quality", lambda *_args, **_kwargs: {
        "score": 0.5, "cv_percent": 1.0,
    })
    monkeypatch.setattr(mask_workflow, "prepare_optimization_runtime", lambda *_args, **_kwargs: ({
        "machine": {"masks": [{"profile": original_profile}]},
        "profile": {"steps": []},
    }, None))
    monkeypatch.setattr(process_simulator, "simulate_process", lambda *_args, **_kwargs: simulation)
    monkeypatch.setattr(input_snapshot, "candidate_snapshot", lambda *_args, **_kwargs: {
        "process_path": "synthetic:process.json",
        "files": [{"kind": "process", "path": "synthetic:process.json"}],
    })
    monkeypatch.setattr(result_summary, "compact_result_metadata", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(simulation_assets, "plain", lambda value: value)

    service._optimize_masks_worker(
        Settings(grid_size=2), process_path, pending_only=True, checkpoint_path=checkpoint,
    )

    assert checkpoint.is_file(), "verified candidate should be atomically checkpointed"
    envelope = json.loads(checkpoint.read_text(encoding="utf-8"))
    candidate = envelope["result"]
    assert candidate["partial_result"]["kind"] == "best_candidate"
    assert candidate["partial_result"]["candidate_verified"] is True
    assert candidate["field_shape"] == [2, 3]
    assert candidate["pending_masks"] == [{"slot": 0, "profile": candidate_profile}]

