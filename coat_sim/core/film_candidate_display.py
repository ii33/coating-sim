"""Run-local, bounded display observations; never optimizer acceptance state.

The caller supplies complete original-grid scores and immutable condition rows.
Only the latest current/incumbent row references are retained, not designs,
Jacobians, audit grids or speculative work. Display failures cannot stop physics.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
import math
from time import monotonic
from typing import Mapping

from .film_nominal_acceptance import nominal_condition_pass


DISPLAY_KIND = "coat_sim.film_candidate_display"
MAX_DISPLAY_BYTES = 128 * 1024
MAX_SPECTRUM_POINTS = 1024


@dataclass(frozen=True)
class CandidateScoreConfig:
    weight_error: float = 90.0
    weight_layers: float = 5.0
    weight_thickness: float = 5.0
    layer_reference: float = 10.0
    thickness_reference_nm: float = 1000.0

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{field.name} must be finite and positive")
            try:
                number = float(value)
            except OverflowError as exc:
                raise ValueError(f"{field.name} must be finite and positive") from exc
            if not math.isfinite(number) or number <= 0:
                raise ValueError(f"{field.name} must be finite and positive")
            object.__setattr__(self, field.name, number)


def score_config_from_payload(value=None):
    if value is None:
        return CandidateScoreConfig()
    if isinstance(value, CandidateScoreConfig):
        return value
    if not isinstance(value, Mapping) or set(value) - {field.name for field in fields(CandidateScoreConfig)}:
        raise ValueError("invalid display score configuration fields")
    return CandidateScoreConfig(**value)


def score_config_to_payload(value=None):
    config = score_config_from_payload(value)
    return {field.name: float(getattr(config, field.name)) for field in fields(config)}


def score_candidate(*, weighted_rms_error, layer_count, total_thickness_nm,
                    nominal_pass, score_config=None):
    """Pure display convention, independent of every search/feasibility key."""
    config = score_config or CandidateScoreConfig()
    if isinstance(config, Mapping):
        config = CandidateScoreConfig(**config)
    if not isinstance(config, CandidateScoreConfig):
        raise ValueError("invalid display score configuration")
    error, thickness = float(weighted_rms_error), float(total_thickness_nm)
    if (isinstance(weighted_rms_error, bool) or not math.isfinite(error) or error < 0
            or type(layer_count) is not int or layer_count < 0
            or isinstance(total_thickness_nm, bool) or not math.isfinite(thickness) or thickness < 0
            or type(nominal_pass) is not bool):
        raise ValueError("display score requires complete finite physical metrics")
    # Normalize before summing so valid large positive weights cannot overflow.
    scale = max(config.weight_error, config.weight_layers, config.weight_thickness)
    weights = tuple(value / scale for value in
                    (config.weight_error, config.weight_layers, config.weight_thickness))
    denominator = math.fsum(weights)
    quality = math.fsum((weights[0] / (1.0 + error),
                        weights[1] / (1.0 + layer_count / config.layer_reference),
                        weights[2] / (1.0 + thickness / config.thickness_reference_nm))) / denominator
    return {"nominal_pass": nominal_pass, "quality": quality,
            "score": 50.0 * int(nominal_pass) + 50.0 * quality,
            "weighted_rms_error": error, "layer_count": layer_count,
            "total_thickness_nm": thickness}


def physical_layer_thicknesses(design, frozen_snapshot):
    """All persisted front/back rows in nm; zero rows count, ports do not."""
    from .film_optimization_needle import _physical_layer_thickness
    return {(side, row["id"]): _physical_layer_thickness(row, design, frozen_snapshot)
            for side in ("front_layers", "back_layers") for row in design.get(side, ())}


def _field(row, name):
    return row[name] if isinstance(row, Mapping) else getattr(row, name)


def candidate_physical_hash(design, thickness_values):
    """Display identity only: streaming canonical optics and physical-nm rows."""
    digest = hashlib.sha256()

    def write(value):
        if value is None:
            digest.update(b"n;")
        elif type(value) is bool:
            digest.update(b"t;" if value else b"f;")
        elif isinstance(value, (int, float)):
            digest.update(("d" + float(value or 0).hex() + ";").encode())
        elif isinstance(value, str):
            raw = value.encode("utf-8")
            digest.update(("s" + str(len(raw)) + ":").encode())
            digest.update(raw)
        elif isinstance(value, Mapping):
            digest.update(b"{")
            for key in sorted(value):
                write(key)
                write(value[key])
            digest.update(b"}")
        elif isinstance(value, (tuple, list)):
            digest.update(b"[")
            for item in value:
                write(item)
            digest.update(b"]")
        else:
            raise ValueError("unsupported physical identity value")

    for key in ("layer_order", "reference_wavelength_nm", "substrate", "incident_medium",
                "exit_medium", "assume_lossless", "first_surface", "use_k"):
        write(key)
        write(design.get(key))
    values = iter(thickness_values)
    for side in ("front_layers", "back_layers"):
        write(side)
        digest.update(b"[")
        for row in design.get(side, ()):
            digest.update(b"{")
            for key in sorted(row):
                if key in {"id", "note", "symbol", "group", "thickness"}:
                    continue
                write(key)
                write(row[key])
            write("thickness_nm")
            write(next(values))
            digest.update(b"}")
        digest.update(b"]")
    return digest.hexdigest()


def record_candidate(observer, *args, **kwargs):
    """Do not expose compute-owned references to arbitrary observer objects."""
    if type(observer) is CandidateDisplayObserver:
        CandidateDisplayObserver.record(observer, *args, **kwargs)


def finish_candidate_display(observer, *, state):
    if type(observer) is CandidateDisplayObserver:
        CandidateDisplayObserver.finish(observer, state=state)


def refresh_candidate_display(observer, *, spectrum_provider=None):
    if type(observer) is CandidateDisplayObserver:
        CandidateDisplayObserver.refresh(observer, spectrum_provider=spectrum_provider)


class CandidateDisplayObserver:
    """Optional observer with one-Hz lazy preview and an independent top ten.

    ``condition_rows`` must be the producer's complete immutable tuple of frozen
    FilmConditionError records. References remain valid after a newer incumbent
    is found, allowing the next due frame to show the true incumbent without
    copying full spectra between frames. Only recorded objective metrics exist;
    switching to an uncomputed condition reports waiting rather than recomputing.
    """

    def __init__(self, *, context_id, objectives, control_provider,
                 publish_callback, score_config=None, clock=monotonic,
                 scope="training", strict_lower_bound_objective_indices=(),
                 strict_upper_bound_objective_indices=()):
        if not isinstance(context_id, str) or not context_id or not isinstance(scope, str) or not scope:
            raise ValueError("display context and scope must be explicit")
        self.context_id, self.scope = context_id, scope
        self.objectives = tuple(objectives)
        self.score_config = (CandidateScoreConfig(**score_config) if isinstance(score_config, Mapping)
                             else score_config or CandidateScoreConfig())
        if not isinstance(self.score_config, CandidateScoreConfig):
            raise ValueError("invalid display score configuration")
        self.control_provider, self.publish_callback, self.clock = control_provider, publish_callback, clock
        self.strict_lower = frozenset(strict_lower_bound_objective_indices)
        self.strict_upper = frozenset(strict_upper_bound_objective_indices)
        self.expected_conditions = sum(o.wavelength_samples * len(o.angles_deg) for o in self.objectives)
        self._top = {}
        self._current = self._best = None
        self._last_publish = -math.inf
        self._sequence = 0
        self._finished = False
        self.last_error = ""
        self.metadata_only_packets = self.spectral_packets = 0
        self.spectral_snapshot_builds = self.spectral_points_copied = 0
        # Observer-local JSON encodes, excluding the separate transport layer.
        self.spectral_serializations = 0

    def _original_rows(self, rows):
        cursor = 0
        nominal = True
        for index, objective in enumerate(self.objectives):
            count = objective.wavelength_samples
            step = ((objective.wavelength_max_nm - objective.wavelength_min_nm) / (count - 1)
                    if count > 1 else 0.0)
            for angle in objective.angles_deg:
                for point in range(count):
                    row = rows[cursor]
                    cursor += 1
                    expected = (objective.wavelength_max_nm if point == count - 1 and count > 1
                                else objective.wavelength_min_nm + point * step)
                    actual_wave = _field(row, "wavelength_nm")
                    if (type(row) is not type(rows[0]) or _field(row, "objective_index") != index
                            or _field(row, "angle_deg") != angle or not _finite(actual_wave)
                            or abs(actual_wave - expected) > 4 * max(math.ulp(actual_wave), math.ulp(expected))
                            or any(_field(row, key) != getattr(objective, key) for key in
                                   ("polarization", "metric", "target_value", "lower_bound", "upper_bound"))
                            or not _finite(_field(row, "actual"))):
                        return None
                    if nominal:
                        nominal = nominal_condition_pass(objective, _field(row, "actual"),
                            strict_lower=index in self.strict_lower,
                            strict_upper=index in self.strict_upper)
        return nominal

    def record(self, design, *, weighted_rms_error, max_normalized_error,
               condition_rows, physical_thickness_nm, is_best=False,
               is_current=True, selection=None, iterations=0, evaluations=0,
               scope=None, source=None, guard_pass=None, sensitivity_objective=None,
               normalized_sensitivity_objective=None, usable_candidate=None, nominal_pass=None,
               condition_count=None, publish=True, spectrum_provider=None):
        """Observe only a complete, accepted normal candidate; never a probe.

        ``is_best`` is the optimizer's actual incumbent decision, not display
        rank. ``selection`` contains optional guard/robustness scalar facts.
        Invalid observations and delivery failures are isolated from the solver.
        """
        try:
            if self._finished or (scope is not None and scope != self.scope):
                return
            maximum = float(max_normalized_error)
            if not math.isfinite(maximum) or maximum < 0:
                return
            if condition_rows is None:
                # Complete batch scorers may retain only exact nominal/scalar
                # evidence. Missing spectra are explicitly unavailable, not 0.
                if (type(nominal_pass) is not bool or type(condition_count) is not int
                        or condition_count != self.expected_conditions):
                    return
                nominal = nominal_pass
            else:
                if (type(condition_rows) is not tuple or len(condition_rows) != self.expected_conditions
                        or not condition_rows):
                    return
                # Retain only immutable scalar records, never a mutable buffer.
                from .film_optimization import FilmConditionError
                if type(condition_rows[0]) is not FilmConditionError:
                    return
                # Repeated incumbent publication reuses the same frozen scalar
                # tuple. Its already checked grid/nominal facts cannot change.
                known = next((retained for retained in (self._current, self._best)
                              if retained is not None and retained[1] is condition_rows), None)
                nominal = known[0]["nominal_pass"] if known is not None else self._original_rows(condition_rows)
                if nominal is None:
                    return
            count = sum(len(design.get(side, ())) for side in ("front_layers", "back_layers"))
            thickness_values = ([physical_thickness_nm[(side, row["id"])]
                                 if (side, row["id"]) in physical_thickness_nm else physical_thickness_nm[row["id"]]
                                 for side in ("front_layers", "back_layers") for row in design.get(side, ())]
                                if isinstance(physical_thickness_nm, Mapping) else physical_thickness_nm)
            thickness_values = tuple(float(value) for value in thickness_values)
            if len(thickness_values) != count or any(not math.isfinite(v) or v < 0 for v in thickness_values):
                return
            summary = score_candidate(weighted_rms_error=weighted_rms_error, layer_count=count,
                total_thickness_nm=math.fsum(thickness_values), nominal_pass=nominal,
                score_config=self.score_config)
            summary.update(physical_hash=candidate_physical_hash(design, thickness_values),
                           max_normalized_error=maximum, iterations=int(iterations),
                           evaluations=int(evaluations), apply_eligible=False,
                           condition_count=self.expected_conditions,
                           expected_condition_count=self.expected_conditions)
            summary["candidate_id"] = summary["physical_hash"]
            for key in ("guard_pass", "robustness_value", "objective_value", "solver_acceptable"):
                value = (selection or {}).get(key)
                if type(value) is bool or (isinstance(value, (int, float)) and math.isfinite(value)):
                    summary[key] = value
            extras = {"guard_pass": guard_pass, "sensitivity_objective": sensitivity_objective,
                      "normalized_sensitivity_objective": normalized_sensitivity_objective,
                      "usable_candidate": usable_candidate}
            for key, value in extras.items():
                if type(value) is bool or (isinstance(value, (int, float)) and math.isfinite(value)):
                    summary[key] = value
            if isinstance(source, str) and len(source) <= 64:
                summary["source"] = source
            summary["candidate_eligible"] = nominal and summary.get("guard_pass", True)
            retained = (summary, condition_rows)
            if is_current:
                self._current = retained
            if is_best:
                self._best = retained
            self._top[summary["physical_hash"]] = summary
            ordered = sorted(self._top.values(), key=self._rank)[:10]
            self._top = {row["physical_hash"]: row for row in ordered}
            if publish:
                self._publish(spectrum_provider=spectrum_provider)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _rank(summary):
        return (not summary["nominal_pass"], -summary["quality"], summary["physical_hash"])

    def _slot(self, retained, selection, limit, *, spectrum_provider=None):
        if retained is None:
            return None
        summary, rows = retained
        result = dict(summary)
        if rows is None:
            spectrum = spectrum_provider(selection, limit) if spectrum_provider is not None else None
            if spectrum is not None:
                self.spectral_snapshot_builds += 1
                self.spectral_points_copied += len(spectrum.get("actual", ()))
            result.update(availability="waiting" if spectrum is None else "available", spectrum=spectrum)
            return result
        objective_index = selection["objective_index"]
        objective = self.objectives[objective_index]
        if selection["angle_deg"] not in objective.angles_deg or selection["metric"] != objective.metric:
            result.update(availability="waiting", spectrum=None)
            return result
        count = objective.wavelength_samples
        offset = sum(o.wavelength_samples * len(o.angles_deg) for o in self.objectives[:objective_index])
        offset += objective.angles_deg.index(selection["angle_deg"]) * count
        indices = (list(range(count)) if count <= limit else
                   [i * (count - 1) // (limit - 1) for i in range(limit)])
        result.update(availability="available", spectrum={
            "wavelength_nm": [_field(rows[offset + i], "wavelength_nm") for i in indices],
            "actual": [_field(rows[offset + i], "actual") for i in indices],
            "indices": indices, "original_point_count": count})
        self.spectral_snapshot_builds += 1
        self.spectral_points_copied += len(indices)
        return result

    def _publish(self, *, terminal=False, state="running", spectrum_provider=None):
        control = self.control_provider() or {}
        enabled = control.get("enabled", False) is True
        now = self.clock()
        if not terminal and now - self._last_publish < 1.0:
            return
        # Reserve the attempt before sampling/serialization, including failures.
        self._last_publish = now
        index = int(control.get("objective_index", 0))
        if not 0 <= index < len(self.objectives):
            return
        objective = self.objectives[index]
        selection = {"objective_index": index,
                     "angle_deg": float(control.get("angle_deg", objective.angles_deg[0])),
                     "metric": str(control.get("metric", objective.metric))}
        self._sequence += 1
        packet = {"schema_version": 1, "kind": DISPLAY_KIND,
                  "context_id": self.context_id, "scope": self.scope,
                  "sequence": self._sequence, "enabled": enabled,
                  "selection": selection, "terminal": terminal, "state": state,
                  "top10": [dict(row) for row in sorted(self._top.values(), key=self._rank)]}
        limit = MAX_SPECTRUM_POINTS
        while True:
            # The one terminal bypass samples only the already retained real
            # incumbent. Last-trial metadata remains available without work.
            if terminal or not enabled:
                packet["current"] = (None if self._current is None else
                    dict(self._current[0], availability="waiting", spectrum=None))
            else:
                packet["current"] = self._slot(
                    self._current, selection, limit, spectrum_provider=spectrum_provider)
            packet["best"] = (None if self._best is None else
                dict(self._best[0], availability="waiting", spectrum=None)) if not enabled else self._slot(self._best, selection, limit)
            has_spectra = any(packet[role] is not None and packet[role]["spectrum"] is not None
                              for role in ("current", "best"))
            self.spectral_serializations += int(has_spectra)
            if len(json.dumps(packet, allow_nan=False, separators=(",", ":")).encode("utf-8")) <= MAX_DISPLAY_BYTES:
                break
            if limit <= 2:
                return
            limit = max(2, limit // 2)
        checked = validate_candidate_display_packet(packet)
        self.spectral_serializations += int(has_spectra)
        if checked is None:
            return
        self.publish_callback(checked)
        self.spectral_packets += int(has_spectra)
        self.metadata_only_packets += int(not has_spectra)

    def refresh(self, *, spectrum_provider=None):
        """Consume a short-lived existing raw source only after the time gate.

        The provider is never retained. It must only index existing raw arrays,
        returning bounded detached samples, and may not perform physical work.
        """
        if self._finished:
            return
        try:
            self._publish(spectrum_provider=spectrum_provider)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"

    def finish(self, *, state="completed"):
        if self._finished:
            return
        try:
            self._publish(terminal=True, state=state)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._finished = True


_SUMMARY_REQUIRED = frozenset({"physical_hash", "nominal_pass", "quality", "score",
    "weighted_rms_error", "max_normalized_error", "layer_count", "total_thickness_nm",
    "iterations", "evaluations", "apply_eligible", "candidate_eligible", "candidate_id",
    "condition_count", "expected_condition_count"})
_SUMMARY_OPTIONAL = frozenset({"source", "guard_pass", "robustness_value", "objective_value",
    "solver_acceptable", "sensitivity_objective", "normalized_sensitivity_objective", "usable_candidate"})


def validate_candidate_display_packet(value):
    """Return a detached closed-schema packet, or None for untrusted bad data."""
    try:
        keys = {"schema_version", "kind", "context_id", "scope", "sequence", "enabled",
                "selection", "terminal", "state", "top10", "current", "best"}
        if not isinstance(value, dict) or set(value) != keys:
            return None
        if (type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["kind"] != DISPLAY_KIND or type(value["enabled"]) is not bool
                or type(value["terminal"]) is not bool or type(value["sequence"]) is not int
                or value["sequence"] < 1):
            return None
        for key, limit in (("context_id", 256), ("scope", 64), ("state", 100)):
            if not isinstance(value[key], str) or not 1 <= len(value[key]) <= limit:
                return None
        chosen = value["selection"]
        if (not isinstance(chosen, dict) or set(chosen) != {"objective_index", "angle_deg", "metric"}
                or type(chosen["objective_index"]) is not int or chosen["objective_index"] < 0
                or chosen["metric"] not in ("R", "T") or not _finite(chosen["angle_deg"])):
            return None

        def summary(row, *, slot=False):
            extra = {"availability", "spectrum"} if slot else set()
            if (not isinstance(row, dict) or not _SUMMARY_REQUIRED <= set(row)
                    or set(row) - _SUMMARY_REQUIRED - _SUMMARY_OPTIONAL - extra
                    or (slot and not extra <= set(row))):
                return False
            identity = row["physical_hash"]
            if not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
                return False
            if row["candidate_id"] != identity:
                return False
            for key in ("nominal_pass", "apply_eligible", "candidate_eligible"):
                if type(row[key]) is not bool:
                    return False
            if row["apply_eligible"]:
                return False  # A display packet never grants application authority.
            if row["candidate_eligible"] != (row["nominal_pass"] and row.get("guard_pass", True)):
                return False
            for key in ("layer_count", "iterations", "evaluations", "condition_count", "expected_condition_count"):
                if type(row[key]) is not int or row[key] < 0:
                    return False
            if row["condition_count"] != row["expected_condition_count"] or row["condition_count"] < 1:
                return False
            for key in ("quality", "score", "weighted_rms_error", "max_normalized_error", "total_thickness_nm"):
                if not _finite(row[key]) or row[key] < 0:
                    return False
            if row["quality"] > 1 or row["score"] > 100:
                return False
            if not math.isclose(row["score"], 50 * int(row["nominal_pass"]) + 50 * row["quality"], rel_tol=0, abs_tol=1e-12):
                return False
            for key in ("guard_pass", "solver_acceptable", "usable_candidate"):
                if key in row and type(row[key]) is not bool:
                    return False
            for key in ("robustness_value", "objective_value", "sensitivity_objective", "normalized_sensitivity_objective"):
                if key in row and (not _finite(row[key]) or row[key] < 0):
                    return False
            if "source" in row and (not isinstance(row["source"], str) or len(row["source"]) > 64):
                return False
            if not slot:
                return True
            spectrum = row["spectrum"]
            if row["availability"] == "waiting":
                return spectrum is None
            if row["availability"] != "available" or not isinstance(spectrum, dict) or set(spectrum) != {"wavelength_nm", "actual", "indices", "original_point_count"}:
                return False
            count = spectrum["original_point_count"]
            waves, actual, indices = (spectrum[key] for key in ("wavelength_nm", "actual", "indices"))
            if (type(count) is not int or count < 1 or not all(type(items) is list for items in (waves, actual, indices))
                    or not 1 <= len(waves) <= MAX_SPECTRUM_POINTS or len(waves) != len(actual) or len(waves) != len(indices)
                    or not all(_finite(n) for n in waves + actual)
                    or not all(type(i) is int and 0 <= i < count for i in indices)
                    or any(b <= a for a, b in zip(indices, indices[1:], strict=False))
                    or any(b < a for a, b in zip(waves, waves[1:], strict=False))):
                return False
            return True

        top = value["top10"]
        if type(top) is not list or len(top) > 10 or not all(summary(row) for row in top):
            return None
        if (len({row["physical_hash"] for row in top}) != len(top)
                or top != sorted(top, key=CandidateDisplayObserver._rank)):
            return None
        if any(value[role] is not None and not summary(value[role], slot=True) for role in ("current", "best")):
            return None
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_DISPLAY_BYTES:
            return None
        return json.loads(encoded)
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        return None


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
