"""Optical engine regression oracles and independent moderate-stack matrix check."""
import json
import math
import unittest

import numpy as np

from coat_sim.core.film_optics import coherent_rta, incoherent_plate_rta


def matrix_reference(wavelength, indices, thickness, angle, polarization):
    """Independent tangential-field characteristic matrix (moderate loss only)."""
    n = np.asarray(indices, dtype=complex)
    beta = n[0].real * math.sin(math.radians(angle))
    q = np.sqrt(n * n - beta * beta + 0j)
    y = q if polarization == "s" else n * n / q
    matrix = np.eye(2, dtype=complex)
    for index, d in enumerate(thickness, 1):
        delta = 2 * math.pi * q[index] * d / wavelength
        c, s = np.cos(delta), np.sin(delta)
        matrix = matrix @ np.array([[c, -1j * s / y[index]], [-1j * y[index] * s, c]])
    a = matrix[0, 0] + matrix[0, 1] * y[-1]
    b = matrix[1, 0] + matrix[1, 1] * y[-1]
    r = (y[0] * a - b) / (y[0] * a + b)
    t = 2 * y[0] / (y[0] * a + b)
    return abs(r) ** 2, y[-1].real / y[0].real * abs(t) ** 2


class FilmOpticsTests(unittest.TestCase):
    def assertRta(self, result, expected, tolerance=2e-12):
        for name, value in zip(("R", "T", "A"), expected):
            self.assertAlmostEqual(result[name], value, delta=tolerance, msg=name)
        self.assertAlmostEqual(result["R"] + result["T"] + result["A"], 1, delta=2e-12)

    def test_bare_fresnel_and_absorbing_exit_definition(self):
        self.assertRta(coherent_rta(550, [1, 1.5], []), (.04, .96, 0))
        result = coherent_rta(550, [1, 1.5 + .2j], [])
        self.assertRta(result, (abs((1 - (1.5 + .2j)) / (1 + (1.5 + .2j))) ** 2,
                               1 - result["R"], 0))
        self.assertIn("finite", result["absorption_definition"])

    def test_absorbing_45_degree_oracle(self):
        stack = [1, 2.1 + .08j, 1.52]
        self.assertRta(coherent_rta(550, stack, [100], 45, "s"),
                       (.26429244310016714, .6080923206293543, .12761523627047855))
        self.assertRta(coherent_rta(550, stack, [100], 45, "p"),
                       (.06890570676975344, .768946215429843, .16214807780040363))

    def test_very_thick_absorber_has_no_overflow(self):
        for p, reflectance in (("s", .9706473293776041), ("p", .8661783287829408)):
            with np.errstate(over="raise", invalid="raise", under="ignore"):
                result = coherent_rta(550, [1, .2 + 3j, 1.5], [1e6], 67, p)
            self.assertRta(result, (reflectance, 0, 1 - reflectance))

    def test_quarter_wave_ar_hr(self):
        self.assertRta(coherent_rta(550, [1, math.sqrt(1.5), 1.5],
                                   [112.26827987756234]), (0, 1, 0))
        layers = [2.1, 1.45] * 10
        result = coherent_rta(550, [1] + layers + [1.52], [550 / (4 * n) for n in layers])
        self.assertRta(result, (.9984046942529268, 1 - .9984046942529268, 0))

    def test_brewster_tir_and_unpolarized_average(self):
        brewster = math.degrees(math.atan(1.5))
        self.assertRta(coherent_rta(550, [1, 1.5], [], brewster, "p"), (0, 1, 0))
        self.assertRta(coherent_rta(550, [1.5, 1], [], 60), (1, 0, 0))
        s = coherent_rta(550, [1, 2 + .1j, 1.5], [90], 55, "s")
        p = coherent_rta(550, [1, 2 + .1j, 1.5], [90], 55, "p")
        u = coherent_rta(550, [1, 2 + .1j, 1.5], [90], 55)
        for key in ("R", "T", "A"):
            self.assertAlmostEqual(u[key], (s[key] + p[key]) / 2)

    def test_independent_characteristic_matrix(self):
        rng = np.random.default_rng(461)
        for count in (1, 2, 4, 6):
            for angle in (0, 23, 61):
                n = [1] + list(rng.uniform(1.2, 2.5, count) + 1j * rng.uniform(.005, .15, count)) + [1.52]
                d = rng.uniform(20, 180, count)
                for p in ("s", "p"):
                    expected_r, expected_t = matrix_reference(550, n, d, angle, p)
                    self.assertRta(coherent_rta(550, n, d, angle, p),
                                   (expected_r, expected_t, 1 - expected_r - expected_t))

    def test_double_sided_plate_and_thickness_invariance(self):
        for d in (0, 1e3, 1e6, 1e9):
            self.assertRta(incoherent_plate_rta(550, 1, 1.5, 1, substrate_thickness_nm=d),
                           (.07692307692307694, .923076923076923, 0))
        ar = math.sqrt(1.5)
        self.assertRta(incoherent_plate_rta(550, 1, 1.5, 1,
                       [ar], [550 / (4 * ar)], [ar], [550 / (4 * ar)], 1e6), (0, 1, 0))

    def test_asymmetric_absorbing_plate_and_polarization(self):
        results = {}
        for p in ("s", "p", "unpolarized"):
            result = incoherent_plate_rta(550, 1, 1.52, 1.1,
                     [2.1 + .08j, 1.4], [100, 70], [1.7 + .05j], [120], 1e6, 45, p)
            self.assertGreater(result["A"], 0)
            self.assertTrue(all(0 <= result[key] <= 1 for key in ("R", "T", "A")))
            self.assertRta(result, (result["R"], result["T"], result["A"]))
            results[p] = result
        for key in ("R", "T", "A"):
            self.assertAlmostEqual(results["unpolarized"][key], (results["s"][key] + results["p"][key]) / 2)

    def test_dispersive_spectra_and_json_types(self):
        waves = [500, 550, 600]
        indices = [[1, 1.8 + .03j, 1.5], [1, 1.9 + .04j, 1.51], [1, 2 + .05j, 1.52]]
        result = coherent_rta(waves, indices, [100], 30)
        for i, wave in enumerate(waves):
            reference = coherent_rta(wave, indices[i], [100], 30)
            for key in ("R", "T", "A"):
                # Scalar recursion and batched wavelength arithmetic can
                # differ by a final floating-point rounding bit.
                self.assertAlmostEqual(result[key][i], reference[key], delta=8e-13)
        plate = incoherent_plate_rta(waves, [1, 1, 1], [1.5, 1.51, 1.52], [1, 1, 1],
                                     [[1.8], [1.9], [2]], [80])
        self.assertEqual(len(plate["R"]), 3)
        json.dumps(result, allow_nan=False)
        json.dumps(plate, allow_nan=False)

    def test_wavelength_batch_matches_scalar_for_30_60_120_layer_stacks(self):
        waves = np.linspace(420.0, 760.0, 13)
        for layer_count in (30, 60, 120):
            indices = np.empty((waves.size, layer_count + 2), dtype=complex)
            indices[:, 0] = 1.0
            for layer in range(layer_count):
                dispersion = (waves - 550.0) * 1e-4
                if layer % 2:
                    indices[:, layer + 1] = 2.10 + dispersion + 0.03j
                else:
                    indices[:, layer + 1] = 1.43 + dispersion * 0.3 + 0.001j
            indices[:, -1] = 1.52 + (waves - 550.0) * 2e-5
            thickness = np.resize(np.array([63.0, 91.0]), layer_count)
            for angle in (0.0, 47.0):
                for polarization in ("s", "p", "unpolarized"):
                    batch = coherent_rta(waves, indices, thickness, angle, polarization)
                    reference = [coherent_rta(wave, indices[i], thickness, angle, polarization)
                                 for i, wave in enumerate(waves)]
                    for channel in ("R", "T", "A"):
                        np.testing.assert_allclose(
                            batch[channel], [row[channel] for row in reference],
                            rtol=4e-14, atol=4e-14)

    def test_wavelength_batch_preserves_critical_layer_and_incoherent_plate_paths(self):
        waves = np.array([500.0, 550.0, 600.0])
        beta = np.array([0.2, 1.0, 0.3])
        critical = coherent_rta(waves, [[1.5, 1.0, 1.5]] * len(waves), [100.0], beta=beta)
        critical_reference = [coherent_rta(wave, [1.5, 1.0, 1.5], [100.0], beta=value)
                              for wave, value in zip(waves, beta)]
        for channel in ("R", "T", "A"):
            np.testing.assert_allclose(critical[channel], [row[channel] for row in critical_reference],
                                       rtol=2e-14, atol=2e-14)

        incident = np.ones(len(waves))
        substrate = np.array([1.50 + 0.004j, 1.51 + 0.005j, 1.52 + 0.006j])
        exit_index = np.ones(len(waves))
        front = np.array([[2.10 + 0.03j, 1.44 + 0.001j],
                          [2.11 + 0.032j, 1.45 + 0.001j],
                          [2.12 + 0.034j, 1.46 + 0.001j]])
        back = np.array([[1.70 + 0.02j], [1.71 + 0.021j], [1.72 + 0.022j]])
        for polarization in ("s", "p", "unpolarized"):
            batch = incoherent_plate_rta(waves, incident, substrate, exit_index,
                                         front, [83.0, 67.0], back, [105.0],
                                         1e6, 37.0, polarization)
            reference = [incoherent_plate_rta(
                wave, incident[i], substrate[i], exit_index[i], front[i], [83.0, 67.0],
                back[i], [105.0], 1e6, 37.0, polarization)
                for i, wave in enumerate(waves)]
            for channel in ("R", "T", "A"):
                np.testing.assert_allclose(batch[channel], [row[channel] for row in reference],
                                           rtol=4e-14, atol=4e-14)

    def test_fixed_beta_matches_angle(self):
        a = coherent_rta(550, [1, 2 + .05j, 1.5], [100], 45)
        b = coherent_rta(550, [1, 2 + .05j, 1.5], [100], beta=math.sin(math.pi / 4))
        self.assertRta(a, (b["R"], b["T"], b["A"]))

    def test_exact_critical_internal_layer_analytic_limit(self):
        # n_layer == beta makes its longitudinal wavevector exactly zero;
        # this has a finite shear-matrix limit, rather than a 0/0 interface.
        q0 = math.sqrt(1.5 ** 2 - 1)
        length = 2 * math.pi * 100 / 550
        for p in ("s", "p"):
            y0 = q0 if p == "s" else 1.5 ** 2 / q0
            ratio = length * y0 if p == "s" else length / y0
            reflectance = ratio ** 2 / (4 + ratio ** 2)
            self.assertRta(coherent_rta(550, [1.5, 1, 1.5], [100], polarization=p, beta=1),
                           (reflectance, 1 - reflectance, 0))
            self.assertRta(coherent_rta(550, [1.5, 1, 1.5], [0], polarization=p, beta=1), (0, 1, 0))

    def test_absorbing_substrate_matches_independent_phase_average(self):
        # Characteristic matrices with randomized substrate phase while keeping
        # physical attenuation fixed: independent oracle, no intensity cascade.
        n=np.array([1,2.1+.04j,1.5+.007j,1.7+.06j,1.1],complex)
        d=[100,6000,80]; wave=550
        for angle in (0,45):
            beta=math.sin(math.radians(angle)); q=np.sqrt(n*n-beta*beta+0j)
            for pol in ('s','p'):
                y=q if pol=='s' else n*n/q
                expected=[]
                for phase in np.linspace(0,2*math.pi,1024,endpoint=False):
                    matrix=np.eye(2,dtype=complex)
                    for i,thickness in enumerate(d,1):
                        delta=2*math.pi*q[i]*thickness/wave
                        if i==2: delta=complex(phase,delta.imag)
                        c,z=np.cos(delta),np.sin(delta)
                        matrix=matrix@np.array([[c,-1j*z/y[i]],[-1j*y[i]*z,c]])
                    a=matrix[0,0]+matrix[0,1]*y[-1]; b=matrix[1,0]+matrix[1,1]*y[-1]
                    r=(y[0]*a-b)/(y[0]*a+b); t=2*y[0]/(y[0]*a+b)
                    expected.append((abs(r)**2,y[-1].real/y[0].real*abs(t)**2))
                er,et=np.mean(expected,axis=0)
                actual=incoherent_plate_rta(wave,n[0],n[2],n[-1],[n[1]],[d[0]],[n[3]],[d[2]],d[1],angle,pol)
                self.assertRta(actual,(er,et,1-er-et),tolerance=2e-11)
        weak=incoherent_plate_rta(550,1,1.5+.001j,1,substrate_thickness_nm=1000)
        thick=incoherent_plate_rta(550,1,1.5+.001j,1,substrate_thickness_nm=10000)
        self.assertLess(thick['T'],weak['T']); self.assertGreater(thick['A'],weak['A'])

    def test_explicit_unsupported_and_invalid_inputs(self):
        for args in ((550, [1 + .01j, 1.5], []), (0, [1, 1.5], []),
                     (550, [1, 2 - .1j, 1.5], [100]), (550, [1, 2, 1.5], [-1])):
            with self.assertRaises(ValueError):
                coherent_rta(*args)
        with self.assertRaisesRegex(ValueError, "propagating"):
            incoherent_plate_rta(550, 1.5, 1, 1.5, angle_deg=60)
        with self.assertRaises(ValueError):
            coherent_rta(550, [1, 1.5], [], beta=.5 + .1j)


if __name__ == "__main__":
    unittest.main()
