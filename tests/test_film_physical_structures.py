from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from coat_sim.core.film_design import new_design, validate_design
from coat_sim.core.film_optics import coherent_rta
from coat_sim.core.film_physical_structures import (
    FilmBraggStackSection as Bragg, FilmChirpedSection as Chirp,
    FilmCoupledCavitySection as Cavity, FilmDigitalRugateSection as Rugate,
    FilmOpticalLayerSpec as Layer, FilmStructureMaterialBinding as Binding,
    FilmStructureSpec as Spec, generate_physical_structure as generate,
    physical_structure_catalog, structure_spec_from_mapping, structure_spec_to_mapping,
    dual_window_ar_objectives, dual_passband_objectives, six_line_ar_objectives,
)

pytestmark = pytest.mark.algorithm


def fixture_problem(nh=2.2, nl=1.45, ns=1.):
    """Analytic constant-index oracle, not evidence for real material data."""
    materials = []
    for name, n in (('high', nh), ('low', nl), ('substrate', ns)):
        materials.append(dict(id=name, name=name, design_ready=True, optical_sets=[
            dict(id='nk', name=name, kind='tabulated', wavelength_unit='nm',
                 wavelength_min=400., wavelength_max=16000., wavelengths=[400., 16000.],
                 n=[float(n)]*2, k=[0.]*2, notes='analytical oracle',
                 source='synthetic test input, not physical material evidence')]))
    env = new_design(design_id='physical-test')
    env['substrate'].update(material_id='substrate', optical_set_id='nk')
    bindings = (Binding('H', 'high', 'nk'), Binding('L', 'low', 'nk'))
    return env, dict(materials=materials), bindings


def response(result, wavelengths, nh=2.2, nl=1.45, ns=1.):
    rows = list(reversed(result.metadata['layers']))
    indices = [1.] + [{'high': nh, 'low': nl}[r['material_id']] for r in rows] + [ns]
    result = coherent_rta(wavelengths, indices, [r['physical_thickness_nm'] for r in rows])
    return {key: np.asarray(value) if isinstance(value, list) else value for key,value in result.items()}


def test_bragg_center_matches_exact_admittance_and_order():
    env, snapshot, bindings = fixture_problem(ns=1.5)
    result = generate(env, Spec('multistack', bindings, (Bragg(1000., pairs=5),)), snapshot)
    assert validate_design(result.candidate_design) == result.candidate_design
    assert [r['role'] for r in result.metadata['layers']] == ['L', 'H']*5
    y = 1.5*(2.2/1.45)**10
    assert response(result, 1000., ns=1.5)['R'] == pytest.approx(((y-1)/(y+1))**2, abs=1e-12)
    assert response(result, 1000., ns=1.5)['R'] > .95
    assert result.metadata['layer_order'] == 'substrate_outward'
    assert result.metadata['layer_count'] == 10


def test_two_stacks_and_independent_matching_layers_preserve_parameters():
    env, snapshot, bindings = fixture_problem()
    sections = (Layer('L', 900., thickness_nm=33.),
                Bragg(900., pairs=4, high_low_optical_ratio=2.),
                Layer('H', 1100., optical_thickness_waves=.13),
                Bragg(1200., pairs=6, thickness_scale=.9),
                Layer('L', 1200., thickness_nm=44.))
    result = generate(env, Spec('multistack', bindings, sections), snapshot)
    assert result.metadata['layer_count'] == 23
    assert result.metadata['layers'][0]['physical_thickness_nm'] == 33.
    assert result.metadata['layers'][-1]['physical_thickness_nm'] == 44.
    first = result.metadata['layers'][1:3]
    assert first[1]['n_at_wavelength']*first[1]['physical_thickness_nm'] / (first[0]['n_at_wavelength']*first[0]['physical_thickness_nm']) == pytest.approx(2.)
    # Two Bragg centers remain high-reflection regions in the combined stack.
    assert np.min(response(result, np.array([900., 1200.]))['R']) > .5


