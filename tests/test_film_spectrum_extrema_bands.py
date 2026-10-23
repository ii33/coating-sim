"""Discrete extrema are restricted to explicitly configured sample bands."""

import math

import pytest

from coat_sim.core.film_spectrum_analysis import (
    calculate_spectrum_analysis,
    calculate_spectrum_extrema,
)


def sample_result():
    return {
        "settings": {"polarization": "unpolarized", "angle_deg": 12.5},
        "wavelength_nm": [400.0, 500.0, 550.0, 600.0, 650.0, 700.0, 800.0],
        "R": [0.2, 0.8, 0.8, 0.1, 0.5, 0.1, 0.9],
        "T": [0.8, 0.2, 0.2, 0.9, 0.5, 0.9, 0.1],
    }


def test_band_extrema_reuse_plateau_rules_with_only_in_band_samples():
    stats = calculate_spectrum_extrema(sample_result(), "R", "unpolarized", 400, 700)

    assert stats["status"] == "ok"
    assert stats["sample_count"] == 6
    assert (stats["sample_minimum_nm"], stats["sample_maximum_nm"]) == (400, 700)
    assert [(point["wavelength_min_nm"], point["wavelength_max_nm"])
            for point in stats["global_min"]] == [(600, 600), (700, 700)]
    assert [(point["wavelength_min_nm"], point["wavelength_max_nm"])
            for point in stats["global_max"]] == [(500, 550)]
    assert [(point["wavelength_min_nm"], point["wavelength_max_nm"])
            for point in stats["local_peaks"]] == [(500, 550), (650, 650)]
    assert [(point["wavelength_min_nm"], point["wavelength_max_nm"])
            for point in stats["local_valleys"]] == [(600, 600)]
    assert stats["local_peaks"][0]["wavelength_nm"] == 525
    assert stats["angle_deg"] == 12.5


def test_overlapping_bands_are_independent_and_band_endpoints_are_not_local_extrema():
    result = sample_result()
    left = calculate_spectrum_extrema(result, "R", "unpolarized", 400, 700)
    overlap = calculate_spectrum_extrema(result, "R", "unpolarized", 500, 800)

    assert left["global_max"][0]["value"] == 0.8
    assert overlap["global_max"][0]["value"] == 0.9
    assert (overlap["sample_minimum_nm"], overlap["sample_maximum_nm"]) == (500, 800)
    assert [(point["wavelength_nm"], point["value"])
            for point in overlap["local_peaks"]] == [(650, 0.5)]
    assert [(point["wavelength_nm"], point["value"])
            for point in overlap["local_valleys"]] == [(600, 0.1), (700, 0.1)]


def test_only_original_samples_are_selected_without_boundary_interpolation():
    result = sample_result()
    one_point = calculate_spectrum_extrema(result, "R", "unpolarized", 450, 520)
    empty = calculate_spectrum_extrema(result, "R", "unpolarized", 451, 499)
    two_points = calculate_spectrum_extrema(result, "R", "unpolarized", 450, 560)

    assert one_point["status"] == "insufficient_samples"
    assert one_point["sample_count"] == 1
    assert one_point["sample_minimum_nm"] == one_point["sample_maximum_nm"] == 500
    assert empty["status"] == "no_samples"
    assert empty["sample_count"] == 0
    assert two_points["status"] == "ok"
    assert two_points["sample_count"] == 2
    assert (two_points["sample_minimum_nm"], two_points["sample_maximum_nm"]) == (500, 550)
    assert two_points["local_peaks"] == []


@pytest.mark.parametrize(
    "minimum,maximum,expected",
    [(None, 700, "incomplete_range"), (400, None, "incomplete_range"),
     (700, 700, "invalid_range"), (701, 700, "invalid_range"),
     (math.nan, 700, "invalid_range")],
)
def test_invalid_or_incomplete_bands_are_explicit(minimum, maximum, expected):
    assert calculate_spectrum_extrema(
        sample_result(), "R", "unpolarized", minimum, maximum)["status"] == expected


def test_full_spectrum_analysis_can_skip_extrema_while_preserving_threshold_intervals(monkeypatch):
    def extrema_not_requested(*_args, **_kwargs):
        raise AssertionError("full-spectrum extrema should not be calculated")

    monkeypatch.setattr("coat_sim.core.film_spectrum_analysis._group_equal_runs", extrema_not_requested)
    stats = calculate_spectrum_analysis(sample_result(), "T", "unpolarized", 0.5,
                                        include_extrema=False)

    assert stats["status"] == "ok"
    assert stats["global_min"] == []
    assert stats["global_max"] == []
    assert stats["local_peaks"] == []
    assert stats["local_valleys"] == []
    assert stats["long_wave_pass_intervals"]


def test_extrema_omission_does_not_change_existing_full_domain_pass_intervals():
    result = sample_result()
    full = calculate_spectrum_analysis(result, "T", "unpolarized", 0.5)
    intervals_only = calculate_spectrum_analysis(
        result, "T", "unpolarized", 0.5, include_extrema=False)

    assert intervals_only["long_wave_pass_intervals"] == full["long_wave_pass_intervals"]
    assert intervals_only["short_wave_pass_intervals"] == full["short_wave_pass_intervals"]
    assert intervals_only["threshold_fraction"] == full["threshold_fraction"]
