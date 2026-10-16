import unittest

from legacy.mask_designer import smooth_closed


class MaskDesignerGeometryTests(unittest.TestCase):
    def test_smooth_curve_passes_through_control_points_and_is_bounded(self):
        controls = [[0, 0], [40, 0], [40, 30], [0, 30]]
        contour = smooth_closed(controls)
        self.assertEqual(contour[0], [0.0, 0.0])
        self.assertLessEqual(len(contour), 500)
        self.assertGreater(len(contour), len(controls))
        self.assertNotEqual(contour[-1], contour[0])  # polygon closure is an implicit final edge

    def test_curve_requires_three_control_points(self):
        with self.assertRaisesRegex(ValueError, "至少需要 3"):
            smooth_closed([[0, 0], [1, 1]])


if __name__ == "__main__":
    unittest.main()