def single_cavity(pairs=4):
    return Cavity(Bragg(1000., pairs=pairs, first_role='L'),
                  (Layer('H', 1000., optical_thickness_waves=.5),), (),
                  Bragg(1000., pairs=pairs, first_role='H'))


def test_half_wave_cavity_has_unit_peak_and_mirrors_reject_nearby():
    env, snapshot, bindings = fixture_problem()
    result = generate(env, Spec('coupled_cavity', bindings, (single_cavity(),)), snapshot)
    t = response(result, np.array([970., 1000., 1030.]))['T']
    assert t[1] == pytest.approx(1., abs=1e-12)
    assert max(t[0], t[2]) < .65
    # Cavity thickness is an independently configurable real film.
    shifted = replace(single_cavity(), cavities=(Layer('H', 1000., thickness_nm=240.),))
    changed = generate(env, Spec('coupled_cavity', bindings, (shifted,)), snapshot)
    assert response(changed, 1000.)['T'] < .5


def test_two_coupled_cavities_split_resonances_and_allow_blocking():
    env, snapshot, bindings = fixture_problem()
    multi = Cavity(Bragg(1000., pairs=4, first_role='L'),
        (Layer('H', 1000., .5), Layer('H', 1000., .5)),
        (Bragg(1000., pairs=1, first_role='H', terminal_role='H'),), Bragg(1000., pairs=4, first_role='H'))
    wavelengths = np.linspace(900., 1100., 2001)
    result = generate(env, Spec('coupled_cavity', bindings, (multi,)), snapshot)
    t = response(result, wavelengths)['T']
    peaks = np.flatnonzero((t[1:-1] > t[:-2]) & (t[1:-1] > t[2:]))+1
    assert len(peaks[t[peaks] > .8]) >= 2
    block = Bragg(1450., pairs=5)
    combined = generate(env, Spec('composite', bindings, (multi, block)), snapshot)
    assert combined.metadata['layer_count'] == result.metadata['layer_count']+10
    assert response(combined, 1450.)['T'] < .1
    # Coupling mirror and cavity detuning remain independent values.
    altered = replace(multi, coupling_mirrors=(Bragg(1000., pairs=2),),
                      cavities=(Layer('H',1000.,.5), Layer('L', 1020., .5)))
    different = generate(env, Spec('coupled_cavity', bindings, (altered,)), snapshot)
    assert different.metadata['parameters']['sections'][0]['cavities'][1]['wavelength_nm'] == 1020.
    assert not np.allclose(t, response(different, wavelengths)['T'])


@pytest.mark.parametrize('spacing', ['linear','log'])
def test_chirp_preserves_local_bragg_sum_and_independent_ratio(spacing):
    env, snapshot, bindings = fixture_problem()
    section = Chirp(800., 1400., pairs=7, ratio_start=.4, ratio_end=1.8, spacing=spacing)
    result = generate(env, Spec('chirped', bindings, (section,)), snapshot)
    rows = result.metadata['layers']
    centers = np.array([rows[i]['local_bragg_wavelength_nm'] for i in range(0,len(rows),2)])
    if spacing == 'linear':
        assert np.diff(centers) == pytest.approx([100.]*6)
    else:
        assert centers[1:]/centers[:-1] == pytest.approx([(1400/800)**(1/6)]*6)
    for a,b in zip(rows[::2],rows[1::2], strict=False):
        assert a['n_at_wavelength']*a['physical_thickness_nm']+b['n_at_wavelength']*b['physical_thickness_nm'] == pytest.approx(a['local_bragg_wavelength_nm']/2)
    assert rows[0]['high_low_optical_ratio'] == .4
    assert rows[-1]['high_low_optical_ratio'] == pytest.approx(1.8)


def continuous_rugate_oracle(wavelengths, section, *, slices=4096):
    # Independent direct scalar-profile forward model. It is never exported as
    # invented material data; this checks the digital sequence's thin-cell limit.
    mean, amplitude = 1.8, .18
    total = section.period_nm*section.periods
    z = (np.arange(slices)+.5)*total/slices
    n = mean+amplitude*np.sin(2*np.pi*z/section.period_nm)
    return np.asarray(coherent_rta(wavelengths, np.r_[1., n, 1.], np.full(slices,total/slices))['R'])


