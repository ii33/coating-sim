"""Tests for verifying frozen algorithm sources against committed Git blobs."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.film_synthesis_git_source import verify_git_source


COMMIT = "1" * 40


def _git_blob_id(data: bytes) -> str:
    header = b"blob " + str(len(data)).encode("ascii") + b"\0"
    return hashlib.sha1(header + data).hexdigest()


def _mock_git(root: Path, commit: str, blobs: dict[str, bytes]):
    """Build a deterministic Git command stub backed by committed blob data."""
    blob_ids = {path: _git_blob_id(data) for path, data in blobs.items()}
    by_oid = {blob_ids[path]: data for path, data in blobs.items()}

    def git(_repo_root: Path, args: list[str], input: bytes | None = None) -> bytes:
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return (str(root) + "\n").encode("utf-8")
        if args[:2] == ["rev-parse", "HEAD"]:
            return (commit + "\n").encode("ascii")
        if args and args[0] == "ls-tree":
            requested = []
            if "--" in args:
                requested = args[args.index("--") + 1:]
            else:
                requested = list(blobs)
            text_rows: list[str] = []
            for path in requested:
                if path in blob_ids:
                    text_rows.append(f"100644 blob {blob_ids[path]}\t{path}\n")
            return "".join(text_rows).encode("utf-8")
        if args and args[0] == "cat-file" and "--batch" in args:
            blob_chunks: list[bytes] = []
            for oid in (input or b"").splitlines():
                data = by_oid[oid.decode("ascii")]
                blob_chunks.append(oid + b" blob " + str(len(data)).encode("ascii") + b"\n")
                blob_chunks.append(data + b"\n")
            return b"".join(blob_chunks)
        raise AssertionError(f"Unexpected git command: {args!r}")

    return git, blob_ids


class FilmSynthesisGitSourceTests(unittest.TestCase):
    def test_committed_text_matches_crlf_worktree_and_ignores_unrelated_dirty_file(self):
        committed = b"VALUE = 7\nnext_line()\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "coat_sim" / "core" / "solver.py"
            source.parent.mkdir(parents=True)
            source.write_bytes(committed.replace(b"\n", b"\r\n"))
            (root / "unrelated-untracked.txt").write_text("dirty but irrelevant", encoding="utf-8")
            git, blob_ids = _mock_git(root, COMMIT, {"coat_sim/core/solver.py": committed})
            with patch("tools.film_synthesis_git_source._git", side_effect=git):
                identity = verify_git_source(root, COMMIT, ["coat_sim/core/solver.py"],
                                             required_paths=("coat_sim/core/solver.py",))

        expected_hash = hashlib.sha256(committed).hexdigest()
        self.assertEqual(identity["source_kind"], "git")
        self.assertEqual(identity["git_commit"], COMMIT)
        self.assertEqual(identity["source_sha256"], identity["bundle_source_sha256"])
        self.assertEqual(identity["source_hashes"], {"coat_sim/core/solver.py": expected_hash})
        self.assertEqual(identity["git_blob_ids"], blob_ids)

    def test_semantic_worktree_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "solver.py"
            source.write_text("VALUE = 8\n", encoding="utf-8")
            git, _ = _mock_git(root, COMMIT, {"solver.py": b"VALUE = 7\n"})
            with patch("tools.film_synthesis_git_source._git", side_effect=git):
                with self.assertRaises(ValueError):
                    verify_git_source(root, COMMIT, ["solver.py"], required_paths=("solver.py",))

    def test_required_missing_path_and_head_mismatch_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git, _ = _mock_git(root, COMMIT, {})
            with patch("tools.film_synthesis_git_source._git", side_effect=git):
                with self.assertRaises(ValueError):
                    verify_git_source(root, COMMIT, [], required_paths=("required.py",))
            source = root / "solver.py"
            source.write_text("VALUE = 7\n", encoding="utf-8")
            git, _ = _mock_git(root, "2" * 40, {"solver.py": b"VALUE = 7\n"})
            with patch("tools.film_synthesis_git_source._git", side_effect=git):
                with self.assertRaises(ValueError):
                    verify_git_source(root, COMMIT, ["solver.py"])

    def test_existing_but_untracked_required_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "solver.py").write_text("VALUE = 7\n", encoding="utf-8")
            git, _ = _mock_git(root, COMMIT, {})
            with patch("tools.film_synthesis_git_source._git", side_effect=git):
                with self.assertRaises(ValueError):
                    verify_git_source(root, COMMIT, ["solver.py"], required_paths=("solver.py",))

    def test_paths_that_escape_repository_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git, _ = _mock_git(root, COMMIT, {})
            with patch("tools.film_synthesis_git_source._git", side_effect=git):
                with self.assertRaises(ValueError):
                    verify_git_source(root, COMMIT, ["../outside.py"])
                with self.assertRaises(ValueError):
                    verify_git_source(root, COMMIT, [str(root / "outside.py")])
                with self.assertRaises(ValueError):
                    verify_git_source(root, "not-a-full-sha", [])


if __name__ == "__main__":
    unittest.main()
