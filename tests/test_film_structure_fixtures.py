"""Check the rebuilt queue fixture against an existing independent oracle."""
import numpy as np

from tests.film_structure_fixtures import fixture
from tests.test_film_optics import matrix_reference


def test_rebuilt_fixture_matches_existing_independent_matrix_reference(tmp_path):
    service, _library, design = fixture(tmp_path)
    result = service.simulate(design, {
        "wavelength_min_nm": 550.0, "wavelength_max_nm": 550.1,
        "samples": 2, "angle_deg": 0.0, "polarization": "unpolarized",
        "first_surface": "front",
    })
    for index, wavelength in enumerate(result["wavelength_nm"]):
        expected_r, expected_t = matrix_reference(wavelength, [1.0, 1.23, 1.5], [100.0], 0.0, "s")
        np.testing.assert_allclose([result["R"][index], result["T"][index]],
                                   [expected_r, expected_t], rtol=2e-12, atol=2e-12)