def test_real_digital_rugate_converges_to_scalar_profile_and_rejects_center():
    env, snapshot, bindings = fixture_problem(nh=2., nl=1.6)
    section = Rugate(1000., 1000./3.6, periods=5, mean_n=1.8, modulation_n=.18,
                     apodization='none')
    waves = np.array([900., 1000., 1100.])
    oracle = continuous_rugate_oracle(waves, section)
    errors = []
    for samples in (8, 16, 32):
        result = generate(env, Spec('digital_rugate', bindings,
                                   (replace(section,samples_per_period=samples),)), snapshot)
        rows = result.metadata['layers']
        assert {r['material_id'] for r in rows} == {'high','low'}
        assert {r['optical_set_id'] for r in rows} == {'nk'}
        assert sum(r['physical_thickness_nm'] for r in rows) == pytest.approx(section.periods*section.period_nm)
        errors.append(np.max(np.abs(response(result,waves,nh=2.,nl=1.6)['R']-oracle)))
    assert errors[2] < errors[1] < errors[0]
    assert errors[2] < .003
    # Weak-modulation coupled-wave estimate for the Bragg reflectance.
    estimate = np.tanh(np.pi*.18*section.periods/(2*1.8))**2
    assert response(result, 1000., nh=2.,nl=1.6)['R'] == pytest.approx(estimate, abs=.03)


def test_zero_chirp_reduces_to_periodic_stack():
    env,snapshot,bindings=fixture_problem()
    periodic=generate(env,Spec('multistack',bindings,(Bragg(1000.,pairs=7),)),snapshot)
    unchirped=generate(env,Spec('chirped',bindings,(Chirp(1000.,1000.,pairs=7),)),snapshot)
    assert [r['physical_thickness_nm'] for r in periodic.metadata['layers']] == [r['physical_thickness_nm'] for r in unchirped.metadata['layers']]
    assert response(periodic,np.array([900.,1000.,1100.]))['R'] == pytest.approx(response(unchirped,np.array([900.,1000.,1100.]))['R'])


def test_zero_rugate_modulation_converges_to_uniform_index_slab():
    env,snapshot,bindings=fixture_problem(nh=2.,nl=1.6)
    waves=np.array([850.,1000.,1150.])
    section=Rugate(1000.,250.,periods=3,mean_n=1.8,modulation_n=0.,apodization='none')
    uniform=np.asarray(coherent_rta(waves,[1.,1.8,1.],[750.])['R'])
    errors=[]
    for samples in (8,16,32):
        result=generate(env,Spec('digital_rugate',bindings,(replace(section,samples_per_period=samples),)),snapshot)
        assert {r['cell_target_n'] for r in result.metadata['layers']} == {1.8}
        errors.append(max(abs(response(result,waves,nh=2.,nl=1.6)['R']-uniform)))
    assert errors[2] < errors[1] < errors[0]
    assert errors[2] < .001


def test_replay_is_deterministic_pure_and_parameters_round_trip():
    env, snapshot, bindings = fixture_problem()
    spec = Spec('composite', bindings, (single_cavity(), Chirp(900.,1100.,pairs=3),
                Rugate(1000.,250.,periods=1,samples_per_period=4)))
    original = deepcopy((env,snapshot))
    encoded = structure_spec_to_mapping(spec)
    assert structure_spec_from_mapping(encoded) == spec
    first = generate(env,spec,snapshot)
    assert first == generate(env,structure_spec_from_mapping(encoded),snapshot)
    assert first.candidate_design['reference_wavelength_nm'] == env['reference_wavelength_nm']
    assert (env,snapshot) == original
    first.metadata['layers'][0]['physical_thickness_nm'] = 7.
    assert generate(env,spec,snapshot).metadata['layers'][0]['physical_thickness_nm'] != 7.
    assert first.metadata['unsupported_acceptance_metrics'] == ['phase','GD','GDD']
    assert all(c['status']=='mechanism_seed_not_paper_reproduction' for c in physical_structure_catalog())


