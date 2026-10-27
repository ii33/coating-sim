"""Version-two automatic repair envelope over the existing local executor.

Version-one manual A/B jobs remain readable through film_manufacturing_jobs.
Only the frozen source, optical scope and three physical user inputs cross
this boundary. Search budgets are an internal recorded policy.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
from typing import Mapping

import numpy as np

from .film_auto_synthesis import _validated, _resolve_synthesis_resource_guard, _feasible
from .film_automatic_repair import (
    automatic_repair,
    lower_limit,
    deficient_ids,
    effective_policy,
    paired_interval_pass,
    covers_grid,
    _guard_request,
    curve_audit_metadata,
    candidate_accepted,
    AUDIT_FEEDBACK_POINTS,
)
from .film_design import validate_design
from .film_design_service import simulation_settings
from .film_synthesis_io import (
    film_synthesis_request_from_payload,
    film_synthesis_request_to_payload,
)
from .film_synthesis_manufacturing import manufacturing_source_fingerprint

REQUEST_KIND = "coat_sim.film_manufacturing_request"
JOB_KIND = "coat_sim.film_manufacturing_job"
RESULT_KIND = "coat_sim.film_manufacturing_result"
FIELDS = {
    "schema_version",
    "kind",
    "synthesis",
    "source_design",
    "source_fingerprint",
    "reference_nm",
    "thickness_tolerance_nm",
    "optical_tolerance_percentage_points",
    "original_targets_present",
    "comparison_settings",
}


def _freeze_repair(service, synthesis, settings):
    """Freeze ordinary simulation inputs without synthesis's stricter domain policy."""
    from .film_optimization import _checked_frozen_snapshot
    from .film_optimization_needle import _candidate_payload_from_materials, FilmNeedleMaterialReference
    from .film_design import physical_thickness_nm
    from .optical_constants import prepare_optical_set

    synthesis = _validated(synthesis)
    snapshot = synthesis.frozen_material_snapshot
    if snapshot is None:
        snapshot = service.freeze_design(synthesis.environment, settings)
    else:
        snapshot = _checked_frozen_snapshot(snapshot, synthesis.environment)
    snapshot = deepcopy(dict(snapshot))
    snapshot["settings"] = deepcopy(settings)
    references = list(synthesis.candidate_materials)
    pairs = {(r.material_id, r.optical_set_id) for r in references}
    for seed in synthesis.seed_designs:
        for row in seed["front_layers"]:
            pair = (row["material_id"], row["optical_set_id"])
            if pair not in pairs:
                references.append(FilmNeedleMaterialReference(*pair))
                pairs.add(pair)
    snapshot = _candidate_payload_from_materials(service, snapshot, references)
    materials = {m["id"]: m for m in snapshot["materials"]}
    seeds = []
    for original in synthesis.seed_designs:
        seed = deepcopy(original)
        for row in seed["front_layers"]:
            index = 1.0
            if row["thickness"]["unit"] == "qw":
                optical = next(o for o in materials[row["material_id"]]["optical_sets"]
                               if o["id"] == row["optical_set_id"])
                index, _, _ = prepare_optical_set(optical)(seed["reference_wavelength_nm"], "nm",
                    extrapolation_policy=settings["extrapolation_policy"], include_k=True)
            row["thickness"] = dict(value=physical_thickness_nm(row, seed["reference_wavelength_nm"], index), unit="nm")
        if not _feasible(synthesis, seed):
            raise ValueError("repair source violates native physical constraints")
        seeds.append(seed)
    # Candidate closure recalculated the ordinary frozen snapshot digest.
    snapshot.pop("sha256", None)
    snapshot["sha256"] = hashlib.sha256(json.dumps(snapshot, sort_keys=True,
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return replace(synthesis, seed_designs=tuple(seeds), frozen_material_snapshot=snapshot,
                   parallel_batch_size=synthesis.parallel_batch_size or 1,
                   resource_guard_resolution=_resolve_synthesis_resource_guard(synthesis))


def make_request_payload(
    service,
    synthesis,
    source_design,
    *,
    reference_nm,
    thickness_tolerance_nm,
    optical_tolerance_percentage_points,
    original_targets_present=True,
    comparison_settings=None,
    request_id="automatic-repair",
):
    source = validate_design(deepcopy(dict(source_design)))
    settings = simulation_settings(comparison_settings)
    if settings["first_surface"] != source["first_surface"]:
        raise ValueError("repair comparison incidence differs from the supported source incidence")
    frozen = _freeze_repair(
        getattr(service, "film_design_service", service), replace(synthesis, seed_designs=(source,)), settings
    )
    source = deepcopy(dict(frozen.seed_designs[0]))
    payload = dict(
        schema_version=2,
        kind=REQUEST_KIND,
        synthesis=film_synthesis_request_to_payload(request_id, frozen),
        source_design=source,
        source_fingerprint=manufacturing_source_fingerprint(frozen, source),
        reference_nm=reference_nm,
        thickness_tolerance_nm=thickness_tolerance_nm,
        optical_tolerance_percentage_points=optical_tolerance_percentage_points,
        original_targets_present=original_targets_present,
        comparison_settings=settings,
    )
    request_from_payload(payload)
    return payload


def request_from_payload(value):
    if (
        not isinstance(value, Mapping)
        or set(value) != FIELDS
        or value["schema_version"] not in (2, 3)
        or type(value["schema_version"]) is not int
        or value["kind"] != REQUEST_KIND
        or type(value["original_targets_present"]) is not bool
    ):
        raise ValueError("invalid automatic repair request")
    lower_limit(value["reference_nm"], value["thickness_tolerance_nm"])
    tolerance = value["optical_tolerance_percentage_points"]
    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not math.isfinite(tolerance)
        or tolerance < 0
    ):
        raise ValueError("optical tolerance must be nonnegative finite percentage points")
    _, synthesis, structures = film_synthesis_request_from_payload(value["synthesis"])
    if structures or synthesis.cpu_workers != 1 or synthesis.forward_backend != "cpu":
        raise ValueError("automatic repair uses frozen local CPU optics")
    if value["schema_version"] == 3 and any(getattr(synthesis, k) is not None
            for k in ("max_evaluations", "max_seconds", "max_cycles")):
        raise ValueError("live repair must not inherit ordinary optimizer total quotas")
    settings = simulation_settings(value["comparison_settings"])
    if settings != value["comparison_settings"]:
        raise ValueError("automatic repair comparison settings are not canonical")
    checked = _freeze_repair(None, synthesis, settings)
    if (
        film_synthesis_request_to_payload(value["synthesis"]["request_id"], checked)
        != value["synthesis"]
    ):
        raise ValueError("automatic repair request is not fully frozen")
    source = validate_design(deepcopy(value["source_design"]))
    if (
        source != value["source_design"]
        or manufacturing_source_fingerprint(checked, source) != value["source_fingerprint"]
    ):
        raise ValueError("automatic repair source identity differs")
    if not checked.seed_designs or source != checked.seed_designs[0]:
        raise ValueError("automatic repair source differs from its frozen seed")
    from .film_synthesis_archive import physical_design_hash

    environment = deepcopy(source)
    environment["front_layers"] = environment["back_layers"] = []
    if physical_design_hash(environment) != physical_design_hash(checked.environment):
        raise ValueError("automatic repair source differs from frozen physical ports")
    if (
        source["back_layers"]
        or source["first_surface"] != "front"
        or settings["first_surface"] != source["first_surface"]
        or source["substrate"]["sides"] != "single"
        or not source["use_k"]
    ):
        raise ValueError("automatic repair requires single front incidence with source k")
    if not value["original_targets_present"]:
        if (
            len(checked.objectives) != 2
            or tuple(o.metric for o in checked.objectives) != ("R", "T")
            or any(
                (
                    o.wavelength_min_nm,
                    o.wavelength_max_nm,
                    o.wavelength_samples,
                    o.angles_deg,
                    o.polarization,
                )
                != (settings["wavelength_min_nm"], settings["wavelength_max_nm"],
                    settings["samples"], (settings["angle_deg"],), settings["polarization"])
                or o.mean.enabled
                or not o.pointwise.enabled
                or o.pointwise.mode != "range"
                or o.pointwise.lower_bound != 0.0
                or o.pointwise.upper_bound != 1.0
                or o.pointwise.strict_lower
                or o.pointwise.strict_upper
                for o in checked.objectives
            )
        ):
            raise ValueError(
                "no-original-target repair must use the frozen simulation comparison scope"
            )
    return checked, source


def build_job_payload(service, request):
    request_from_payload(request)
    return dict(schema_version=request["schema_version"], kind=JOB_KIND, request=deepcopy(dict(request)))


def result_matches_job(value, job):
    return bool(
        isinstance(value, Mapping)
        and isinstance(job, Mapping)
        and value.get("schema_version") in (2, 3)
        and value.get("kind") == RESULT_KIND
        and job.get("schema_version") == value.get("schema_version")
        and job.get("kind") == JOB_KIND
        and value.get("request") == job.get("request")
    )


def estimate_resources(request_payload, frozen=None, source=None):
    """Existing full-output model with this finite slice's scalar call bound."""
    from .film_manufacturing_jobs import estimate_result_resources, estimate_audit_resources
    from .film_synthesis_manufacturing import (
        manufacturing_refinement_working_set_bytes,
        _owned_bytes,
    )

    if frozen is None or source is None:
        frozen, source = request_from_payload(request_payload)
    if request_payload["schema_version"] == 3:
        guide = _guard_request(frozen, request_payload["optical_tolerance_percentage_points"] / 100.0)
        numerical = manufacturing_refinement_working_set_bytes(guide, len(source["front_layers"]))
        audit = estimate_audit_resources(guide, source, physical_call_limit=1 + len(guide.objectives))
        return dict(working_set_bytes=max(numerical, audit["working_set_bytes"]) + _owned_bytes(frozen, source),
                    ownership_model="dense upfront; actual scalar/live/checkpoint ownership checked incrementally")
    controls = effective_policy(frozen)
    guide = _guard_request(frozen, request_payload["optical_tolerance_percentage_points"] / 100.0)
    largest = dict(source)
    largest["front_layers"] = list(source["front_layers"])
    if largest["front_layers"]:
        largest["front_layers"] = [*largest["front_layers"], largest["front_layers"][0]]
    resources = estimate_result_resources(
        request_payload, guide, largest, audit_physical_call_limit=controls["physical_calls"]
    )
    audit = estimate_audit_resources(guide, largest, physical_call_limit=controls["physical_calls"])
    dense_feedback = max((replace(o, wavelength_samples=(1 if o.wavelength_samples == 1 else
        (o.wavelength_samples - 1) * frozen.validation_samples_multiplier + 1)) for o in frozen.objectives),
        key=lambda o: manufacturing_refinement_working_set_bytes(replace(guide, objectives=(o,)),
                                                                len(largest["front_layers"])))
    joint = replace(guide, objectives=guide.objectives + (
        frozen.objectives if request_payload["original_targets_present"] else ())
        + (dense_feedback,) * AUDIT_FEEDBACK_POINTS)
    numerical = 2 * manufacturing_refinement_working_set_bytes(joint, len(largest["front_layers"]))
    resources["working_set_bytes"] = max(
        resources["phase_working_set_bytes"], 2 * audit["working_set_bytes"], numerical
    ) + _owned_bytes(frozen, source)
    return resources


def execute_job(parameters, cancel_event, report, checkpoint_path, write_json, **_kwargs):
    job = parameters["payload"]
    if isinstance(job, Mapping) and job.get("schema_version") == 3:
        return _execute_live_job(parameters, cancel_event, report, checkpoint_path, write_json, **_kwargs)
    if (
        set(job) != {"schema_version", "kind", "request"}
        or job.get("kind") != JOB_KIND
        or job.get("schema_version") != 2
    ):
        raise ValueError("invalid automatic repair job")
    frozen, source = request_from_payload(job["request"])
    from .film_manufacturing_jobs import _expanded_json_sizes

    resources = estimate_resources(job["request"], frozen, source)
    external_retained_bytes = _kwargs.get("external_retained_bytes", lambda: 0)

    def external_bytes():
        size = external_retained_bytes()
        if type(size) is not int or size < 0:
            raise ValueError("external retained bytes must be a nonnegative integer")
        return size

    limit = (frozen.resource_guard_resolution or {}).get("effective_bytes")
    if limit is not None and resources["working_set_bytes"] + external_bytes() > limit:
        raise ValueError("automatic repair pre-admission exceeds existing resource guard")

    def envelope(result):
        reason = result["termination"]
        state = "cancelled" if reason == "cancelled" else "completed"
        body = dict(result)
        body["routes"] = [dict(route="automatic", champion=body["best"], status=reason)]
        payload = dict(
            schema_version=2,
            kind=RESULT_KIND,
            request=job["request"],
            state=state,
            is_final=True,
            termination_reason=reason,
            result=body,
            work=body["work"],
        )
        owned, encoded = _expanded_json_sizes(payload)
        external = external_bytes()
        if (
            owned > resources["expanded_projection_bytes_upper_bound"]
            or encoded > resources["encoded_json_bytes_upper_bound"]
            or (limit is not None and 128 * 1024**2 + 4 * owned + 2 * encoded + external > limit)
        ):
            raise ValueError("automatic repair serialization exceeds existing resource guard")
        return payload

    def save(result):
        payload = envelope(result)
        payload = validate_result_payload(payload, expected_job=job, replay=False)
        write_json(
            checkpoint_path,
            dict(
                schema_version=2,
                kind="coat_sim.film_manufacturing_checkpoint",
                job=job,
                result=payload,
            ),
        )

    result = automatic_repair(
        frozen,
        source,
        target_nm=job["request"]["reference_nm"],
        thickness_tolerance_nm=job["request"]["thickness_tolerance_nm"],
        optical_tolerance=job["request"]["optical_tolerance_percentage_points"] / 100.0,
        cancel_event=cancel_event,
        report=report,
        checkpoint=save,
        external_retained_bytes=external_bytes,
        optical_settings=job["request"]["comparison_settings"],
        original_targets_present=job["request"]["original_targets_present"],
    )
    payload = validate_result_payload(envelope(result), expected_job=job, replay=False)
    save(result)
    return payload


def _validate_guard(guard, request, tolerance, *, independent=False):
    fields = {
        "maximum_absolute_change",
        "tolerance_fraction",
        "passed",
        "records",
        "continuous_certificate",
    }
    if not isinstance(guard, Mapping) or set(guard) not in (fields, fields | {"audit"}):
        raise ValueError("invalid curve-difference evidence")
    if independent and "audit" not in guard:
        raise ValueError("curve-difference independent audit metadata is missing")
    if "audit" in guard and not isinstance(guard["audit"], Mapping):
        raise ValueError("curve-difference audit metadata must be a record")
    for name in ("maximum_absolute_change", "tolerance_fraction"):
        if (
            isinstance(guard[name], bool)
            or not isinstance(guard[name], (int, float))
            or not math.isfinite(guard[name])
            or guard[name] < 0
        ):
            raise ValueError("curve-difference scalar evidence is invalid")
    if guard["tolerance_fraction"] != tolerance or guard["continuous_certificate"] is not False:
        raise ValueError("curve-difference tolerance or scope differs")
    seen: set[tuple[int, float]] = set()
    maximum = 0.0
    passed = True
    for row in guard["records"]:
        index = row["objective_index"]
        if type(index) is not int or not 0 <= index < len(request.objectives):
            raise ValueError("curve-difference objective index is invalid")
        objective = request.objectives[index]
        key = index, row["angle_deg"]
        if (
            key in seen
            or row["angle_deg"] not in objective.angles_deg
            or row["metric"] != objective.metric
            or row["polarization"] != objective.polarization
        ):
            raise ValueError("curve-difference objective/angle/metric/polarization differs")
        seen.add(key)
        waves = np.asarray(row["wavelength_nm"], float)
        actual = np.asarray(row["candidate_values"], float)
        baseline = np.asarray(row["reference_values"], float)
        if (
            waves.ndim != 1
            or not len(waves)
            or actual.shape != waves.shape
            or baseline.shape != waves.shape
            or not np.all(np.isfinite(waves))
            or not np.all(np.isfinite(actual))
            or not np.all(np.isfinite(baseline))
            or np.any(actual < -1e-12)
            or np.any(actual > 1.0 + 1e-12)
            or np.any(baseline < -1e-12)
            or np.any(baseline > 1.0 + 1e-12)
            or np.any(np.diff(waves) <= 0)
            or waves[0] != objective.wavelength_min_nm
            or waves[-1] != objective.wavelength_max_nm
        ):
            raise ValueError("curve-difference coverage is incomplete")
        maximum = max(maximum, float(np.max(np.abs(actual - baseline))))
        passed = passed and paired_interval_pass(actual, baseline, tolerance)
        count = objective.wavelength_samples
        audit_kind = guard.get("audit", {}).get("kind")
        if independent and audit_kind == "paired_curve_difference":
            count = 1 if count == 1 else (count - 1) * request.validation_samples_multiplier + 1
        expected_waves = np.linspace(
            objective.wavelength_min_nm, objective.wavelength_max_nm, count
        )
        if not covers_grid(waves, expected_waves):
            raise ValueError("curve-difference coverage omits a required grid")
        if independent and audit_kind == "paired_curve_difference" and count > 1:
            step = (objective.wavelength_max_nm - objective.wavelength_min_nm) / (count - 1)
            shifted = np.linspace(
                objective.wavelength_min_nm + 0.5 * step,
                objective.wavelength_max_nm - 0.5 * step,
                count - 1,
            )
            if not covers_grid(waves, shifted):
                raise ValueError("curve-difference coverage omits the independent offset grid")
    expected = {(i, a) for i, o in enumerate(request.objectives) for a in o.angles_deg}
    if (
        seen != expected
        or guard["maximum_absolute_change"] != maximum
        or type(guard["passed"]) is not bool
        or guard["passed"] is not (bool(expected) and passed)
    ):
        raise ValueError("curve-difference classification differs from paired samples")
    if independent:
        audit = guard["audit"]
        if audit.get("kind") == "paired_curve_difference":
            metadata = {
                "kind",
                "physical_evaluations",
                "wavelength_points",
                "dense_points",
                "offset_points",
                "unique_condition_points",
                "bracket_count",
                "completed_extrema",
                "continuous_certificate",
            }
            if set(audit) != metadata or audit["continuous_certificate"] is not False:
                raise ValueError("curve-difference audit metadata is invalid")
            for name in metadata - {"kind", "continuous_certificate"}:
                if type(audit[name]) is not int or audit[name] < 0:
                    raise ValueError("curve-difference audit counts are invalid")
            dense = offset = offset_calls = 0
            for objective in request.objectives:
                count = (
                    1
                    if objective.wavelength_samples == 1
                    else (objective.wavelength_samples - 1) * request.validation_samples_multiplier
                    + 1
                )
                dense += count * len(objective.angles_deg)
                if count > 1:
                    offset += (count - 1) * len(objective.angles_deg)
                    offset_calls += 1
            scalar = audit["physical_evaluations"] - 1 - offset_calls
            if (
                audit["dense_points"] != dense
                or audit["offset_points"] != offset
                or scalar < 0
                or audit["wavelength_points"] != dense + offset + scalar
                or audit["unique_condition_points"]
                != sum(len(row["wavelength_nm"]) for row in guard["records"])
                or audit["completed_extrema"] > audit["bracket_count"]
            ):
                raise ValueError("curve-difference audit completion differs from coverage")
        elif audit.get("kind") != "identical_frozen_design":
            raise ValueError("curve-difference audit kind is unknown")


def validate_result_payload(value, *, expected_job=None, replay=True):
    if isinstance(value, Mapping) and value.get("schema_version") == 3:
        return _validate_live_result(value, expected_job=expected_job, replay=replay)
    from .film_manufacturing_jobs import _json, _snapshot

    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "schema_version",
            "kind",
            "request",
            "state",
            "is_final",
            "termination_reason",
            "result",
            "work",
        }
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 2
        or value.get("kind") != RESULT_KIND
        or value["is_final"] is not True
        or value["state"] not in {"completed", "cancelled", "failed"}
    ):
        raise ValueError("invalid automatic repair result envelope")
    if expected_job is not None and not result_matches_job(value, expected_job):
        raise ValueError("automatic repair result is not bound to job")
    request, source = request_from_payload(value["request"])
    body = value["result"]
    floor = lower_limit(
        value["request"]["reference_nm"], value["request"]["thickness_tolerance_nm"]
    )
    if (
        not isinstance(body, Mapping)
        or set(body)
        != {
            "source",
            "best",
            "reviewed",
            "detected_layer_ids",
            "lower_limit_nm",
            "policy",
            "work",
            "trace",
            "elapsed_seconds",
            "termination",
            "routes",
        }
        or body["lower_limit_nm"] != floor
        or body["detected_layer_ids"] != deficient_ids(source, floor)
    ):
        raise ValueError("automatic layer detection differs from frozen source")
    if body["work"] != value["work"] or body["termination"] != value["termination_reason"]:
        raise ValueError("automatic repair work/terminal identity differs")
    if (
        body["policy"] != effective_policy(request)
        or effective_policy(request, body["policy"]) != body["policy"]
    ):
        raise ValueError("automatic repair policy differs from its frozen inputs")
    counters = {
        "physical_calls",
        "wavelength_points",
        "candidates",
        "local_calls",
        "audit_calls",
        "reference_calls",
    }
    if (
        not isinstance(body["work"], Mapping)
        or set(body["work"]) != counters | {"partial_physical_work_possible"}
        or type(body["work"]["partial_physical_work_possible"]) is not bool
        or any(type(body["work"][k]) is not int or body["work"][k] < 0 for k in counters)
        or body["work"]["physical_calls"] > body["policy"]["physical_calls"]
        or body["work"]["candidates"] > body["policy"]["proposals"]
        or body["work"]["reference_calls"] > body["work"]["physical_calls"]
        or body["work"]["audit_calls"] > body["work"]["physical_calls"]
    ):
        raise ValueError("automatic repair work exceeds its declared policy")
    if (
        isinstance(body["elapsed_seconds"], bool)
        or not isinstance(body["elapsed_seconds"], (int, float))
        or not math.isfinite(body["elapsed_seconds"])
        or body["elapsed_seconds"] < 0
    ):
        raise ValueError("automatic repair elapsed time is invalid")
    allowed = {
        "search_quantum_complete",
        "proposal_quantum_limit",
        "no_thin_layers",
        "repair_feasible",
        "time_limit",
        "evaluation_limit",
        "resource_guard",
        "cancelled",
        "scheduler_cancel_won",
        "scheduler_failed",
    }
    expected_state = (
        "cancelled"
        if body["termination"] in {"cancelled", "scheduler_cancel_won"}
        else "failed"
        if body["termination"] == "scheduler_failed"
        else "completed"
    )
    if body["termination"] not in allowed or value["state"] != expected_state:
        raise ValueError("automatic repair terminal state/reason differs")
    # Reuse strict original-score evidence validation; the new optical guard is
    # additional evidence and cannot silently relabel original nominal status.
    from types import SimpleNamespace

    proxy = SimpleNamespace(synthesis=request)
    for key in ("source", "best", "reviewed"):
        raw = body[key]
        if raw is None:
            continue
        snapshot = {name: raw[name] for name in ("design", "training_score", "audit_score")}
        _snapshot(snapshot, proxy)
        if key == "source" and raw["design"] != source:
            raise ValueError("repair source snapshot changed")
        if key != "source":
            candidate = raw["design"]
            from .film_design import MAX_LAYERS

            if (
                deficient_ids(candidate, max(floor, request.minimum_nonzero_thickness_nm or 0.0))
                or raw.get("thickness_pass") is not True
                or len(candidate["front_layers"])
                > min(MAX_LAYERS, request.max_layers or MAX_LAYERS)
                or any(row["thickness"]["unit"] != "nm" for row in candidate["front_layers"])
                or (
                    request.max_layer_thickness_nm is not None
                    and any(
                        r["thickness"]["value"] > request.max_layer_thickness_nm
                        for r in candidate["front_layers"]
                    )
                )
                or (
                    request.max_total_thickness_nm is not None
                    and math.fsum(r["thickness"]["value"] for r in candidate["front_layers"])
                    > request.max_total_thickness_nm
                )
            ):
                raise ValueError("repair candidate retains a deficient layer")
            environment = deepcopy(candidate)
            environment["front_layers"] = []
            original_environment = deepcopy(source)
            original_environment["front_layers"] = []
            from .film_synthesis_archive import physical_design_hash

            if physical_design_hash(environment) != physical_design_hash(original_environment):
                raise ValueError("repair candidate changed physical ports")
            pairs = {(r["material_id"], r["optical_set_id"]) for r in source["front_layers"]}
            if any(
                (r["material_id"], r["optical_set_id"]) not in pairs
                for r in candidate["front_layers"]
            ):
                raise ValueError("repair candidate introduced unauthorized material")
            _validate_guard(
                raw["optical_guard"],
                request,
                value["request"]["optical_tolerance_percentage_points"] / 100.0,
                independent=True,
            )
            if raw["optical_guard"]["audit"].get("kind") == "identical_frozen_design":
                if (
                    raw["design"] != source
                    or raw["optical_guard"]["audit"]
                    != dict(
                        kind="identical_frozen_design", physical_hash=physical_design_hash(source)
                    )
                    or raw["optical_guard"]["maximum_absolute_change"] != 0.0
                ):
                    raise ValueError("identical-design zero difference is not bound to source")
                coverage = raw["training_score"]["coverage"]
                for row in raw["optical_guard"]["records"]:

                    def field(record, name):
                        return (
                            record[name] if isinstance(record, Mapping) else getattr(record, name)
                        )

                    matching = next(
                        (
                            c
                            for c in coverage
                            if field(c, "objective_index") == row["objective_index"]
                            and field(c, "angle_deg") == row["angle_deg"]
                        ),
                        None,
                    )
                    if (
                        matching is None
                        or not np.array_equal(
                            row["wavelength_nm"], field(matching, "wavelength_nm")
                        )
                        or not np.array_equal(
                            row["candidate_values"], field(matching, "actual_values")
                        )
                        or not np.array_equal(
                            row["reference_values"], field(matching, "actual_values")
                        )
                    ):
                        raise ValueError(
                            "identical-design curve evidence differs from computed source"
                        )
            if key == "best" and not candidate_accepted(raw, value["request"]["original_targets_present"]):
                raise ValueError("repair best does not pass the joint optical and original-target guard")
    if body["routes"] != [
        dict(route="automatic", champion=body["best"], status=body["termination"])
    ]:
        raise ValueError("repair displayed selection differs from audited best")
    if replay:
        # Recompute the paired independent difference audit from immutable
        # source/materials before presenting persisted evidence as verified.
        from .film_automatic_repair import CurvePair, _guard_request
        from .film_auto_synthesis import _proxy
        from .film_optimization_structure import evaluate_frozen_structure_candidate
        from .film_synthesis_audit import audit_frozen_synthesis_candidate
        from .film_synthesis_manufacturing import manufacturing_snapshot

        seen = set()
        optical_settings = value["request"]["comparison_settings"]
        for raw in (body["source"], body["best"], body["reviewed"]):
            if raw is None:
                continue
            from .film_synthesis_archive import physical_design_hash

            identity = physical_design_hash(raw["design"])
            if identity in seen:
                continue
            seen.add(identity)
            fresh = evaluate_frozen_structure_candidate(_proxy(request), raw["design"],
                optical_settings=optical_settings)
            fresh_audit = audit_frozen_synthesis_candidate(request, raw["design"],
                optical_settings=optical_settings)
            verified = manufacturing_snapshot(request, dict(fresh), audit_value=fresh_audit)
            if _json(verified.training_score) != _json(raw["training_score"]) or _json(
                verified.audit_score
            ) != _json(raw["audit_score"]):
                raise ValueError("persisted original-goal evidence fails physical replay")

        for raw in (body["best"], body["reviewed"]):
            if raw is None:
                continue
            if raw["design"] == source and raw["optical_guard"]["maximum_absolute_change"] == 0.0:
                continue  # Identical physical design has exact zero difference on every wavelength.
            guide = _guard_request(
                request, value["request"]["optical_tolerance_percentage_points"] / 100.0
            )

            def physical(design, *, objectives=None, jacobian=False):
                active = (
                    request
                    if objectives is None
                    else replace(request, objectives=tuple(objectives))
                )
                return evaluate_frozen_structure_candidate(_proxy(active), design,
                    optical_settings=optical_settings)

            pair = CurvePair(guide, source, physical)
            difference_audit = audit_frozen_synthesis_candidate(
                guide, raw["design"], response_transform=pair.transform,
                optical_settings=optical_settings,
            )
            expected = pair.evidence(
                value["request"]["optical_tolerance_percentage_points"] / 100.0
            )
            expected["audit"] = curve_audit_metadata(difference_audit)
            if _json(expected) != _json(raw["optical_guard"]):
                raise ValueError("persisted curve-difference evidence fails physical replay")
    return _json(value)


