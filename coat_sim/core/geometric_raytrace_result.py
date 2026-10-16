"""Strict validators for persisted geometric ray-trace results and queue summaries."""
from __future__ import annotations

import math
from typing import Any


RESULT_SCHEMA = "coat_sim.geometric_raytrace_result"
SCHEMA_VERSION = 1
_MODEL = "ordered_meridional_geometric_rays_local_coherent_coatings"
_DETECTOR_INTERPRETATION = "geometric ray-energy histogram; not a diffraction PSF"
_MAX_RESULT_BYTES = 512 * 1024 * 1024
_SPECTRUM_FIELDS = {
    "wavelength_nm", "spectrum_weight", "incident_weight", "transmitted_weight",
    "reflected_weight", "absorbed_weight", "missed_weight", "transmittance",
    "reflectance", "absorptance", "missed_fraction",
}
_DETECTOR_SPECTRUM_FIELDS = {
    "wavelength_nm", "spectrum_weight", "detected_weight", "missed_weight",
    "ray_energy_histogram_weight",
}
_PATH_FIELDS = {
    "sample_id", "wavelength_nm", "sample_weight", "spectrum_weight", "status",
    "events", "final_ray", "powers",
}
_DETECTOR_PATH_FIELDS = {
    "detector_hit_x_mm", "detector_status", "detector_captured", "detector_weight",
}
_POWER_FIELDS = {
    "incident_weight", "transmitted_weight", "reflected_weight", "absorbed_weight",
    "missed_weight",
}
_PATH_STATUSES = {
    "transmitted", "no_forward_intersection", "outside_aperture",
    "wrong_incident_side", "outside_thickness_field", "total_internal_reflection",
    "nonforward_transmission", "no_transmitted_power",
}


def _invalid(reason: str) -> None:
    raise ValueError(f"invalid_geometric_raytrace_result: {reason}")


def _number(value: Any, *, minimum: float | None = None) -> bool:
    return (type(value) in (int, float) and math.isfinite(float(value))
            and (minimum is None or value >= minimum))


def _close(left: float, right: float, *, relative: float = 1e-8) -> bool:
    return abs(left - right) <= relative * max(1.0, abs(left), abs(right))


