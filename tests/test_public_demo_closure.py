"""Synthetic public examples resolve without private user assets."""
from pathlib import Path

from coat_sim.core.process_profiles import read_process_profile, resolve_process_profile


def test_public_demo_resolves_typed_standalone_dependencies():
    root = Path(__file__).resolve().parents[1]
    path = root / "data/processes/kanban_demo_process.cproc"
    runtime, errors = resolve_process_profile(read_process_profile(path), path.parent)
    assert errors == []
    assert len(runtime["machine"]["sources"]) == 1
    assert runtime["machine"]["masks"] == []
    assert all(step["source_slot"] == 1 and step["mask_slots"] == []
               for step in runtime["steps"])