@pytest.mark.parametrize('fault', ['unknown_k','outside_n','outside_k','reversed_index','negative_k','missing_binding','unready'])
def test_bad_materials_fail_without_guessing_or_extrapolation(fault):
    env,snapshot,bindings = fixture_problem()
    optical = snapshot['materials'][0]['optical_sets'][0]
    wave = 1000.
    if fault == 'unknown_k':
        optical['k'] = None
    elif fault == 'outside_n':
        wave = 17000.
    elif fault == 'outside_k':
        for key in ('wavelengths','n','k'):
            optical.pop(key)
        optical.update(kind='cauchy',parameters={'A':2.2,'B':0.,'C':0.},
            k_data={'wavelength_unit':'nm','wavelength_min':400.,'wavelength_max':800.,
                    'wavelengths':[400.,800.],'k':[0.,0.]})
    elif fault == 'reversed_index':
        optical['n'] = [1.1,1.1]
    elif fault == 'negative_k':
        optical['k'] = [-.1,-.1]
    elif fault == 'missing_binding':
        bindings = bindings[1:]
    elif fault == 'unready':
        snapshot['materials'][0]['design_ready'] = False
    with pytest.raises(ValueError):
        generate(env,Spec('multistack',bindings,(Bragg(wave),)),snapshot)


def test_explicit_validation_grid_checks_all_materials_not_only_construction_centers():
    env,snapshot,bindings=fixture_problem()
    spec=Spec('multistack',bindings,(Bragg(1000.),),validation_wavelengths_nm=(17000.,))
    with pytest.raises(ValueError):
        generate(env,spec,snapshot)


@pytest.mark.parametrize('section', [Bragg(1000.,pairs=True), Bragg(1000.,first_role='bad'),
    Chirp(1000.,1100.,pairs=1), Rugate(1000.,200.,mean_n=3.),
    Cavity(Bragg(1000.),(Layer('H',1000.),Layer('H',1000.)),(),Bragg(1000.))])
def test_invalid_physical_parameters_are_rejected(section):
    env,snapshot,bindings=fixture_problem()
    with pytest.raises(ValueError):
        generate(env,Spec('composite',bindings,(section,)),snapshot)


def test_strict_mapping_and_environment_contract():
    env,snapshot,bindings=fixture_problem()
    spec=Spec('multistack',bindings,(Bragg(1000.),))
    raw=structure_spec_to_mapping(spec)
    raw['sections'][0]['mystery']=1
    with pytest.raises(ValueError):
        structure_spec_from_mapping(raw)
    env['assume_lossless']=True
    with pytest.raises(ValueError,match='assume_lossless'):
        generate(env,spec,snapshot)


def test_three_target_families_do_not_replace_each_other():
    ar=dual_window_ar_objectives(max_reflectance=.01)
    assert [(o.wavelength_min_nm,o.wavelength_max_nm,o.metric) for o in ar] == [(3000.,5000.,'R'),(8000.,12000.,'R')]
    bp=dual_passband_objectives(min_transmittance=.95,max_stop_transmittance=.01,
         stopbands_nm=((2000.,2900.),(5100.,7900.),(12100.,14000.)))
    assert len(bp)==5 and all(o.metric=='T' for o in bp)
    assert bp[0].pointwise.lower_bound==.95 and bp[-1].pointwise.upper_bound==.01
    lines=six_line_ar_objectives(max_reflectance=.01)
    assert len(lines)==6 and all(o.wavelength_samples==1 and o.wavelength_min_nm==o.wavelength_max_nm for o in lines)
    with pytest.raises(ValueError):
        dual_passband_objectives(min_transmittance=.95,max_stop_transmittance=.01,
                                 stopbands_nm=((4900.,6000.),))
