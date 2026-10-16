"""Regression coverage for frozen vectorized coherent-stack evaluation."""
from concurrent.futures import CancelledError
import unittest
from unittest.mock import patch

import numpy as np

from coat_sim.core.film_optics import coherent_rta
from coat_sim.core.film_optics_batch import FrozenCoherentStack


class FrozenCoherentStackTests(unittest.TestCase):
    def assert_matches_scalar(self, waves, indices, thickness, angle=0,
                              polarization="unpolarized", beta=None, tolerance=1e-12):
        prepared = FrozenCoherentStack.prepare(
            waves, indices, angle, polarization, beta=beta)
        actual = prepared.evaluate(thickness)
        expected = np.empty_like(actual)
        for candidate, row in enumerate(thickness):
            for wave_index, wave in enumerate(np.atleast_1d(waves)):
                if beta is None:
                    result = coherent_rta(wave, np.asarray(indices)[wave_index]
                                          if np.asarray(indices).ndim == 2 else indices,
                                          row, angle, polarization)
                else:
                    beta_array = np.asarray(beta)
                    wave_beta = beta_array.item() if beta_array.ndim == 0 else beta_array[wave_index]
                    result = coherent_rta(wave, np.asarray(indices)[wave_index]
                                          if np.asarray(indices).ndim == 2 else indices,
                                          row, angle, polarization, beta=wave_beta)
                expected[candidate, wave_index] = [result[key] for key in ("R", "T", "A")]
        np.testing.assert_allclose(actual, expected, rtol=tolerance, atol=tolerance)
        return prepared, actual

    def test_random_dispersive_stacks_all_polarizations(self):
        rng = np.random.default_rng(88421)
        waves = np.linspace(410.0, 760.0, 9)
        for layers in (0, 1, 3, 8):
            indices = np.empty((waves.size, layers + 2), dtype=np.complex128)
            indices[:, 0] = 1.0
            for layer in range(layers):
                indices[:, layer + 1] = (rng.uniform(1.2, 2.7, waves.size) +
                                         1j * rng.uniform(0, .12, waves.size))
            indices[:, -1] = rng.uniform(1.1, 1.7, waves.size)
            thickness = rng.uniform(0, 450, (13, layers))
            for polarization in ("s", "p", "unpolarized"):
                self.assert_matches_scalar(waves, indices, thickness, 37,
                                           polarization, tolerance=8e-13)

    def test_scalar_matches_single_wave_batch_for_sixty_layer_exact_angle_cases(self):
        """The public scalar path and 1-wave vector path agree on long stacks."""
        wave = 537.123
        layer = (1.55 + .35 * np.sin(np.arange(60) * .37) +
                 1j * (.005 + .002 * (np.arange(60) % 7)))
        thickness = 35.0 + 2.5 * (np.arange(60) % 11)
        ordinary = np.r_[1.0, layer, 1.47]
        reversed_incidence = ordinary[::-1].copy()
        reversed_thickness = thickness[::-1].copy()

        absorbing_layer = np.full(60, 1.72 + .04j, dtype=np.complex128)
        absorbing_layer[[11, 42]] = [.22 + 3.1j, .3 + 2.4j]
        absorbing_thickness = np.full(60, 65.0)
        absorbing_thickness[42] = 1e6
        absorbing = np.r_[1.0, absorbing_layer, 1.5 + .03j]

        critical_layer = np.full(60, 1.67 + .01j, dtype=np.complex128)
        critical_layer[29] = 1.25  # beta equals this layer's index exactly
        critical = np.r_[1.5, critical_layer, 1.6]

        tir_layer = np.full(60, 1.61 + .015j, dtype=np.complex128)
        tir = np.r_[1.5, tir_layer, 1.0]
        cases = (
            (ordinary, thickness, 43.217, None),
            (reversed_incidence, reversed_thickness, 28.319, None),
            (absorbing, absorbing_thickness, 51.137, None),
            (critical, thickness, None, 1.25),
            (tir, thickness, None, 1.25),
        )

        for indices, depths, angle, beta in cases:
            for polarization in ("s", "p", "unpolarized"):
                kwargs = {"polarization": polarization}
                if beta is None:
                    kwargs["angle_deg"] = angle
                else:
                    kwargs["beta"] = beta
                with np.errstate(over="raise", invalid="raise", under="ignore"):
                    scalar = coherent_rta(wave, indices, depths, **kwargs)
                    batch_kwargs = dict(kwargs)
                    if beta is not None:
                        batch_kwargs["beta"] = np.array([beta])
                    batch = coherent_rta(np.array([wave]), indices, depths,
                                         **batch_kwargs)
                np.testing.assert_allclose(
                    [scalar[key] for key in ("R", "T", "A")],
                    [batch[key][0] for key in ("R", "T", "A")],
                    rtol=1e-12, atol=1e-12,
                    err_msg=f"{polarization=} {angle=} {beta=}")
                self.assertEqual(batch["wavelength_nm"], [wave])

        # The exit medium is evanescent in this case, so the finite-stack
        # transmission into that half-space must remain exactly zero.
        for polarization in ("s", "p", "unpolarized"):
            self.assertEqual(coherent_rta(wave, tir, thickness, beta=1.25,
                                          polarization=polarization)["T"], 0.0)

    def test_absorbing_extreme_absorption_and_absorbing_exit(self):
        waves = np.array([430.0, 550.0, 700.0])
        indices = np.array([[1, 2.1 + .08j, .2 + 3j, 1.45 + .04j],
                            [1, 2.0 + .11j, .25 + 2.8j, 1.50 + .03j],
                            [1, 1.9 + .07j, .18 + 3.2j, 1.55 + .02j]])
        thickness = np.array([[0, 0], [100, 1e6], [300, 1e7], [25, 80]], dtype=float)
        for polarization in ("s", "p", "unpolarized"):
            with np.errstate(over="raise", invalid="raise", under="ignore"):
                self.assert_matches_scalar(waves, indices, thickness, 62, polarization,
                                           tolerance=9e-13)

    def test_critical_q_zero_and_evanescent_exit_fallback(self):
        waves = np.array([500.0, 550.0, 600.0])
        # At the middle wavelength beta == n_layer exactly, forcing q == 0.
        indices = np.tile(np.array([1.5, 1.0, 1.5]), (3, 1))
        beta = np.array([.7, 1.0, .8])
        thickness = np.array([[0.0], [40.0], [100.0], [270.0]])
        for polarization in ("s", "p", "unpolarized"):
            self.assert_matches_scalar(waves, indices, thickness, 0, polarization,
                                       beta=beta, tolerance=9e-13)

        adjacent = FrozenCoherentStack.prepare(550, [1.5, 1.0, 1.0, 1.5], beta=1.0)
        self.assertTrue(adjacent.critical[0])
        self.assertFalse(adjacent.critical.flags.writeable)
        adjacent_values = adjacent.evaluate(np.array([[0.0, 0.0], [25.0, 80.0],
                                                       [100.0, 100.0]]))
        for candidate, row in enumerate(([0.0, 0.0], [25.0, 80.0], [100.0, 100.0])):
            result = coherent_rta(550, [1.5, 1.0, 1.0, 1.5], row, beta=1.0)
            np.testing.assert_allclose(adjacent_values[candidate, 0],
                                       [result[key] for key in ("R", "T", "A")],
                                       rtol=9e-13, atol=9e-13)

        evanescent = FrozenCoherentStack.prepare(550.0, [1.5, 1.0], 60, "unpolarized")
        actual = evanescent.evaluate(np.empty((4, 0)))
        expected = np.array([coherent_rta(550, [1.5, 1.0], [], 60)[key]
                             for key in ("R", "T", "A")])
        np.testing.assert_allclose(actual[:, 0, :], np.tile(expected, (4, 1)),
                                   rtol=2e-15, atol=2e-15)

    def test_zero_thickness_and_chunk_size_are_stable(self):
        waves = np.linspace(400, 800, 17)
        indices = [1.0, 2.4 + .04j, 1.35, 1.52]
        thickness = np.array([[0, 0], [0, 80], [95, 0], [0, 0], [110, 73]], dtype=float)
        stack = FrozenCoherentStack.prepare(waves, indices, 46, "p")
        large = stack.evaluate(thickness, chunk_bytes=2**30)
        small = stack.evaluate(thickness, chunk_bytes=1)
        np.testing.assert_array_equal(small, large)
        self.assert_matches_scalar(waves, indices, thickness, 46, "p", tolerance=8e-13)

    def test_frozen_arrays_are_independent_readonly_copies(self):
        waves = np.array([500.0, 600.0])
        indices = np.array([[1, 2 + .1j, 1.5], [1, 2.1 + .2j, 1.52]])
        stack = FrozenCoherentStack.prepare(waves, indices, 20, "unpolarized")
        waves[:] = 1
        indices[:] = 1
        self.assertEqual(stack.waves.dtype, np.float64)
        self.assertEqual(stack.indices.dtype, np.complex128)
        self.assertEqual(stack.q.dtype, np.complex128)
        self.assertFalse(stack.waves.flags.writeable)
        self.assertFalse(stack.indices.flags.writeable)
        self.assertFalse(stack.transverse.flags.writeable)
        self.assertFalse(stack.q.flags.writeable)
        self.assertEqual(stack.polarizations, ("s", "p"))
        self.assertEqual(stack.internal_layer_count, 1)
        self.assertTrue(np.array_equal(stack.beta, stack.transverse))
        with self.assertRaises(ValueError):
            stack.q[0, 0] = 9
        with self.assertRaises(ValueError):
            stack.q.setflags(write=True)
        with self.assertRaises(ValueError):
            stack.interfaces["s"][0].setflags(write=True)
        with self.assertRaises(TypeError):
            stack.interfaces["new"] = stack.interfaces["s"]
        for values in stack.interfaces.values():
            self.assertEqual(values[0].shape, (2, 2))
            self.assertFalse(values[0].flags.writeable)
            self.assertFalse(values[1].flags.writeable)
        for values in stack.fluxes.values():
            self.assertEqual(values[0].shape, (2,))
            self.assertFalse(values[0].flags.writeable)

    def test_cancellation_and_input_validation(self):
        stack = FrozenCoherentStack.prepare([500, 600], [1, 2, 1.5])
        thickness = np.ones((5, 1))
        calls = 0

        def cancel_after_first_block():
            nonlocal calls
            calls += 1
            return calls >= 3

        with self.assertRaises(CancelledError):
            stack.evaluate(thickness, chunk_bytes=1, cancel=cancel_after_first_block)
        with self.assertRaises(CancelledError):
            stack.evaluate(thickness, cancel=True)
        for invalid in (np.ones(1), np.ones((2, 2)), np.array([[np.nan]]), np.array([[-1.]])):
            with self.assertRaises(ValueError):
                stack.evaluate(invalid)
        for invalid in (0, -1, 1.5, True):
            with self.assertRaises(ValueError):
                stack.evaluate(thickness, chunk_bytes=invalid)

    def test_small_budget_tiles_wavelengths_once_and_polls_cancel(self):
        waves = np.linspace(400.0, 800.0, 1001)
        stack = FrozenCoherentStack.prepare(waves, [1.0, 2.0 + .02j, 1.5],
                                            polarization="s")
        visited = []
        original = FrozenCoherentStack._evaluate_regular

        def recording(self, thickness, wave_indices, polarization, *, cancel=None):
            if self is stack:
                visited.extend(wave_indices.tolist())
            return original(self, thickness, wave_indices, polarization, cancel=cancel)

        with patch.object(FrozenCoherentStack, "_evaluate_regular", recording):
            actual = stack.evaluate(np.array([[73.0]]), chunk_bytes=1024)
        self.assertEqual(len(visited), waves.size)
        np.testing.assert_array_equal(np.sort(visited), np.arange(waves.size))
        expected = np.array([
            [coherent_rta(wave, [1.0, 2.0 + .02j, 1.5], [73.0],
                          polarization="s")[key] for key in ("R", "T", "A")]
            for wave in waves])
        np.testing.assert_allclose(actual[0], expected, rtol=9e-13, atol=9e-13)

        calls = 0

        def stop_during_wave_tiles():
            nonlocal calls
            calls += 1
            return calls >= 4

        with self.assertRaises(CancelledError):
            stack.evaluate(np.array([[73.0]]), chunk_bytes=1024,
                           cancel=stop_during_wave_tiles)

        critical = FrozenCoherentStack.prepare(550.0, [1.5, 1.0, 1.5], beta=1.0,
                                               polarization="s")
        critical_calls = 0

        def stop_during_critical_points():
            nonlocal critical_calls
            critical_calls += 1
            return critical_calls >= 4

        with self.assertRaises(CancelledError):
            critical.evaluate(np.array([[20.0]]), cancel=stop_during_critical_points)

        final_calls = 0

        def stop_at_final_check():
            nonlocal final_calls
            final_calls += 1
            return final_calls >= 4

        with self.assertRaises(CancelledError):
            FrozenCoherentStack.prepare(550.0, [1.0, 2.0, 1.5],
                                        polarization="s").evaluate(
                np.array([[20.0]]), cancel=stop_at_final_check)

    def test_prepare_reuses_cpu_validation_contract(self):
        invalid = (
            (0, [1, 1.5]),
            (550, [1 + .01j, 1.5]),
            (550, [1, 2 - .1j, 1.5]),
            (550, [1],),
        )
        for wave, indices in invalid:
            with self.assertRaises(ValueError):
                FrozenCoherentStack.prepare(wave, indices)
        with self.assertRaises(ValueError):
            FrozenCoherentStack.prepare(550, [1, 1.5], beta=.5 + .1j)
        with self.assertRaises(ValueError):
            FrozenCoherentStack.prepare(550, [1, 1.5], polarization="x")


if __name__ == "__main__":
    unittest.main()