def _pair(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and all(_number(x) for x in value)


def _check_keys(value: Any, fields: set[str], name: str) -> None:
    if not isinstance(value, dict) or set(value) != fields:
        _invalid(f"{name}_fields")


def _check_nonnegative_numbers(row: dict, names: set[str], name: str) -> None:
    if any(not _number(row.get(field), minimum=0.0) for field in names):
        _invalid(f"{name}_numeric_fields")


def _validate_spectrum_rows(rows: Any) -> list[dict]:
    if not isinstance(rows, list) or not 1 <= len(rows) <= 512:
        _invalid("spectra_count")
    seen_wavelengths = set()
    for row in rows:
        _check_keys(row, _SPECTRUM_FIELDS, "spectrum")
        if (not _number(row["wavelength_nm"], minimum=0.0)
                or row["wavelength_nm"] == 0
                or not _number(row["spectrum_weight"], minimum=0.0)):
            _invalid("spectrum_identity")
        wavelength = float(row["wavelength_nm"])
        if wavelength in seen_wavelengths:
            _invalid("duplicate_spectrum_wavelength")
        seen_wavelengths.add(wavelength)
        _check_nonnegative_numbers(row, _POWER_FIELDS, "spectrum")
        for key in ("transmittance", "reflectance", "absorptance", "missed_fraction"):
            if not _number(row[key], minimum=0.0) or row[key] > 1.0 + 1e-8:
                _invalid("spectrum_fraction")
        accounted = sum(float(row[key]) for key in (
            "transmitted_weight", "reflected_weight", "absorbed_weight", "missed_weight"))
        if not _close(float(row["incident_weight"]), accounted):
            _invalid("spectrum_energy_balance")
        incident = float(row["incident_weight"])
        for fraction, weight in (("transmittance", "transmitted_weight"),
                                 ("reflectance", "reflected_weight"),
                                 ("absorptance", "absorbed_weight"),
                                 ("missed_fraction", "missed_weight")):
            expected = float(row[weight]) / incident if incident else 0.0
            if not _close(float(row[fraction]), expected):
                _invalid("spectrum_fraction_balance")
    return rows


def _validate_detector_spectrum_rows(rows: Any, wavelengths: list[dict],
                                     pixel_count: int | None = None) -> list[dict]:
    if not isinstance(rows, list) or len(rows) != len(wavelengths):
        _invalid("detector_spectra_count")
    for row, spectrum in zip(rows, wavelengths):
        _check_keys(row, _DETECTOR_SPECTRUM_FIELDS, "detector_spectrum")
        for key in ("wavelength_nm", "spectrum_weight"):
            if not _number(row[key], minimum=0.0) or not _close(
                    float(row[key]), float(spectrum[key])):
                _invalid("detector_spectrum_identity")
        _check_nonnegative_numbers(row, {"detected_weight", "missed_weight"},
                                   "detector_spectrum")
        histogram = row["ray_energy_histogram_weight"]
        if (not isinstance(histogram, list) or not histogram
                or (pixel_count is not None and len(histogram) != pixel_count)
                or any(not _number(value, minimum=0.0) for value in histogram)):
            _invalid("detector_histogram")
        if not _close(sum(float(value) for value in histogram),
                      float(row["detected_weight"]), relative=1e-10):
            _invalid("detector_histogram_balance")
    return rows


def _validate_rta(value: Any, name: str) -> None:
    _check_keys(value, {"R", "T", "A"}, name)
    _check_nonnegative_numbers(value, {"R", "T", "A"}, name)
    if not _close(sum(float(value[key]) for key in ("R", "T", "A")), 1.0):
        _invalid(f"{name}_balance")


def _validate_event(event: Any, polarization: str) -> None:
    missed_fields = {
        "surface_id", "hit_xz_mm", "hit_global_xy_mm", "grid_xy_mm",
        "incidence_angle_deg", "sampled_layer_thickness_nm", "terminal_step_id",
        "input_weight", "reflected_weight", "absorbed_weight", "transmitted_weight",
    }
    interaction_fields = {
        "surface_id", "hit_xz_mm", "hit_global_xy_mm", "grid_xy_mm", "normal_xz",
        "incidence_angle_deg", "sampled_layer_thickness_nm", "rta",
        "rta_by_polarization", "input_weight", "reflected_weight", "absorbed_weight",
        "transmitted_weight",
    }
    if not isinstance(event, dict):
        _invalid("event_row")
    outside_field = "terminal_step_id" in event
    _check_keys(event, missed_fields if outside_field else interaction_fields, "event")
    if not isinstance(event["surface_id"], str) or not event["surface_id"].strip():
        _invalid("event_surface")
    if not _pair(event["hit_xz_mm"]) or not _pair(event["hit_global_xy_mm"]):
        _invalid("event_hit")
    if event["grid_xy_mm"] is not None and not _pair(event["grid_xy_mm"]):
        _invalid("event_grid_hit")
    angle = event["incidence_angle_deg"]
    if not _number(angle, minimum=0.0) or angle > 90.0 + 1e-8:
        _invalid("event_incidence_angle")
    thicknesses = event["sampled_layer_thickness_nm"]
    if not isinstance(thicknesses, list) or any(
            not _number(value, minimum=0.0) for value in thicknesses):
        _invalid("event_layer_thicknesses")
    weights = {"input_weight", "reflected_weight", "absorbed_weight", "transmitted_weight"}
    _check_nonnegative_numbers(event, weights, "event")
    if not _close(float(event["input_weight"]), sum(float(event[key]) for key in (
            "reflected_weight", "absorbed_weight", "transmitted_weight"))):
        _invalid("event_energy_balance")
    if outside_field:
        if not isinstance(event["terminal_step_id"], str) or not event["terminal_step_id"].strip():
            _invalid("event_terminal_step")
        if any(float(event[key]) != 0.0 for key in (
                "reflected_weight", "absorbed_weight", "transmitted_weight")):
            _invalid("outside_field_event_weights")
        return
    if not _pair(event["normal_xz"]):
        _invalid("event_normal")
    _validate_rta(event["rta"], "event_rta")
    by_polarization = event["rta_by_polarization"]
    required_polarizations = {"s", "p"} if polarization == "unpolarized" else {polarization}
    if not isinstance(by_polarization, dict) or set(by_polarization) != required_polarizations:
        _invalid("event_polarization_rows")
    for row in by_polarization.values():
        _validate_rta(row, "event_polarized_rta")


def validate_geometric_raytrace_result(result: Any) -> dict:
    """Validate the complete version-1 JSON result emitted by ``trace``."""
    base_fields = {"schema", "schema_version", "units", "polarization", "model",
                   "phase_between_surfaces", "spectra", "paths"}
    if not isinstance(result, dict):
        _invalid("root_object")
    has_detector = "detector" in result
    _check_keys(result, base_fields | ({"detector"} if has_detector else set()), "root")
    if (result["schema"] != RESULT_SCHEMA or type(result["schema_version"]) is not int
            or result["schema_version"] != SCHEMA_VERSION):
        _invalid("schema_version")
    if result["units"] != {"length": "mm", "wavelength": "nm", "index": "n+i*k"}:
        _invalid("units")
    polarization = result["polarization"]
    if not isinstance(polarization, str) or polarization not in {"s", "p", "unpolarized"}:
        _invalid("polarization")
    if (result["model"] != _MODEL or result["phase_between_surfaces"] != "not modeled"):
        _invalid("model")
    spectra = _validate_spectrum_rows(result["spectra"])
    wavelengths = {float(row["wavelength_nm"]): row for row in spectra}
    paths = result["paths"]
    if (not isinstance(paths, list) or not 1 <= len(paths) <= 100_000
            or len(paths) % len(spectra) != 0):
        _invalid("path_count")
    counts = {wave: 0 for wave in wavelengths}
    totals = {wave: {name: 0.0 for name in _POWER_FIELDS} for wave in wavelengths}
    sample_counts: dict[str, dict[float, int]] = {}
    expected_path_fields = _PATH_FIELDS | (_DETECTOR_PATH_FIELDS if has_detector else set())
    detector_hits: dict[float, list[float | None]] = {wave: [] for wave in wavelengths}
    for path in paths:
        _check_keys(path, expected_path_fields, "path")
        sample_id = path["sample_id"]
        wave = path["wavelength_nm"]
        if not isinstance(sample_id, str) or not sample_id.strip() or not _number(wave, minimum=0.0):
            _invalid("path_identity")
        wave = float(wave)
        if wave not in wavelengths:
            _invalid("path_wavelength")
        spectrum = wavelengths[wave]
        if (not _number(path["sample_weight"], minimum=0.0)
                or not _number(path["spectrum_weight"], minimum=0.0)
                or not _close(float(path["spectrum_weight"]), float(spectrum["spectrum_weight"]))):
            _invalid("path_weights")
        if not isinstance(path["status"], str) or path["status"] not in _PATH_STATUSES:
            _invalid("path_status")
        events = path["events"]
        if not isinstance(events, list):
            _invalid("path_events")
        for event in events:
            _validate_event(event, polarization)
        final_ray = path["final_ray"]
        if path["status"] == "transmitted":
            _check_keys(final_ray, {"origin_xz_mm", "direction_xz"}, "final_ray")
            if not _pair(final_ray["origin_xz_mm"]) or not _pair(final_ray["direction_xz"]):
                _invalid("final_ray_vectors")
        elif final_ray is not None:
            _invalid("terminal_path_has_final_ray")
        powers = path["powers"]
        _check_keys(powers, _POWER_FIELDS, "path_powers")
        _check_nonnegative_numbers(powers, _POWER_FIELDS, "path_powers")
        expected_incident = float(path["sample_weight"]) * float(path["spectrum_weight"])
        if (not _close(float(powers["incident_weight"]), expected_incident)
                or not _close(float(powers["incident_weight"]), sum(float(powers[key]) for key in (
                    "transmitted_weight", "reflected_weight", "absorbed_weight", "missed_weight")))):
            _invalid("path_energy_balance")
        counts[wave] += 1
        sample_counts.setdefault(sample_id, {}).setdefault(wave, 0)
        sample_counts[sample_id][wave] += 1
        for name in _POWER_FIELDS:
            totals[wave][name] += float(powers[name])
        if has_detector:
            hit_x = path["detector_hit_x_mm"]
            detector_status = path["detector_status"]
            captured = path["detector_captured"]
            detector_weight = path["detector_weight"]
            if (not isinstance(detector_status, str)
                    or detector_status not in {"no_transmitted_ray", "plane_not_forward",
                                               "outside_aperture", "detected"}
                    or type(captured) is not bool
                    or (hit_x is not None and not _number(hit_x))
                    or not _number(detector_weight, minimum=0.0)):
                _invalid("path_detector_fields")
            if captured != (detector_status == "detected"):
                _invalid("path_detector_capture_state")
            if captured:
                if (hit_x is None or path["status"] != "transmitted"
                        or not _close(float(detector_weight), float(powers["transmitted_weight"]))):
                    _invalid("path_detector_capture")
            elif detector_weight != 0.0:
                _invalid("path_detector_weight")
            if path["status"] != "transmitted" and (
                    detector_status != "no_transmitted_ray" or hit_x is not None):
                _invalid("path_detector_terminal_state")
            detector_hits[wave].append(float(hit_x) if captured else None)

    samples_per_spectrum = len(paths) // len(spectra)
    if not samples_per_spectrum or any(count != samples_per_spectrum for count in counts.values()):
        _invalid("path_spectrum_counts")
    if any(len(values) != len(spectra) or any(count != 1 for count in values.values())
           for values in sample_counts.values()):
        _invalid("sample_spectrum_coverage")
    for spectrum in spectra:
        wave = float(spectrum["wavelength_nm"])
        for name in _POWER_FIELDS:
            if not _close(float(spectrum[name]), totals[wave][name]):
                _invalid("spectrum_path_totals")

    if has_detector:
        detector = result["detector"]
        detector_fields = {"z_mm", "aperture_radius_mm", "pixel_count", "x_bin_edges_mm",
                           "spectra", "interpretation"}
        _check_keys(detector, detector_fields, "detector")
        pixel_count = detector["pixel_count"]
        if (not _number(detector["z_mm"]) or not _number(detector["aperture_radius_mm"], minimum=0.0)
                or detector["aperture_radius_mm"] == 0 or type(pixel_count) is not int
                or not 1 <= pixel_count <= 1_000_000
                or detector["interpretation"] != _DETECTOR_INTERPRETATION):
            _invalid("detector_settings")
        edges = detector["x_bin_edges_mm"]
        aperture = float(detector["aperture_radius_mm"])
        if (not isinstance(edges, list) or len(edges) != pixel_count + 1
                or any(not _number(edge) for edge in edges)
                or any(float(right) <= float(left) for left, right in zip(edges, edges[1:]))
                or not _close(float(edges[0]), -aperture)
                or not _close(float(edges[-1]), aperture)):
            _invalid("detector_edges")
        detector_rows = _validate_detector_spectrum_rows(detector["spectra"], spectra, pixel_count)
        detector_by_wave = {float(row["wavelength_nm"]): row for row in detector_rows}
        histograms = {wave: [0.0] * pixel_count for wave in wavelengths}
        detected = {wave: 0.0 for wave in wavelengths}
        missed = {wave: 0.0 for wave in wavelengths}
        for path in paths:
            wave = float(path["wavelength_nm"])
            row = detector_by_wave[wave]
            if path["detector_captured"]:
                x = min(max(float(path["detector_hit_x_mm"]), -aperture), aperture)
                import bisect
                bin_index = min(max(bisect.bisect_right(edges, x) - 1, 0), pixel_count - 1)
                histograms[wave][bin_index] += float(path["detector_weight"])
                detected[wave] += float(path["detector_weight"])
            else:
                missed[wave] += float(path["powers"]["transmitted_weight"])
        for wave, row in detector_by_wave.items():
            if (not _close(float(row["detected_weight"]), detected[wave])
                    or not _close(float(row["missed_weight"]), missed[wave])
                    or not _close(float(totals[wave]["transmitted_weight"]),
                                  detected[wave] + missed[wave])
                    or any(not _close(float(actual), expected) for actual, expected in
                           zip(row["ray_energy_histogram_weight"], histograms[wave]))):
                _invalid("detector_path_totals")
    return result


def validate_geometric_raytrace_summary(summary: Any) -> dict:
    """Validate the typed scheduler summary that advertises a downloadable result."""
    required = {"kind", "result_schema", "path_count", "spectrum_count", "spectra",
                "detector_spectra", "result_artifacts"}
    allowed = required | {"result_file", "artifact_id"}
    if not isinstance(summary, dict) or not required <= set(summary) or set(summary) - allowed:
        _invalid("summary_fields")
    if (summary["kind"] != "geometric_raytrace" or summary["result_schema"] != RESULT_SCHEMA
            or type(summary["path_count"]) is not int or not 1 <= summary["path_count"] <= 100_000
            or type(summary["spectrum_count"]) is not int or not 1 <= summary["spectrum_count"] <= 512):
        _invalid("summary_identity")
    spectra = _validate_spectrum_rows(summary["spectra"])
    if (summary["spectrum_count"] != len(spectra)
            or summary["path_count"] % summary["spectrum_count"]):
        _invalid("summary_counts")
    detector_rows = summary["detector_spectra"]
    if not isinstance(detector_rows, list):
        _invalid("summary_detector_spectra")
    if detector_rows:
        _validate_detector_spectrum_rows(detector_rows, spectra)
    artifacts = summary["result_artifacts"]
    if not isinstance(artifacts, list):
        _invalid("summary_artifacts")
    result_artifacts = [item for item in artifacts if isinstance(item, dict) and item.get("role") == "result"]
    if len(result_artifacts) != 1:
        _invalid("summary_result_artifact")
    if (type(result_artifacts[0].get("bytes")) is not int
            or not 0 <= result_artifacts[0]["bytes"] <= _MAX_RESULT_BYTES):
        _invalid("summary_result_artifact_size")
    if "result_file" in summary and not isinstance(summary["result_file"], str):
        _invalid("summary_private_path")
    if "artifact_id" in summary and summary["artifact_id"] != result_artifacts[0].get("artifact_id"):
        _invalid("summary_artifact_identity")
    return summary


def geometric_raytrace_summary_matches_result(summary: Any, result: Any) -> bool:
    """Check the public counts and compact spectra against the downloaded result."""
    try:
        validate_geometric_raytrace_summary(summary)
        validate_geometric_raytrace_result(result)
    except (TypeError, ValueError):
        return False
    expected_detector = (result.get("detector") or {}).get("spectra", [])
    return bool(
        summary["path_count"] == len(result["paths"])
        and summary["spectrum_count"] == len(result["spectra"])
        and summary["spectra"] == result["spectra"]
        and summary["detector_spectra"] == expected_detector
    )
