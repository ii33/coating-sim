"""Regress metre-scale clipping and the scalar Mitsuba eta contract."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from coat_sim.appearance import render
from coat_sim.appearance.contract import validate_job
from tests.test_appearance_circle import _circle_job, _mock_mitsuba
from tests.test_appearance_result_contract import _job


@pytest.mark.parametrize('circle', [False, True])
def test_close_camera_near_clip_stays_before_front_interface(circle):
    job = _circle_job() if circle else _job()
    job['camera']['position_mm'] = [0, 0, -10]
    job['camera']['target_mm'] = [0, 0, 0]
    scene = render._scene_dict(_mock_mitsuba(), validate_job(job), 500., 0., 1.)
    sensor = scene['sensor']
    assert sensor['near_clip'] == 1e-6
    origin = sensor['to_world'].matrix[:3, 3]
    axis = sensor['to_world'].matrix[:3, 2]
    ray_origin = origin + sensor['near_clip'] * axis
    assert ray_origin[2] < 0  # default 0.01 m would start on the interface


@pytest.mark.parametrize('inside', [False, True])
@pytest.mark.parametrize('radiance', [False, True])
@pytest.mark.parametrize('reflection', [False, True])
def test_scalar_bsdf_eta_matches_transmitted_over_incident(inside, radiance, reflection):
    registered = {}

    class Base:
        def __init__(self, props):
            pass

    def vector(x, y, z):
        return SimpleNamespace(x=x, y=y, z=z)

    mi = SimpleNamespace(
        BSDF=Base, BSDFSample3f=SimpleNamespace, Color3f=float, UInt32=int,
        Vector3f=vector, reflect=lambda wi: vector(-wi.x, -wi.y, wi.z),
        BSDFFlags=SimpleNamespace(DeltaReflection=1, DeltaTransmission=2,
                                 FrontSide=4, BackSide=8, Reflection=1, Transmission=2),
        TransportMode=SimpleNamespace(Radiance='radiance'),
        register_bsdf=lambda name, factory: registered.update({name: factory}),
    )
    with patch.object(render, '_BSDF_REGISTERED', False):
        render._register_bsdf(mi)
        bsdf = registered['appearance_coherent_stack'](
            {'wavelength_nm': 500., 'ambient_n': 1., 'substrate_n': 1.5})
    ctx = SimpleNamespace(mode='radiance' if radiance else 'importance',
                          is_enabled=lambda flag, component: component == (0 if reflection else 1))
    si = SimpleNamespace(wi=vector(0, 0, -1 if inside else 1))
    bs, weight = bsdf.sample(ctx, si, .5, (0., 0.))
    eta = 1 / 1.5 if inside else 1.5
    assert bs.eta == pytest.approx(1. if reflection else eta)
    assert bs.pdf == 1.
    expected = .04 if reflection else .96 * (eta ** -2 if radiance else 1.)
    assert weight == pytest.approx(expected)
    assert bs.wo.z == (si.wi.z if reflection else -si.wi.z)
