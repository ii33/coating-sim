"""Actual two-face synthesis TMM with physical-side analytic thickness columns.

The coherent recurrence is shared with the existing derivative engine. Thick
plates use exactly film_optics.incoherent_plate_rta's forward/reverse intensity
sum, differentiated by the product/quotient rules before polarization averaging.
No independent-face merit, virtual front stack or finite-difference scorer exists.
"""
from copy import deepcopy
import math
import numpy as np

from .film_design import validate_design, physical_thickness_nm
from .film_design_service import (simulation_settings, automatic_substrate_handling,
    validate_external_ports, FilmDesignService, active_material_references)
from .film_optimization_derivatives import (_PreparedMaterialData, _variable_fields,
    _analytic_one_polarization, _prepare_thickness_jacobian_inputs, _thickness_jacobian_arrays)
from .film_optics import (_beta,
    _longitudinal_many, _coherent_many, _polarizations)


def simulate_synthesis_with_thickness_jacobian(design, materials, variables, settings, *,
        zero_thickness_layer_ids=(), wavelengths_nm=None):
    if design['substrate']['sides'] == 'single':
        inputs = _prepare_thickness_jacobian_inputs(design, materials, variables, settings,
            zero_thickness_layer_ids=zero_thickness_layer_ids, wavelengths_nm=wavelengths_nm)
        jr, jt = _thickness_jacobian_arrays(*inputs)
        value = dict(wavelength_nm=inputs[0].tolist(), jacobian_R=jr.tolist(), jacobian_T=jt.tolist())
        checked = validate_design(deepcopy(design))
        normalized = simulation_settings(dict(settings, first_surface=settings.get('first_surface', checked['first_surface'])))
        prepared = materials if type(materials) is _PreparedMaterialData else _PreparedMaterialData(materials, checked)
        catalog = {row['material_id']: prepared._material_record(row['material_id'])
                   for row in active_material_references(checked)}
        direct = FilmDesignService._simulate_chunk(object.__new__(FilmDesignService), checked,
            normalized, np.asarray(value['wavelength_nm']), include_reference_queries=False, catalog_snapshot=catalog)
        for metric in ('R', 'T'):
            value[metric] = direct[metric]
        return value
    design = validate_design(deepcopy(design))
    normalized = simulation_settings(dict(settings, first_surface=settings.get('first_surface', design['first_surface'])))
    waves = (np.linspace(normalized['wavelength_min_nm'], normalized['wavelength_max_nm'], normalized['samples'])
             if wavelengths_nm is None else np.asarray(wavelengths_nm, dtype=float))
    if (waves.shape != (normalized['samples'],) or not np.all(np.isfinite(waves))
            or np.any(waves <= 0) or np.any(np.diff(waves) < 0)
            or waves[0] != normalized['wavelength_min_nm']
            or waves[-1] != normalized['wavelength_max_nm']):
        raise ValueError('synthesis wavelengths must match the exact finite simulation grid')
    prepared = materials if type(materials) is _PreparedMaterialData else _PreparedMaterialData(materials, design)
    rows = {row['id']: (side, row) for side in ('front', 'back') for row in design[side + '_layers']}
    columns = {}
    for index, variable in enumerate(variables):
        side, identifier, lower, upper = _variable_fields(variable)
        if (identifier in columns or identifier not in rows or rows[identifier][0] != side
                or not math.isfinite(lower) or not math.isfinite(upper) or lower < 0 or upper < lower):
            raise ValueError('synthesis thickness variables require unique matching physical-side IDs and finite bounds')
        columns[identifier] = index
    zeros = tuple(zero_thickness_layer_ids)
    if len(set(zeros)) != len(zeros):
        raise ValueError('zero-thickness IDs must be unique')
    for identifier in zeros:
        if (identifier not in rows or rows[identifier][1]['thickness'] != dict(value=0., unit='nm')):
            raise ValueError('zero-thickness IDs must identify exact physical nm zeros')
    use_k, policy = design['use_k'], normalized['extrapolation_policy']
    def series(reference):
        if reference.get('kind') == 'constant':
            return np.full(len(waves), reference['n'], dtype=complex)
        return prepared._index_series(reference, waves, use_k=use_k, extrapolation_policy=policy)
    def layer_data(layers):
        indices, thicknesses, ids = [], [], []
        for row in layers:
            indices.append(series(row))
            ids.append(row['id'])
            nref = None
            if row['thickness']['unit'] == 'qw':
                nref = prepared._sampler(row)(design['reference_wavelength_nm'], 'nm',
                    extrapolation_policy=policy, include_k=use_k)[0]
            thicknesses.append(physical_thickness_nm(row, design['reference_wavelength_nm'], nref))
        return (np.column_stack(indices) if indices else np.empty((len(waves), 0), complex),
                np.asarray(thicknesses), ids)
    ni, ne, ns = series(design['incident_medium']), series(design['exit_medium']), series(design['substrate'])
    validate_external_ports(ni, ne)
    front, back = design['front_layers'], design['back_layers']
    if normalized['first_surface'] == 'back':
        front, back, ni, ne = back, front, ne, ni
    fi, fd, fids = layer_data(list(reversed(front)))
    bi, bd, bids = layer_data(back)
    handling = automatic_substrate_handling(design, normalized['wavelength_max_nm'])
    angle, polarization = normalized['angle_deg'], normalized['polarization']
    if handling['resolved_mode'] == 'coherent':
        indices = np.column_stack((ni, fi, ns, bi, ne))
        ds = np.concatenate((fd, [handling['thickness_nm']], bd))
        ids = [*fids, None, *bids]
        jac = [_analytic_one_polarization(waves, indices, ds, ids, columns, angle, p)
               for p in _polarizations(polarization)] if columns else []
        jr = np.mean([j[0] for j in jac], axis=0) if jac else np.zeros((len(waves), 0))
        jt = np.mean([j[1] for j in jac], axis=0) if jac else np.zeros_like(jr)
    else:
        beta = _beta(None, ni, angle)
        nf, nb = np.column_stack((ni, fi, ns)), np.column_stack((ns, bi, ne))
        survival = np.exp(-4 * math.pi * _longitudinal_many(ns[:, None], beta)[:, 0].imag
                          * handling['thickness_nm'] / waves)
        derivatives = []
        for p in _polarizations(polarization):
            if not columns:
                break
            def block(indices, ds, ids, polarization=p):
                values = _coherent_many(waves, indices, ds, beta, polarization)
                dr, dt = _analytic_one_polarization(waves, indices, ds, ids, columns, angle, polarization, beta=beta)
                return values[:, 0, None], values[:, 1, None], dr, dt
            rf, tf, drf, dtf = block(nf, fd, fids)
            rr, tr, drr, dtr = block(nf[:, ::-1], fd[::-1], list(reversed(fids)))
            rb, tb, drb, dtb = block(nb, bd, bids)
            a, a2 = survival[:, None], survival[:, None] ** 2
            denominator = 1 - rr * rb * a2
            droundtrip = (drr * rb + rr * drb) * a2
            numerator_r = tf * tr * rb * a2
            dnumerator_r = (dtf * tr * rb + tf * dtr * rb + tf * tr * drb) * a2
            numerator_t = tf * tb * a
            dnumerator_t = (dtf * tb + tf * dtb) * a
            derivatives.append((drf + dnumerator_r / denominator + numerator_r * droundtrip / denominator ** 2,
                                dnumerator_t / denominator + numerator_t * droundtrip / denominator ** 2))
        jr = np.mean([j[0] for j in derivatives], axis=0) if derivatives else np.zeros((len(waves), 0))
        jt = np.mean([j[1] for j in derivatives], axis=0) if derivatives else np.zeros_like(jr)
    # Canonical production service is the sole forward truth; recurrence
    # blocks above exist only to differentiate the same model analytically.
    catalog = {row['material_id']: prepared._material_record(row['material_id'])
               for row in active_material_references(design)}
    response = FilmDesignService._simulate_chunk(object.__new__(FilmDesignService),
        design, normalized, waves, include_reference_queries=False, catalog_snapshot=catalog)
    if not np.all(np.isfinite(jr)) or not np.all(np.isfinite(jt)):
        raise ValueError('nonfinite two-face synthesis thickness Jacobian')
    return dict(wavelength_nm=waves.tolist(), R=np.asarray(response['R']).reshape(-1).tolist(),
        T=np.asarray(response['T']).reshape(-1).tolist(), jacobian_R=jr.tolist(), jacobian_T=jt.tolist())
