"""Remote appearance progress keeps bounded scalar ETA without payload arrays."""

from coat_sim.core.compute_tasks import _compact_progress


def test_appearance_wavelength_progress_and_eta_survive_queue_compaction():
    value = _compact_progress({
        "stage": "rendering", "completed_wavelengths": 3,
        "total_wavelengths": 9, "fraction": 1 / 3,
        "elapsed_s": 12.5, "estimated_remaining_s": 25.0,
        "estimate_kind": "measured", "ray_paths": [1, 2, 3],
    })
    assert value == {
        "stage": "rendering", "completed_wavelengths": 3,
        "total_wavelengths": 9, "fraction": 1 / 3,
        "elapsed_s": 12.5, "estimated_remaining_s": 25.0,
        "estimate_kind": "measured",
    }
