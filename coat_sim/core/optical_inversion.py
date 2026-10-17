"""Bounded single-layer optical-constant estimation from absolute specular R/T.

This is a pure preview API: no files, material records or project state are
written. Thickness, substrate and measurement geometry are supplied, never fit
or silently inferred. All forward calls rebuild the varying film index and use
the established coherent/incoherent film-optics solvers, without frozen-index
caches. A small residual does not establish unique or experimentally true n/k.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
import json
import math
from typing import Any, NoReturn

import numpy as np
from scipy.optimize import least_squares  # type: ignore[import-untyped]

from coat_sim.core.film_optics import coherent_rta, incoherent_plate_rta
from coat_sim.core.optical_constants import prepare_optical_set


HC_EV_NM = 1239.8419843320026
MAX_SPECTRA = 16
MAX_SAMPLES = 4096
_MODELS: dict[str, dict[str, Any]] = {
    "cauchy": {
        "names": ("A", "B", "C"),
        "bounds": ((1.0, 4.0), (0.0, 0.3), (0.0, 0.03)),
        "initial": (1.8, 0.005, 0.0001),
        "units": ("1", "um^2", "um^4"),
        "formula": "n=A+B/lambda_um^2+C/lambda_um^4; k=0 (declared transparent band)",
    },
    "lorentz": {
        "names": ("eps_inf", "strength_ev2", "resonance_ev", "damping_ev"),
        "bounds": ((1.0, 16.0), (0.0, 100.0), (0.2, 12.0), (0.01, 5.0)),
        "initial": (3.0, 10.0, 4.0, 0.3),
        "units": ("1", "eV^2", "eV", "eV"),
        "formula": "epsilon=eps_inf+strength_ev2/(resonance_ev^2-E_eV^2-i*damping_ev*E_eV); N=sqrt(epsilon)",
    },
}


class _InvalidRequest(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Cancelled(Exception):
    pass


class _EvaluationBudgetReached(Exception):
    pass


@dataclass
class _Spectrum:
    original: dict[str, Any]
    wave_nm: np.ndarray
    measured: np.ndarray
    sigma: np.ndarray
    substrate: np.ndarray
    angle: float
    polarization: str
    quantity: str


@dataclass
class _Problem:
    request: dict[str, Any]
    model: str
    names: tuple[str, ...]
    low: np.ndarray
    high: np.ndarray
    initial: np.ndarray
    spectra: list[_Spectrum]
    structure: dict[str, Any]
    options: dict[str, Any]
    warnings: list[dict[str, str]]


def _fail(code: str, message: str) -> NoReturn:
    raise _InvalidRequest(code, message)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        _fail("invalid_" + name, name + " must be an object.")
    return dict(value)


def _number(value: Any, name: str, *, low: float | None = None,
            high: float | None = None, positive: bool = False) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.number)):
        _fail("invalid_number", name + " must be a finite number.")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        _fail("invalid_number", name + " must be a finite number.")
    if (not math.isfinite(result) or positive and result <= 0
            or low is not None and result < low or high is not None and result > high):
        _fail("invalid_number", name + " is outside its supported finite range.")
    return result


def _integer(value: Any, name: str, low: int, high: int) -> int:
    number = _number(value, name, low=low, high=high)
    if number != int(number):
        _fail("invalid_option", name + " must be an integer.")
    return int(number)


def _array(value: Any, name: str, *, minimum: int = 2) -> np.ndarray:
    if not isinstance(value, (list, tuple)) or not minimum <= len(value) <= MAX_SAMPLES:
        _fail("invalid_samples", name + " must be a bounded numeric sample list.")
    return np.asarray([_number(item, name) for item in value], dtype=float)


def _text(value: Any, name: str, cap: int = 2000) -> str:
    if not isinstance(value, str) or len(value) > cap or "\x00" in value:
        _fail("invalid_text", name + " must be bounded text.")
    return value


def _unknown_fields(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    if set(value) - allowed:
        _fail("unsupported_fields", name + " contains unsupported fields: "
              + ", ".join(sorted(set(value) - allowed)))


def _warning(code: str, message: str, severity: str = "warning") -> dict[str, str]:
    return {"code": code, "message": message, "severity": severity}


def _cancel(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise _Cancelled


def _validate(request: Mapping[str, Any], cancelled: Callable[[], bool] | None) -> _Problem:
    raw = _mapping(request, "request")
    _unknown_fields(raw, {"model", "transparent_region", "film_thickness_nm", "substrate",
                          "geometry", "spectra", "parameter_bounds", "initial_parameters",
                          "optimization", "source", "name"}, "request")
    model = raw.get("model")
    if model not in _MODELS:
        _fail("unsupported_model", "Choose cauchy or lorentz; arbitrary pointwise n/k fitting is unsupported.")
    if "transparent_region" in raw and type(raw["transparent_region"]) is not bool:
        _fail("invalid_transparency", "transparent_region must be an explicit boolean.")
    if model == "cauchy" and raw.get("transparent_region") is not True:
        _fail("transparency_required", "Cauchy requires an explicitly declared transparent measurement band (k=0).")
    thickness = _number(raw.get("film_thickness_nm"), "film_thickness_nm", positive=True, high=1e6)
    geometry = _mapping(raw.get("geometry"), "geometry")
    _unknown_fields(geometry, {"backside", "incident_n", "exit_n"}, "geometry")
    backside = geometry.get("backside")
    if backside not in {"incoherent", "coherent", "semi_infinite"}:
        _fail("backside_required", "Explicitly choose incoherent, coherent or semi_infinite substrate geometry.")
    incident = _number(geometry.get("incident_n", 1.0), "incident_n", positive=True, high=10)
    exit_n = _number(geometry.get("exit_n", 1.0), "exit_n", positive=True, high=10)
    substrate = _mapping(raw.get("substrate"), "substrate")
    _unknown_fields(substrate, {"n", "k", "assume_lossless", "thickness_nm", "optical_set", "source"}, "substrate")
    if "source" in substrate:
        _text(substrate["source"], "substrate.source")
    assume_lossless = substrate.get("assume_lossless", False)
    if type(assume_lossless) is not bool:
        _fail("invalid_lossless_assumption", "assume_lossless must be an explicit boolean.")
    substrate_thickness = None
    if backside != "semi_infinite":
        substrate_thickness = _number(substrate.get("thickness_nm"), "substrate.thickness_nm",
                                      positive=True, high=1e9)
    elif "thickness_nm" in substrate and substrate["thickness_nm"] is not None:
        _number(substrate["thickness_nm"], "substrate.thickness_nm", positive=True, high=1e9)
    warnings = [_warning("non_unique", "This is a model-dependent estimate. A small R/T residual, including R+T, does not establish unique n/k or validate the model."),
                _warning("fixed_thickness", "Film thickness and substrate are fixed inputs. Their errors can bias n/k; thickness uncertainty is not fitted or propagated.")]
    evaluator = None
    scalar_substrate = None
    lossless_assumption_used = False
    if "optical_set" in substrate:
        if "n" in substrate or "k" in substrate:
            _fail("ambiguous_substrate", "Supply either a substrate optical_set or scalar n/k, not both.")
        try:
            evaluator = prepare_optical_set(substrate["optical_set"], k_sampling_policy="source")
        except (ValueError, TypeError, KeyError, OverflowError):
            _fail("invalid_substrate", "The substrate optical dataset is invalid.")
    else:
        n = _number(substrate.get("n"), "substrate.n", positive=True, high=100)
        if substrate.get("k") is None:
            if not assume_lossless:
                _fail("unknown_substrate_k", "Substrate k is unknown. Provide it or explicitly select assume_lossless.")
            k = 0.0
            lossless_assumption_used = True
        else:
            k = _number(substrate["k"], "substrate.k", low=0, high=100)
        scalar_substrate = complex(n, k)
    if backside == "semi_infinite":
        warnings.append(_warning("semi_infinite_substrate", "Backside and substrate propagation are excluded by explicit choice. T means flux entering the substrate, not transmission outside a finite plate."))
    if backside == "coherent":
        warnings.append(_warning("coherent_substrate_resolution", "Finite substrate fringes are modeled coherently at the raw wavelengths; spectral resolution, averaging and partial coherence are not modeled."))
    records = raw.get("spectra")
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_SPECTRA:
        _fail("invalid_spectra", f"Supply 1–{MAX_SPECTRA} independent R/T spectra.")
    spectra = []
    total_samples = 0
    used_ids: set[str] = set()
    for index, item in enumerate(records):
        _cancel(cancelled)
        record = _mapping(item, "spectrum")
        _unknown_fields(record, {"id", "quantity", "wavelengths", "wavelength_unit", "values",
                                "value_unit", "angle_deg", "polarization", "measurement",
                                "transmission_definition", "source", "sigma"}, "spectrum")
        identity = _text(record.get("id", str(index + 1)), "spectrum.id", 100)
        if not identity or identity in used_ids:
            _fail("duplicate_spectrum_id", "Spectrum IDs must be nonempty and unique.")
        used_ids.add(identity)
        if record.get("measurement") != "absolute_specular":
            _fail("unsupported_measurement", "Only absolute specular R/T is supported; bare-substrate-normalized transmission and total/scattered light are not interchangeable.")
        quantity = record.get("quantity")
        if quantity not in {"R", "T"}:
            _fail("invalid_quantity", "Each spectrum must measure R or T.")
        if record.get("wavelength_unit") not in {"nm", "um"} or record.get("value_unit") not in {"fraction", "%"}:
            _fail("invalid_units", "Explicit wavelength nm/um and value fraction/% units are required.")
        wave = _array(record.get("wavelengths"), "wavelengths")
        measured = _array(record.get("values"), "values")
        if len(wave) != len(measured):
            _fail("sample_length_mismatch", "Wavelength and measured-value counts must agree.")
        wave = wave * (1000 if record["wavelength_unit"] == "um" else 1)
        if np.any(wave <= 0) or np.any(np.diff(wave) <= 0) or np.any(wave > 1e9):
            _fail("invalid_wavelength_grid", "Each original spectrum must have positive strictly increasing wavelengths; no sorting or resampling is performed.")
        measured = measured / (100 if record["value_unit"] == "%" else 1)
        if np.any(measured < 0) or np.any(measured > 1):
            _fail("nonphysical_measurement", "Absolute specular R/T must be between zero and one (or 0–100%).")
        angle = _number(record.get("angle_deg"), "angle_deg", low=0, high=85)
        polarization = record.get("polarization")
        if polarization not in {"s", "p", "unpolarized"}:
            _fail("invalid_polarization", "Explicit s, p or unpolarized polarization is required.")
        if quantity == "T":
            definition = record.get("transmission_definition")
            expected = "into_substrate" if backside == "semi_infinite" else "external"
            if definition != expected:
                _fail("transmission_geometry_mismatch", "T must explicitly use " + expected + " for this substrate geometry.")
        elif "transmission_definition" in record and record["transmission_definition"] not in {"external", "into_substrate"}:
            _fail("invalid_transmission_definition", "Transmission definition is unsupported.")
        _text(record.get("source", ""), "spectrum.source")
        sigma = np.ones_like(measured)
        if "sigma" in record:
            supplied = record["sigma"]
            if isinstance(supplied, (list, tuple)):
                sigma = _array(supplied, "sigma")
                if len(sigma) != len(wave):
                    _fail("sigma_length_mismatch", "Uncertainties must match their spectrum samples.")
            else:
                sigma = np.full_like(measured, _number(supplied, "sigma", positive=True))
            sigma = sigma / (100 if record["value_unit"] == "%" else 1)
            if np.any(sigma <= 1e-12) or np.any(sigma > 1):
                _fail("invalid_sigma", "Absolute one-sigma uncertainties must be in (1e-12,1] after unit conversion.")
        if evaluator is None:
            sampled_substrate = np.full(wave.size, scalar_substrate, dtype=complex)
        else:
            constants = []
            for point in wave:
                _cancel(cancelled)
                try:
                    nr, kr, _details = evaluator(float(point), "nm", extrapolation_policy="error")
                except (ValueError, TypeError, OverflowError):
                    _fail("substrate_out_of_range", "Substrate n/k must cover every raw measurement wavelength; extrapolation is forbidden.")
                if kr is None:
                    if not assume_lossless:
                        _fail("unknown_substrate_k", "Substrate k is unknown; an explicit assume_lossless choice is required.")
                    kr = 0.0
                    lossless_assumption_used = True
                constants.append(complex(nr, kr))
            sampled_substrate = np.asarray(constants, dtype=complex)
        beta = incident * math.sin(math.radians(angle))
        if backside == "incoherent" and (np.any(beta >= sampled_substrate.real) or beta >= exit_n):
            _fail("unsupported_incoherent_geometry", "Incoherent substrate/exit ports must propagate. Evanescent coupling requires a separately justified coherent model.")
        original = deepcopy(record)
        original["id"] = identity
        spectra.append(_Spectrum(original, wave, measured, sigma, sampled_substrate,
                                 angle, polarization, quantity))
        total_samples += wave.size
        if total_samples > MAX_SAMPLES:
            _fail("sample_budget_exceeded", f"At most {MAX_SAMPLES} total measured samples are supported in this preview.")
    unique_waves = np.unique(np.concatenate([s.wave_nm for s in spectra]))
    if lossless_assumption_used:
        warnings.append(_warning("substrate_lossless_assumption", "Missing substrate k is explicitly assumed zero; this is an assumption, not measured absorption data."))
    if unique_waves.size < 2:
        _fail("insufficient_wavelengths", "At least two distinct wavelengths are required.")
    spec = _MODELS[model]
    names = spec["names"]
    low = np.asarray([bound[0] for bound in spec["bounds"]], dtype=float)
    high = np.asarray([bound[1] for bound in spec["bounds"]], dtype=float)
    limits = _mapping(raw.get("parameter_bounds", {}), "parameter_bounds")
    _unknown_fields(limits, set(names), "parameter_bounds")
    for index, name in enumerate(names):
        if name in limits:
            bound = _array(limits[name], "parameter_bounds", minimum=2)
            if len(bound) != 2 or not low[index] <= bound[0] < bound[1] <= high[index]:
                _fail("invalid_parameter_bounds", name + " bounds must tighten the documented model limits and have positive width.")
            low[index], high[index] = bound
    guess = _mapping(raw.get("initial_parameters", {}), "initial_parameters")
    _unknown_fields(guess, set(names), "initial_parameters")
    initial = np.asarray(spec["initial"], dtype=float)
    initial = np.clip(initial, low, high)
    for index, name in enumerate(names):
        if name in guess:
            initial[index] = _number(guess[name], name, low=low[index], high=high[index])
    options = _mapping(raw.get("optimization", {}), "optimization")
    _unknown_fields(options, {"multistarts", "max_nfev", "seed", "acceptance_rmse"}, "optimization")
    options = {"multistarts": _integer(options.get("multistarts", 4), "multistarts", 2, 8),
               "max_nfev": _integer(options.get("max_nfev", 250), "max_nfev", 10, 1000),
               "seed": _integer(options.get("seed", 0), "seed", 0, 2**32 - 1),
               "acceptance_rmse": _number(options.get("acceptance_rmse", 0.01), "acceptance_rmse", positive=True, high=0.2)}
    if "source" in raw:
        _text(raw["source"], "source")
    if "name" in raw:
        _text(raw["name"], "name", 120)
    # Request is deliberately a finite JSON document so provenance can be saved
    # by the caller without implicit serialization of objects or executable code.
    try:
        saved_request = json.loads(json.dumps(raw, allow_nan=False))
    except (ValueError, TypeError, OverflowError):
        _fail("invalid_provenance", "Request metadata must be finite JSON.")
    structure = {"film_thickness_nm": thickness, "substrate": deepcopy(saved_request["substrate"]),
                 "backside": backside, "substrate_thickness_nm": substrate_thickness,
                 "incident_n": incident, "exit_n": exit_n,
                 "front_layers": 1, "back_layers": 0, "isotropic": True, "homogeneous": True}
    return _Problem(saved_request, model, names, low, high, initial, spectra,
                    structure, options, warnings)


def _index(model: str, parameters: np.ndarray, wavelength_nm: np.ndarray) -> np.ndarray:
    if model == "cauchy":
        wavelength_um = wavelength_nm * 0.001
        a, b, c = parameters
        return (a + b / wavelength_um**2 + c / wavelength_um**4).astype(complex)
    eps_inf, strength, resonance, damping = parameters
    energy = HC_EV_NM / wavelength_nm
    epsilon = eps_inf + strength / (resonance**2 - energy**2 - 1j * damping * energy)
    return np.sqrt(epsilon)


def _predict(problem: _Problem, parameters: np.ndarray,
             cancelled: Callable[[], bool] | None) -> list[np.ndarray]:
    structure = problem.structure
    predictions = []
    for spectrum in problem.spectra:
        _cancel(cancelled)
        film = _index(problem.model, parameters, spectrum.wave_nm)
        if not np.all(np.isfinite(film)) or np.any(film.real <= 0) or np.any(film.imag < 0):
            raise ValueError("nonphysical_model_index")
        count = spectrum.wave_nm.size
        incident = np.full(count, structure["incident_n"], dtype=complex)
        if structure["backside"] == "incoherent":
            result = incoherent_plate_rta(
                spectrum.wave_nm, incident, spectrum.substrate, structure["exit_n"],
                front_indices=film[:, None], front_thickness_nm=[structure["film_thickness_nm"]],
                substrate_thickness_nm=structure["substrate_thickness_nm"],
                angle_deg=spectrum.angle, polarization=spectrum.polarization)
        else:
            if structure["backside"] == "coherent":
                exit_index = np.full(count, structure["exit_n"], dtype=complex)
                indices = np.column_stack((incident, film, spectrum.substrate, exit_index))
                thicknesses = [structure["film_thickness_nm"], structure["substrate_thickness_nm"]]
            else:
                indices = np.column_stack((incident, film, spectrum.substrate))
                thicknesses = [structure["film_thickness_nm"]]
            result = coherent_rta(spectrum.wave_nm, indices, thicknesses,
                                  angle_deg=spectrum.angle, polarization=spectrum.polarization)
        prediction = np.asarray(result[spectrum.quantity], dtype=float)
        if not np.all(np.isfinite(prediction)):
            raise ValueError("nonfinite_prediction")
        predictions.append(prediction)
    _cancel(cancelled)
    return predictions


def _rmse(residual: np.ndarray) -> float:
    return float(np.sqrt(np.mean(residual**2)))


def fit_optical_constants(request: Mapping[str, Any], *,
                          cancelled: Callable[[], bool] | None = None,
                          progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Estimate a bounded shared dispersion model, returning an unsaved preview.

    Cancellation is checked during validation, every residual call, each forward
    spectrum and final assembly. It returns ``status='cancelled'`` with no partial
    optical dataset. Progress receives plain dictionaries on the calling thread.
    Input/serialization failures are returned as ``invalid_request``. The caller
    must save full provenance separately before applying the candidate dataset.
    """
    try:
        _cancel(cancelled)
        try:
            problem = _validate(request, cancelled)
        except _InvalidRequest:
            raise
        except (ValueError, TypeError, OverflowError, KeyError):
            return {"ok": False, "status": "invalid_request", "error": "invalid_request",
                    "message": "The measurement request contains an invalid or unsupported value."}
        rng = np.random.default_rng(problem.options["seed"])
        span = problem.high - problem.low
        first = np.clip((problem.initial - problem.low) / span, 1e-8, 1 - 1e-8)
        starts = [first]
        while len(starts) < problem.options["multistarts"]:
            if len(starts) == 1:
                trial = first + rng.normal(0, 0.08, len(first))
            else:
                trial = rng.uniform(0.02, 0.98, len(first))
            starts.append(np.clip(trial, 1e-8, 1 - 1e-8))
        runs: list[dict[str, Any]] = []
        candidates: list[tuple[Any, np.ndarray, list[np.ndarray], float]] = []
        evaluations = 0
        for start_index, start in enumerate(starts):
            _cancel(cancelled)
            calls = 0
            residual_limit = (len(problem.names) + 1) * (problem.options["max_nfev"] + 1)

            def residual(scaled: np.ndarray, current_start: int = start_index,
                         call_limit: int = residual_limit) -> np.ndarray:
                nonlocal calls, evaluations
                _cancel(cancelled)
                if calls >= call_limit:
                    raise _EvaluationBudgetReached
                parameters = problem.low + scaled * span
                predictions = _predict(problem, parameters, cancelled)
                values = np.concatenate([(prediction - spectrum.measured) / spectrum.sigma
                                         for prediction, spectrum in zip(predictions, problem.spectra, strict=True)])
                calls += 1
                evaluations += 1
                if progress is not None and (calls == 1 or calls % 10 == 0):
                    progress({"stage": "fitting", "start": current_start + 1,
                              "starts": len(starts), "evaluations": evaluations,
                              "weighted_rmse": _rmse(values)})
                _cancel(cancelled)
                return values

            try:
                solution = least_squares(residual, start, bounds=(np.zeros_like(start),
                                         np.ones_like(start)), method="trf", x_scale="jac",
                                         max_nfev=problem.options["max_nfev"],
                                         ftol=1e-10, xtol=1e-10, gtol=1e-10)
                parameters = problem.low + solution.x * span
                predictions = _predict(problem, parameters, cancelled)
                difference = np.concatenate([prediction - spectrum.measured
                                             for prediction, spectrum in zip(predictions, problem.spectra, strict=True)])
                cost = float(np.mean(solution.fun**2))
                rms = _rmse(difference)
                candidates.append((solution, parameters, predictions, cost))
                runs.append({"start": start_index + 1, "converged": bool(solution.success),
                             "status": int(solution.status), "nfev": int(solution.nfev),
                             "residual_evaluations": calls, "rmse": rms,
                             "weighted_rmse": math.sqrt(cost),
                             "parameters": dict(zip(problem.names, parameters.tolist(), strict=True))})
            except _EvaluationBudgetReached:
                runs.append({"start": start_index + 1, "converged": False,
                             "status": -1, "error": "residual_budget_exhausted",
                             "residual_evaluations": calls})
            except (ValueError, FloatingPointError, np.linalg.LinAlgError):
                runs.append({"start": start_index + 1, "converged": False,
                             "status": -1, "error": "forward_or_solver_failed",
                             "residual_evaluations": calls})
            if progress is not None:
                progress({"stage": "start_completed", "start": start_index + 1,
                          "starts": len(starts), "evaluations": evaluations})
        _cancel(cancelled)
        if not candidates:
            return {"ok": False, "status": "failed", "error": "no_valid_solution",
                    "message": "No bounded model could be evaluated for this measurement structure.",
                    "diagnostics": {"multistarts": runs}}
        candidates.sort(key=lambda entry: entry[3])
        solution, parameters, predictions, _cost = candidates[0]
        result = _assemble(problem, solution, parameters, predictions, runs, candidates, evaluations)
        _cancel(cancelled)
        if progress is not None:
            progress({"stage": "completed", "evaluations": evaluations, "rmse": result["rmse"]})
        _cancel(cancelled)
        # Public outputs must never contain NaN/Infinity (e.g. Jacobian condition).
        json.dumps(result, allow_nan=False)
        return result
    except _Cancelled:
        return {"ok": False, "status": "cancelled", "error": "cancelled",
                "message": "Optical inversion cancelled; no candidate dataset was saved."}
    except _InvalidRequest as exc:
        return {"ok": False, "status": "invalid_request", "error": exc.code, "message": str(exc)}
    except (ValueError, TypeError, OverflowError, np.linalg.LinAlgError):
        return {"ok": False, "status": "failed", "error": "numerical_failure",
                "message": "The requested model could not be fitted with finite results."}


