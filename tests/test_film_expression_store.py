import tempfile
from pathlib import Path
import unittest

from coat_sim.core.film_design import expand_expression, new_design
from coat_sim.core.film_design_store import FilmDesignStore
from coat_sim.core.film_expression_store import FilmExpressionTemplateStore


class FilmExpressionTemplateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "library.sqlite3"

    def tearDown(self):
        self.temporary.cleanup()

    def test_templates_are_separate_named_sqlite_records_and_designs_remain_readable(self):
        design_store = FilmDesignStore(self.database)
        existing_design = design_store.save(new_design(name="Full design", design_id="design_existing"))
        templates = FilmExpressionTemplateStore(self.database)
        template = {
            "schema_version": 1,
            "template_id": "expr_ar_stack",
            "name": "AR expression",
            "note": "Saved separately\nwith material aliases.",
            "expression": {
                "text": "g1 a\nG2 (0.5 A)^2",
                "symbols": {
                    "A": {
                        "material_id": "film-case-sensitive-ID",
                        "optical_set_id": "optical-set-01",
                        "thickness": {"value": 1.0, "unit": "qw"},
                        "note": "low-index alias",
                    },
                },
            },
        }
        saved = templates.save(template)

        self.assertEqual(saved["name"], "AR expression")
        self.assertEqual(saved["note"], "Saved separately\nwith material aliases.")
        self.assertEqual(saved["expression"]["text"], "G1 A\nG2 (0.5 A)^2")
        self.assertEqual(saved["expression"]["symbols"]["A"]["material_id"], "film-case-sensitive-ID")
        self.assertEqual(saved["expression"]["symbols"]["A"]["optical_set_id"], "optical-set-01")
        expanded = expand_expression(saved["expression"]["text"], saved["expression"]["symbols"])
        self.assertEqual(len(expanded), 3)
        self.assertEqual([row["group"] for row in expanded], ["G1", "G2", "G2"])

        self.assertEqual(templates.load("expr_ar_stack"), saved)
        self.assertEqual([row["template_id"] for row in templates.list()], ["expr_ar_stack"])
        self.assertEqual([row["design_id"] for row in design_store.list()], ["design_existing"])
        self.assertEqual(design_store.load("design_existing"), existing_design)

    def test_invalid_or_unbound_expression_is_rejected(self):
        templates = FilmExpressionTemplateStore(self.database)
        invalid = {
            "name": "Bad template",
            "expression": {"text": "A B", "symbols": {"A": {
                "material_id": "film", "optical_set_id": "set",
                "thickness": {"value": 1, "unit": "nm"},
            }}},
        }
        with self.assertRaisesRegex(ValueError, "unknown expression symbol"):
            templates.save(invalid)
        for version in (True, 1.0, "1"):
            with self.subTest(schema_version=version):
                invalid_version = {**invalid, "schema_version": version}
                with self.assertRaisesRegex(ValueError, "unsupported expression template schema_version"):
                    templates.save(invalid_version)
        with self.assertRaises(FileNotFoundError):
            templates.load("missing-template")


if __name__ == "__main__":
    unittest.main()
