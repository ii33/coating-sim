import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from coat_sim.core.compute_tasks import ComputeTaskStore, queue_job_is_recent


class QueueJobsPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ComputeTaskStore(Path(self.temp.name))
        self.now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        self.cutoff = self.now - timedelta(hours=24)
        self._insert_jobs()

    def tearDown(self):
        self.temp.cleanup()

    def _insert(self, job_id, state, finished_at=None, *, private=False):
        payload = {
            "job_id": job_id,
            "state": state,
            "summary": job_id,
            "created_at": self.now.isoformat(),
            "finished_at": finished_at,
        }
        if private:
            payload["_private_test"] = "never return this field"
        with self.store.connection() as connection:
            connection.execute(
                "INSERT INTO queue_jobs(job_id,payload,state,created_at) VALUES(?,?,?,?)",
                (job_id, json.dumps(payload), state, payload["created_at"]))

    def _insert_jobs(self):
        # More than the legacy 100-row snapshot cap, so loading all pages is
        # required to prove the history view does not silently truncate.
        for index in range(137):
            self._insert(f"old-{index:03}", "succeeded",
                         (self.now - timedelta(days=2, seconds=index)).isoformat())
        self._insert("old-without-finish", "failed")
        self._insert("old-invalid-finish", "cancelled", "not-a-timestamp")
        self._insert("at-cutoff", "succeeded", self.cutoff.isoformat())
        self._insert("inside-window", "failed",
                     (self.cutoff + timedelta(microseconds=1)).isoformat())
        self._insert("outside-window", "succeeded",
                     (self.cutoff - timedelta(microseconds=1)).isoformat())
        self._insert("offset-at-cutoff", "cancelled",
                     self.cutoff.astimezone(timezone(timedelta(hours=8))).isoformat())
        self._insert("future-finish", "cancelled",
                     (self.now + timedelta(seconds=1)).isoformat())
        self._insert("old-active-running", "running", private=True)
        self._insert("old-active-queued", "queued")

    def _all_ids(self, bucket, limit):
        ids, cursors = [], []
        cursor = None
        while True:
            page = self.store.queue_jobs_page(
                bucket, before=cursor, limit=limit, now=self.now)
            ids.extend(job["job_id"] for job in page["jobs"])
            if not page["has_more"]:
                self.assertIsNone(page["next_cursor"])
                break
            cursor = page["next_cursor"]
            cursors.append(cursor)
        self.assertEqual(len(cursors), len(set(cursors)))
        return ids

    def test_recent_bucket_includes_all_active_and_24_hour_boundary(self):
        recent = self._all_ids("recent", limit=2)
        self.assertEqual(set(recent), {
            "at-cutoff", "offset-at-cutoff", "inside-window", "future-finish",
            "old-active-running", "old-active-queued",
        })
        self.assertEqual(len(recent), len(set(recent)))
        first = self.store.queue_jobs_page("recent", limit=2, now=self.now)
        self.assertTrue(first["has_more"])
        self.assertEqual(len(first["jobs"]), 2)
        self.assertTrue(all(not key.startswith("_") for key in first["jobs"][0]))

    def test_history_pages_cover_every_older_terminal_without_gaps(self):
        history = self._all_ids("history", limit=17)
        expected = {f"old-{index:03}" for index in range(137)}
        expected.update({"old-without-finish", "old-invalid-finish", "outside-window"})
        self.assertEqual(set(history), expected)
        self.assertEqual(len(history), len(expected))
        self.assertGreater(len(history), 100)

    def test_buckets_are_disjoint_and_page_cursor_is_validated(self):
        recent = set(self._all_ids("recent", limit=3))
        history = set(self._all_ids("history", limit=19))
        self.assertFalse(recent & history)
        with self.assertRaises(ValueError):
            self.store.queue_jobs_page("all", now=self.now)
        with self.assertRaises(ValueError):
            self.store.queue_jobs_page("history", before="bad", now=self.now)

    def test_sql_filter_and_view_rule_agree_without_mutating_records(self):
        with self.store.connection() as connection:
            original = [tuple(row) for row in connection.execute(
                "SELECT rowid,job_id,payload,state FROM queue_jobs ORDER BY rowid")]
        for bucket in ("recent", "history"):
            ids = set(self._all_ids(bucket, limit=13))
            for _, job_id, payload, _ in original:
                self.assertEqual(job_id in ids,
                    queue_job_is_recent(json.loads(payload), self.now) == (bucket == "recent"))
        with self.store.connection() as connection:
            self.assertEqual(original, [tuple(row) for row in connection.execute(
                "SELECT rowid,job_id,payload,state FROM queue_jobs ORDER BY rowid")])

    def test_first_recent_page_also_exposes_all_old_active_jobs(self):
        for index in range(80):
            self._insert(f"new-terminal-{index}", "succeeded", self.now.isoformat())
        page = self.store.queue_jobs_page("recent", limit=3, now=self.now)
        self.assertTrue(all(row["state"] == "succeeded" for row in page["jobs"]))
        self.assertEqual({row["job_id"] for row in page["active_jobs"]},
                         {"old-active-running", "old-active-queued"})
        next_page = self.store.queue_jobs_page("recent", before=page["next_cursor"],
                                              limit=3, now=self.now)
        self.assertEqual(next_page["active_jobs"], [])


if __name__ == "__main__":
    unittest.main()