def _assemble(problem: _Problem, solution: Any, parameters: np.ndarray,
              predictions: list[np.ndarray], runs: list[dict[str, Any]],
              candidates: list[tuple[Any, np.ndarray, list[np.ndarray], float]],
              evaluations: int) -> dict[str, Any]:
    warnings = list(problem.warnings)
    channels = {spectrum.quantity for spectrum in problem.spectra}
    if channels == {"R"}:
        warnings.append(_warning("reflectance_only", "Reflectance-only fitting is strongly underconstrained for general n/k. This is only a bounded-model estimate; independent T/angles/polarizations and thickness checks are recommended.", "strong"))
    elif channels == {"T"}:
        warnings.append(_warning("transmittance_only", "Transmission-only fitting does not generally identify n/k uniquely. Add independent reflectance and angle/polarization measurements.", "strong"))
    if all(spectrum.angle == 0 for spectrum in problem.spectra):
        warnings.append(_warning("normal_incidence_ambiguity", "Normal-incidence spectra alone can remain ambiguous, even with both R and T. Oblique independent measurements improve constraints but do not guarantee uniqueness."))
    if problem.model == "cauchy":
        warnings.append(_warning("transparent_model_assumption", "k=0 is imposed only because this input band was explicitly declared transparent. Cauchy is empirical and not a general absorption/KK-consistent model."))
    else:
        warnings.append(_warning("limited_lorentz_model", "One passive Lorentz oscillator plus eps_inf is a restricted model, not a universal material response. Bandgaps, multiple transitions, roughness, gradients and anisotropy are not modeled."))
    summaries = []
    residuals = []
    for spectrum, predicted in zip(problem.spectra, predictions, strict=True):
        difference = predicted - spectrum.measured
        residuals.append(difference)
        summaries.append({"id": spectrum.original["id"], "quantity": spectrum.quantity,
                          "wavelength_nm": spectrum.wave_nm.tolist(),
                          "measured_fraction": spectrum.measured.tolist(),
                          "predicted_fraction": predicted.tolist(),
                          "residual_fraction": difference.tolist(), "rmse": _rmse(difference),
                          "angle_deg": spectrum.angle, "polarization": spectrum.polarization,
                          "original": deepcopy(spectrum.original)})
    combined = np.concatenate(residuals)
    rms = _rmse(combined)
    if rms > problem.options["acceptance_rmse"]:
        warnings.append(_warning("poor_fit", "Residual RMSE exceeds the configured tolerance. Check model choice, film thickness, substrate/backside, measurement normalization and spectral resolution.", "strong"))
    if not solution.success:
        warnings.append(_warning("solver_not_converged", "The best run reached its numerical limit without convergence; returned values are an incomplete estimate.", "strong"))
    if any("error" in run for run in runs):
        warnings.append(_warning("multistart_incomplete", "One or more sampled starts failed or exhausted its residual budget. The remaining starts provide incomplete coverage of possible solutions."))
    bounds = {name: [float(low), float(high)]
              for name, low, high in zip(problem.names, problem.low, problem.high, strict=True)}
    relative = (parameters - problem.low) / (problem.high - problem.low)
    bound_hits = [name for name, position in zip(problem.names, relative, strict=True)
                  if position <= 1e-5 or position >= 1 - 1e-5]
    if bound_hits:
        warnings.append(_warning("parameter_at_bound", "One or more fitted parameters touch their bounds: " + ", ".join(bound_hits) + ". Bounds may control the estimate."))
    singular = np.linalg.svd(solution.jac, compute_uv=False)
    rank = int(np.linalg.matrix_rank(solution.jac))
    condition = (float(singular[0] / singular[-1])
                 if singular.size and singular[-1] > 0 else None)
    if condition is not None and not math.isfinite(condition):
        condition = None
    if rank < len(problem.names) or condition is None or condition > 1e8:
        warnings.append(_warning("ill_conditioned", "The local weighted Jacobian is rank-deficient or poorly conditioned. Parameter values are sensitive and should not be treated as independently measured.", "strong"))
    if combined.size <= len(problem.names):
        warnings.append(_warning("insufficient_independent_data", "The number of measured samples is no larger than the number of fitted parameters; a low residual cannot establish a reliable estimate.", "strong"))
    best_weighted = math.sqrt(float(candidates[0][3]))
    # Project heuristic for exploration, never a statistical confidence region.
    near = [entry[1] for entry in candidates
            if math.sqrt(entry[3]) <= best_weighted + max(1e-6, 0.05 * best_weighted)]
    spread = (np.ptp(np.stack(near), axis=0) / (problem.high - problem.low)
              if len(near) > 1 else np.zeros_like(parameters))
    if np.max(spread) > 0.02:
        warnings.append(_warning("multiple_similar_solutions", "Different starts produced materially different parameters with similar residuals. These are sampled alternatives, not an exhaustive uncertainty estimate.", "strong"))
    wave = np.unique(np.concatenate([spectrum.wave_nm for spectrum in problem.spectra]))
    if wave.size > 2 and np.max(np.diff(wave)) > 5 * np.median(np.diff(wave)):
        warnings.append(_warning("spectral_gaps", "Large gaps exist between measured wavelengths. The fitted model interpolates across them without independent validation; the tabulated candidate is only sampled model output."))
    index = _index(problem.model, parameters, wave)
    values = dict(zip(problem.names, parameters.tolist(), strict=True))
    notes = ("Model-dependent single-layer optical inversion preview. "
             "Known thickness and substrate; no uniqueness claim. "
             + _MODELS[problem.model]["formula"] + "\nParameters: "
             + json.dumps(values, sort_keys=True) + "\nWarning codes: "
             + ", ".join(item["code"] for item in warnings)
             + "\nSave the complete fit provenance (original spectra, units, geometry, bounds and diagnostics) with this candidate. This tabulated dataset samples the fitted model at the union of raw measurement wavelengths; subsequent linear interpolation is not the analytic model itself. No extrapolation beyond the measured band is validated.")
    optical_set = {"id": "inversion_preview", "name": problem.request.get("name") or "Optical inversion candidate",
                   "kind": "tabulated", "wavelength_unit": "nm",
                   "wavelength_min": float(wave[0]), "wavelength_max": float(wave[-1]),
                   "wavelengths": wave.tolist(), "n": index.real.tolist(), "k": index.imag.tolist(),
                   "source": problem.request.get("source") or "Model-dependent optical inversion; original spectra retained in fit provenance.",
                   "notes": notes}
    return {"ok": True, "status": "completed", "converged": bool(solution.success),
            "evidence_type": "model_estimate", "unique_solution": False,
            "model": problem.model, "formula": _MODELS[problem.model]["formula"],
            "parameters": values, "parameter_units": dict(zip(problem.names, _MODELS[problem.model]["units"], strict=True)),
            "bounds": bounds, "structure": problem.structure, "request": problem.request,
            "nk": {"wavelength_nm": wave.tolist(), "n": index.real.tolist(), "k": index.imag.tolist()},
            "spectra": summaries, "rmse": rms, "rmse_unit": "absolute_fraction",
            "weighted_rmse": best_weighted, "warnings": warnings,
            "diagnostics": {"bound_hits": bound_hits, "multistarts": runs,
                            "residual_evaluations": evaluations, "jacobian_rank": rank,
                            "condition_number": condition, "non_unique": True,
                            "normalized_parameter_spread": dict(zip(problem.names, spread.tolist(), strict=True)),
                            "near_best_starts": len(near),
                            "multistart_note": "Finite sampled starts do not prove global optimality or uniqueness.",
                            "acceptance_rmse": problem.options["acceptance_rmse"],
                            "options": problem.options},
            "optical_set": optical_set, "saved": False,
            "provenance_required_before_save": True}
