import tempfile
import unittest
from pathlib import Path

from coat_sim.core.film_design import new_design
from coat_sim.core.film_design_store import FilmDesignStore


class FilmDesignStoreTests(unittest.TestCase):
    def test_sqlite_roundtrip_update_and_case_sensitive_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = FilmDesignStore(Path(temporary) / "library.sqlite3")
            first = new_design()
            first["id"] = "Design"
            first["name"] = "第一版"
            first["note"] = "初始备注"
            self.assertEqual(store.save(first)["note"], "初始备注")
            revised = dict(first, name="修订版", note="已更新")
            store.save(revised)
            lower = dict(first, id="design", name="大小写独立")
            store.save(lower)
            self.assertEqual(store.load("Design")["name"], "修订版")
            self.assertEqual(store.load("Design")["note"], "已更新")
            self.assertEqual(store.load("design")["name"], "大小写独立")
            rows = store.list()
            self.assertEqual({row["design_id"] for row in rows}, {"Design", "design"})
            self.assertEqual(next(row for row in rows if row["design_id"] == "Design")["note"], "已更新")
            with self.assertRaises(FileNotFoundError):
                store.load("missing")


if __name__ == "__main__":
    unittest.main()
