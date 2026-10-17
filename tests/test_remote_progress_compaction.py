"""Progress keeps bounded scalar ETA/device identity without payload arrays."""

from coat_sim.core.compute_tasks import _compact_progress


def test_film_backend_identity_survives_as_bounded_scalars_only():
    value = _compact_progress(
        {
            "actual_backend": "gpu",
            "gpu_runtime": "xpu",
            "device_uuid": "8680677d-0600-0000-0002-000000000000",
            "device": "Intel" * 100,
            "dtype": "float64/complex128",
            "output_device": "xpu:0",
            "requested_backend": "xpu",
            "spectrum": [1.0, 2.0],
            "execution_device": {"kind": "ignored"},
        }
    )
    assert value["gpu_runtime"] == "xpu" and value["output_device"] == "xpu:0"
    assert len(value["device"]) == 240
    assert "spectrum" not in value and "execution_device" not in value


def test_appearance_wavelength_progress_and_eta_survive_queue_compaction():
    value = _compact_progress(
        {
            "stage": "rendering",
            "completed_wavelengths": 3,
            "total_wavelengths": 9,
            "fraction": 1 / 3,
            "elapsed_s": 12.5,
            "estimated_remaining_s": 25.0,
            "estimate_kind": "measured",
            "ray_paths": [1, 2, 3],
        }
    )
    assert value == {
        "stage": "rendering",
        "completed_wavelengths": 3,
        "total_wavelengths": 9,
        "fraction": 1 / 3,
        "elapsed_s": 12.5,
        "estimated_remaining_s": 25.0,
        "estimate_kind": "measured",
    }