def make_live_request_payload(service, synthesis, source_design, **kwargs):
    """Version three removes inherited total quotas, not native process limits."""
    frozen = replace(synthesis, max_evaluations=None, max_seconds=None, max_cycles=None)
    value = make_request_payload(service, frozen, source_design, **kwargs)
    value["schema_version"] = 3
    request_from_payload(value)
    return value


def _live_identity(request_payload):
    from .film_manufacturing_jobs import _json
    from .film_synthesis_archive import physical_design_hash
    request_hash = hashlib.sha256(json.dumps(_json(request_payload), sort_keys=True,
        ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()).hexdigest()
    return dict(request_id=request_payload["synthesis"]["request_id"], request_hash=request_hash,
                source_fingerprint=request_payload["source_fingerprint"],
                source_hash=physical_design_hash(request_payload["source_design"]))


def _validate_live_layers(layers, design, source):
    if not isinstance(layers, list) or len(layers) != len(design["front_layers"]):
        raise ValueError("live layer lineage does not cover the current design")
    originals = {r["id"]: r for r in source["front_layers"]}
    for row, physical in zip(layers, design["front_layers"], strict=True):
        if (not isinstance(row, Mapping) or set(row) != {"id", "material_id", "optical_set_id", "thickness_nm", "origins"}
                or any(row[k] != physical[k] for k in ("id", "material_id", "optical_set_id"))
                or row["thickness_nm"] != physical["thickness"]["value"]
                or not isinstance(row["origins"], list) or len(set(row["origins"])) != len(row["origins"])
                or any(k not in originals or any(originals[k][name] != row[name] for name in ("material_id", "optical_set_id"))
                       for k in row["origins"])):
            raise ValueError("live layer identity or original-layer provenance differs")


def _validate_live_design(design, request, source, *, allow_zero=False, minimum_probe_id=None):
    from .film_automatic_repair import legal_thicknesses
    from .film_synthesis_archive import physical_design_hash
    checked = validate_design(deepcopy(design))
    if checked != design or not legal_thicknesses(request, checked, 0.0,
            allow_zero=allow_zero, minimum_probe_id=minimum_probe_id):
        raise ValueError("live design violates native physical boundaries")
    if checked.get("workflow_settings") != source.get("workflow_settings"):
        raise ValueError("live repair metadata differs from the frozen source workflow")
    ports, original = deepcopy(checked), deepcopy(source)
    ports["front_layers"], original["front_layers"] = [], []
    if physical_design_hash(ports) != physical_design_hash(original):
        raise ValueError("live repair changed frozen physical ports")
    allowed = {(r["material_id"], r["optical_set_id"]) for r in source["front_layers"]}
    if any((r["material_id"], r["optical_set_id"]) not in allowed for r in checked["front_layers"]):
        raise ValueError("live repair introduced an unfrozen material pair")
    return checked


def _validate_live_curve_binding(records, candidate, source_snapshot, *, objective_indices=None,
                                 require_candidate_extrema=False):
    """Bind paired numbers to already recorded complete training/dense spectra."""
    from .film_automatic_repair import _live_field
    by_key = {(r["objective_index"], float(r["angle_deg"])): r for r in records}
    for snapshot, field in ((candidate, "candidate_values"), (source_snapshot, "reference_values")):
        for scope in ("training_score", "audit_score"):
            for coverage in snapshot[scope]["coverage"]:
                key = _live_field(coverage, "objective_index"), float(_live_field(coverage, "angle_deg"))
                if objective_indices is not None and key[0] not in objective_indices:
                    continue
                if key not in by_key:
                    raise ValueError("paired optical proof is missing a physical condition")
                row = by_key[key]
                waves = np.asarray(row["wavelength_nm"], float)
                expected = np.asarray(_live_field(coverage, "wavelength_nm"), float)
                positions = np.searchsorted(waves, expected)
                if (np.any(positions >= len(waves)) or not np.array_equal(waves[positions], expected)
                        or not np.array_equal(np.asarray(row[field], float)[positions],
                                              np.asarray(_live_field(coverage, "actual_values"), float))):
                    raise ValueError("paired optical values are not bound to the candidate/source spectra")
        for coverage in snapshot["audit_score"].get("audit_coverage", ()):
            key = _live_field(coverage, "objective_index"), float(_live_field(coverage, "angle_deg"))
            if objective_indices is not None and key[0] not in objective_indices:
                continue
            if key not in by_key:
                if require_candidate_extrema and field == "candidate_values":
                    raise ValueError("original-protection proof is missing audited candidate extrema")
                continue
            row = by_key[key]
            waves = np.asarray(row["wavelength_nm"], float)
            expected = np.asarray(_live_field(coverage, "wavelength_nm"), float)
            positions = np.searchsorted(waves, expected)
            valid = positions < len(waves)
            matching = np.zeros(len(expected), dtype=bool)
            matching[valid] = waves[positions[valid]] == expected[valid]
            if require_candidate_extrema and field == "candidate_values" and not np.all(matching):
                raise ValueError("original-protection proof does not cover the complete candidate audit")
            if not np.array_equal(np.asarray(row[field], float)[positions[matching]],
                                  np.asarray(_live_field(coverage, "actual_values"), float)[matching]):
                raise ValueError("paired optical values differ at an already audited physical point")


def _validate_live_snapshot(raw, source_snapshot, payload, request, source, *, is_source=False):
    from types import SimpleNamespace
    from .film_manufacturing_jobs import _snapshot, _json
    from .film_automatic_repair import assess_live_candidate, thickness_defects
    from .film_synthesis_archive import physical_design_hash
    if not isinstance(raw, Mapping):
        raise ValueError("live candidate must be a complete snapshot")
    checked = _validate_live_design(raw["design"], request, source, allow_zero=is_source)
    if not is_source and any(r["thickness"]["value"] == 0.0 for r in checked["front_layers"]):
        raise ValueError("live audited candidate must delete exact zero layers before scoring")
    identity = _live_identity(payload)
    if (raw.get("physical_hash") != physical_design_hash(checked)
            or raw.get("evidence_binding") != dict(training_hash=physical_design_hash(checked), audit_hash=physical_design_hash(checked))
            or any(raw.get(k) != identity[k] for k in ("request_hash", "source_fingerprint", "source_hash"))
            or raw.get("complete_evidence") is not True or type(raw.get("converged")) is not bool):
        raise ValueError("live evidence identity/completeness differs from the frozen job")
    _validate_live_layers(raw["layers"], checked, source)
    _snapshot({k: raw[k] for k in ("design", "training_score", "audit_score")}, SimpleNamespace(synthesis=request))
    _validate_guard(raw["optical_guard"], request,
        payload["optical_tolerance_percentage_points"] / 100.0, independent=True)
    if is_source and checked != source:
        raise ValueError("live source snapshot differs from the frozen original")
    if raw["optical_guard"]["audit"].get("kind") == "identical_frozen_design":
        if checked != source or raw["optical_guard"]["maximum_absolute_change"] != 0.0:
            raise ValueError("identical-curve proof does not belong to the frozen source")
        for row in raw["optical_guard"]["records"]:
            if row["candidate_values"] != row["reference_values"]:
                raise ValueError("identical source has nonzero paired difference")
        from .film_automatic_repair import _live_field
        coverage_by_key = {(_live_field(c, "objective_index"), float(_live_field(c, "angle_deg"))): c
                           for c in raw["training_score"]["coverage"]}
        for row in raw["optical_guard"]["records"]:
            coverage = coverage_by_key[row["objective_index"], float(row["angle_deg"])]
            if (row["wavelength_nm"] != list(_live_field(coverage, "wavelength_nm"))
                    or row["candidate_values"] != list(_live_field(coverage, "actual_values"))):
                raise ValueError("identical curve proof is not the computed original spectrum")
    if source_snapshot is not None:
        if raw["optical_guard"]["audit"].get("kind") != "identical_frozen_design":
            _validate_live_curve_binding(raw["optical_guard"]["records"], raw, source_snapshot)
        if raw["protection_guard"].get("kind") == "per_condition_nonincrease":
            _validate_live_curve_binding(raw["protection_guard"]["records"], raw, source_snapshot,
                objective_indices={i for i, objective in enumerate(request.objectives) if objective.pointwise.enabled},
                require_candidate_extrema=True)
        assessment = assess_live_candidate(raw, source_snapshot, request,
            lower_limit(payload["reference_nm"], payload["thickness_tolerance_nm"]),
            original_targets_present=payload["original_targets_present"])
        if raw.get("assessment") != assessment or raw.get("fully_repaired") is not assessment["all_requirements_met"]:
            raise ValueError("live improvement assessment is stale or forged")
        if raw["protection_guard"].get("passed") is not assessment["original_protection_pass"]:
            raise ValueError("declared original-protection flag differs from paired evidence")
        if raw.get("thickness_pass") is not (thickness_defects(checked,
                lower_limit(payload["reference_nm"], payload["thickness_tolerance_nm"]))["count"] == 0):
            raise ValueError("live thickness completion flag differs from actual layers")
    return _json(raw)


def applicable_improvement(snapshot, source_snapshot, request_payload):
    """Pure UI/worker gate. No optical evaluation or solver work is performed."""
    try:
        if request_payload.get("schema_version") != 3 or source_snapshot is None or snapshot is None:
            return False
        request, source = request_from_payload(request_payload)
        _validate_live_snapshot(source_snapshot, None, request_payload, request, source, is_source=True)
        checked = _validate_live_snapshot(snapshot, source_snapshot, request_payload, request, source)
        return checked["assessment"]["applicable_improvement"] is True
    except (ValueError, TypeError, KeyError, ArithmeticError, AttributeError):
        return False


def _validate_live_current(current, payload, request, source):
    from .film_synthesis_archive import physical_design_hash
    from .film_automatic_repair import ZERO_COLLAPSE_OPERATOR
    if current is None:
        return
    if (not isinstance(current, Mapping) or set(current) != {"design", "physical_hash", "candidate_id",
            "evidence_scope", "operator", "parent_hash", "layers"}
            or current["evidence_scope"] not in {"training", "audited"}
            or not isinstance(current["candidate_id"], str) or not isinstance(current["operator"], str)
            or not isinstance(current["parent_hash"], str)):
        raise ValueError("invalid live current-preview record")
    probe_id = (current["operator"][len(ZERO_COLLAPSE_OPERATOR):]
        if current["evidence_scope"] == "training" and current["operator"].startswith(ZERO_COLLAPSE_OPERATOR) else None)
    checked = _validate_live_design(current["design"], request, source,
        allow_zero=True, minimum_probe_id=probe_id)
    if probe_id is not None and probe_id not in {r["id"] for r in checked["front_layers"]}:
        raise ValueError("live zero-directed preview does not identify its current mathematical probe")
    if current["physical_hash"] != physical_design_hash(checked):
        raise ValueError("live preview hash differs from its design")
    _validate_live_layers(current["layers"], checked, source)


def validate_live_progress(frame, expected_request, after_seq=-1):
    from .film_manufacturing_jobs import _json
    request, source = request_from_payload(expected_request)
    if expected_request["schema_version"] != 3:
        raise ValueError("live progress requires the exact version-three request")
    identity = _live_identity(expected_request)
    if (not isinstance(frame, Mapping) or set(frame) != {"stage", "schema_version", "seq", "request_id",
            "request_hash", "source_fingerprint", "source_hash", "status", "current", "champion", "source", "work"}
            or frame.get("stage") != "repair_live" or type(frame.get("schema_version")) is not int
            or frame["schema_version"] != 1 or type(frame.get("seq")) is not int or frame["seq"] < 0
            or type(after_seq) is not int or frame["seq"] <= after_seq
            or any(frame.get(k) != v for k, v in identity.items())
            or frame["status"] not in {"running", "completed", "cancelled"}):
        raise ValueError("live progress identity, sequence or status differs")
    if frame["source"] is not None:
        _validate_live_snapshot(frame["source"], None, expected_request, request, source, is_source=True)
    if frame["champion"] is not None and not applicable_improvement(frame["champion"], frame["source"], expected_request):
        raise ValueError("live champion lacks complete protected improvement evidence")
    _validate_live_current(frame["current"], expected_request, request, source)
    if frame["current"] is not None and frame["current"]["evidence_scope"] == "audited":
        hashes = {v["physical_hash"] for v in (frame["source"], frame["champion"]) if v is not None}
        if frame["current"]["physical_hash"] not in hashes:
            raise ValueError("audited preview is not a complete source or champion")
    _validate_live_work(frame["work"])
    return _json(frame)


def _validate_live_work(work):
    counters = {"physical_calls", "wavelength_points", "candidates", "local_calls", "audit_calls", "reference_calls"}
    if (not isinstance(work, Mapping) or set(work) != counters | {"partial_physical_work_possible"}
            or type(work["partial_physical_work_possible"]) is not bool
            or any(type(work[k]) is not int or work[k] < 0 for k in counters)
            or work["audit_calls"] > work["physical_calls"] or work["reference_calls"] > work["physical_calls"]):
        raise ValueError("invalid live repair work ledger")


def _validate_live_result(value, *, expected_job=None, replay=False):
    from .film_manufacturing_jobs import _json
    from .film_automatic_repair import LIVE_POLICY
    if (not isinstance(value, Mapping) or set(value) != {"schema_version", "kind", "request", "state", "is_final",
            "termination_reason", "result", "work"} or type(value.get("schema_version")) is not int
            or value["schema_version"] != 3 or value["kind"] != RESULT_KIND):
        raise ValueError("invalid live repair result envelope")
    if expected_job is not None and not result_matches_job(value, expected_job):
        raise ValueError("live repair result is not bound to its frozen job")
    request, source = request_from_payload(value["request"])
    body = value["result"]
    if (not isinstance(body, Mapping) or set(body) != {"source", "best", "reviewed", "detected_layer_ids", "lower_limit_nm",
            "policy", "work", "trace", "trace_total", "elapsed_seconds", "termination", "progress", "routes"}
            or body["policy"] != LIVE_POLICY or value["work"] != body["work"]
            or value["termination_reason"] != body["termination"]):
        raise ValueError("live repair result policy or evidence structure differs")
    if (any(type(body["policy"][k]) is not int for k in LIVE_POLICY)
            or isinstance(body["elapsed_seconds"], bool) or not isinstance(body["elapsed_seconds"], (int, float))
            or not math.isfinite(body["elapsed_seconds"]) or body["elapsed_seconds"] < 0):
        raise ValueError("live policy types or elapsed time are invalid")
    _validate_live_work(value["work"])
    reason = body["termination"]
    allowed = {"running", "cancelled", "resource_guard", "no_thin_layers", "frontier_exhausted", "no_effective_improvement",
               "local_stagnation", "thickness_goal_reached", "thickness_goal_reached_original_targets_unmet",
               "scheduler_cancel_won", "scheduler_failed"}
    state = ("running" if reason == "running" else "cancelled" if reason in {"cancelled", "scheduler_cancel_won"}
             else "failed" if reason == "scheduler_failed" else "completed")
    if reason not in allowed or value["state"] != state or value["is_final"] is not (reason != "running"):
        raise ValueError("live repair terminal state/reason differs")
    floor = lower_limit(value["request"]["reference_nm"], value["request"]["thickness_tolerance_nm"])
    if body["lower_limit_nm"] != floor or body["detected_layer_ids"] != deficient_ids(source, floor, include_zero=False):
        raise ValueError("live repair original layer detection differs")
    if body["source"] is not None:
        _validate_live_snapshot(body["source"], None, value["request"], request, source, is_source=True)
    for key in ("best", "reviewed"):
        if body[key] is not None:
            if body["source"] is None:
                raise ValueError("live repair candidate has no original audit")
            _validate_live_snapshot(body[key], body["source"], value["request"], request, source)
    if body["best"] is not None and not applicable_improvement(body["best"], body["source"], value["request"]):
        raise ValueError("live best is not a protected improvement")
    if (type(body["trace_total"]) is not int or body["trace_total"] < 0 or not isinstance(body["trace"], list)
            or len(body["trace"]) > LIVE_POLICY["trace_capacity"] or len(body["trace"]) > body["trace_total"]):
        raise ValueError("live repair rolling trace/cumulative count differs")
    if body["routes"] != [dict(route="automatic", champion=body["best"], status=reason)]:
        raise ValueError("live displayed champion differs from complete evidence")
    _validate_live_current(body["progress"]["current"], value["request"], request, source)
    if replay:
        _replay_live_result(value)
    return _json(value)


def _execute_live_job(parameters, cancel_event, report, checkpoint_path, write_json, **kwargs):
    from .film_automatic_repair import automatic_repair_live
    from .film_manufacturing_jobs import _expanded_json_sizes
    job = parameters["payload"]
    if set(job) != {"schema_version", "kind", "request"} or job["kind"] != JOB_KIND or job["schema_version"] != 3:
        raise ValueError("invalid live repair job")
    frozen, source = request_from_payload(job["request"])
    identity = _live_identity(job["request"])
    identity.pop("source_hash")
    external = kwargs.get("external_retained_bytes", lambda: 0)
    limit = (frozen.resource_guard_resolution or {}).get("effective_bytes")
    if limit is not None and estimate_resources(job["request"], frozen, source)["working_set_bytes"] + external() > limit:
        raise ValueError("live repair pre-admission exceeds existing resource guard")

    def envelope(result):
        reason = result["termination"]
        state = "running" if reason == "running" else "cancelled" if reason == "cancelled" else "completed"
        body = dict(result, routes=[dict(route="automatic", champion=result["best"], status=reason)])
        value = dict(schema_version=3, kind=RESULT_KIND, request=job["request"], state=state,
                     is_final=reason != "running", termination_reason=reason, result=body, work=body["work"])
        owned, encoded = _expanded_json_sizes(value)
        if limit is not None and 128 * 1024**2 + 4 * owned + 2 * encoded + external() > limit:
            raise ValueError("live repair checkpoint serialization exceeds the resource guard")
        return validate_result_payload(value, expected_job=job, replay=False)

    def save(result):
        write_json(checkpoint_path, dict(schema_version=3, kind="coat_sim.film_manufacturing_checkpoint",
            job=job, result=envelope(result)))

    result = automatic_repair_live(frozen, source, target_nm=job["request"]["reference_nm"],
        thickness_tolerance_nm=job["request"]["thickness_tolerance_nm"],
        optical_tolerance=job["request"]["optical_tolerance_percentage_points"] / 100.0,
        cancel_event=cancel_event, report=report, checkpoint=save, optical_settings=job["request"]["comparison_settings"],
        original_targets_present=job["request"]["original_targets_present"],
        external_retained_bytes=external, request_identity=identity)
    save(result)
    return envelope(result)


def _replay_live_result(value):
    """Explicit history replay, never used by cancellation or live progress."""
    from .film_auto_synthesis import _proxy
    from .film_optimization_structure import evaluate_frozen_structure_candidate
    from .film_synthesis_audit import audit_frozen_synthesis_candidate
    from .film_synthesis_manufacturing import manufacturing_snapshot
    from .film_manufacturing_jobs import _json
    from .film_automatic_repair import (
        CurvePair, _guard_request, curve_audit_metadata, _live_nominal,
        _live_mean_actual, original_protection_pass,
    )
    request, source = request_from_payload(value["request"])
    settings = value["request"]["comparison_settings"]
    original_present = value["request"]["original_targets_present"]
    source_snapshot = value["result"]["source"]
    tolerance = value["request"]["optical_tolerance_percentage_points"] / 100.0

    def physical(design, *, objectives=None, jacobian=False):
        active = request if objectives is None else replace(request, objectives=tuple(objectives))
        return evaluate_frozen_structure_candidate(_proxy(active), design, optical_settings=settings)

    seen = set()
    for snapshot in (value["result"]["source"], value["result"]["best"], value["result"]["reviewed"]):
        if snapshot is None or snapshot["physical_hash"] in seen:
            continue
        seen.add(snapshot["physical_hash"])
        raw = physical(snapshot["design"])
        original_pair = CurvePair(request, source, physical)

        def observe(objectives, incoming, index=None, owner=original_pair):
            if original_present and not _live_nominal(source_snapshot):
                owner.transform(objectives, incoming, index)
            return incoming

        audit = audit_frozen_synthesis_candidate(request, snapshot["design"], optical_settings=settings,
                                               response_transform=observe)
        checked = manufacturing_snapshot(request, dict(raw), audit_value=audit)
        if _json(checked.training_score) != _json(snapshot["training_score"]) or _json(checked.audit_score) != _json(snapshot["audit_score"]):
            raise ValueError("live persisted original evidence fails physical replay")
        if snapshot["design"] == source:
            continue
        guide = _guard_request(request, tolerance)
        pair = CurvePair(guide, source, physical)
        difference_audit = audit_frozen_synthesis_candidate(guide, snapshot["design"],
            optical_settings=settings, response_transform=pair.transform)
        guard = pair.evidence(tolerance)
        guard["audit"] = curve_audit_metadata(difference_audit)
        if _json(guard) != _json(snapshot["optical_guard"]):
            raise ValueError("live persisted paired optical evidence fails physical replay")
        if not original_present:
            protection = dict(kind="not_applicable", records=[], means=[], passed=True)
        elif _live_nominal(source_snapshot):
            protection = dict(kind="nominal_preserved", records=[], means=[], passed=_live_nominal(snapshot))
        else:
            combined: dict[tuple[int, float], dict[float, tuple[float, float]]] = {}
            for owner in (original_pair, pair):
                for key, points in owner.points.items():
                    combined.setdefault(key, {}).update(points)
            records = []
            for (index, angle), points in sorted(combined.items()):
                objective = request.objectives[index]
                if objective.pointwise.enabled:
                    waves = sorted(points)
                    records.append(dict(objective_index=index, angle_deg=angle, metric=objective.metric,
                        polarization=objective.polarization, wavelength_nm=waves,
                        candidate_values=[points[w][0] for w in waves], reference_values=[points[w][1] for w in waves]))
            means = [dict(scope=scope, objective_index=i, angle_deg=float(a),
                candidate_actual=_live_mean_actual(snapshot, scope, i, a),
                reference_actual=_live_mean_actual(source_snapshot, scope, i, a))
                for scope in ("training_score", "audit_score") for i, o in enumerate(request.objectives)
                if o.mean.enabled for a in o.angles_deg]
            protection = dict(kind="per_condition_nonincrease", records=records, means=means)
            temporary = dict(snapshot, protection_guard=protection)
            protection["passed"] = original_protection_pass(temporary, source_snapshot, request)
        if _json(protection) != _json(snapshot["protection_guard"]):
            raise ValueError("live persisted original-protection pairs fail physical replay")
