"""Verify the active algorithm files against an explicitly selected Git commit.

Read-only Git commands; unrelated worktree edits are permitted. Text hashes use
LF-normalized UTF-8 bytes so a Windows CRLF checkout and Linux LF checkout agree.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


def _git(repo_root: Path, args: list[str], input: bytes | None = None) -> bytes:
    try:
        return subprocess.run(["git", *args], cwd=repo_root, input=input,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Read-only Git source identity check failed") from exc


def _text(data: bytes) -> bytes:
    data.decode("utf-8", errors="strict")
    return data.replace(b"\r\n", b"\n")


def verify_git_source(repo_root: Path, expected_commit: str, paths: list[str], *,
                      required_paths: tuple[str, ...] = ()) -> dict:
    repo_root = Path(repo_root).resolve(strict=True)
    if not isinstance(expected_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", expected_commit):
        raise ValueError("Git mode requires an exact lowercase 40-character --expect-commit SHA")
    if (not isinstance(paths, list) or not paths or any(not isinstance(p, str) for p in paths)
            or len(set(paths)) != len(paths)):
        raise ValueError("Git source paths must be a nonempty unique list")
    if not set(required_paths).issubset(paths):
        raise ValueError("Required algorithm/manifest source paths are missing")
    for relative in paths:
        parts = PurePosixPath(relative).parts
        if (not re.fullmatch(r"[A-Za-z0-9_./-]+", relative) or not parts
                or PurePosixPath(relative).is_absolute() or ".." in parts
                or relative != PurePosixPath(relative).as_posix()):
            raise ValueError("Git source path is not a safe repository-relative path")
        source = repo_root / relative
        if (not source.is_file() or source.is_symlink()
                or not source.resolve().is_relative_to(repo_root)):
            raise ValueError(f"Git source is missing or escapes repository: {relative}")
    head = _git(repo_root, ["rev-parse", "HEAD"]).decode("ascii").strip()
    if head != expected_commit:
        raise ValueError("Current HEAD differs from --expect-commit")
    toplevel = Path(_git(repo_root, ["rev-parse", "--show-toplevel"]).decode("utf-8").strip()).resolve()
    if toplevel != repo_root:
        raise ValueError("--repo-root must name the exact Git worktree root")
    listing = _git(repo_root, ["ls-tree", "-r", expected_commit, "--", *sorted(paths)])
    blob_ids = {}
    for line in listing.decode("utf-8").splitlines():
        meta, relative = line.split("\t", 1)
        mode, kind, oid = meta.split()
        if mode not in {"100644", "100755"} or kind != "blob" or not re.fullmatch(r"[0-9a-f]{40}", oid):
            raise ValueError("Git source path is not a regular committed blob")
        blob_ids[relative] = oid
    if set(blob_ids) != set(paths):
        raise ValueError("Source path is not tracked in the selected commit")
    ordered_oids = sorted(set(blob_ids.values()))
    raw = _git(repo_root, ["cat-file", "--batch"],
               input=("\n".join(ordered_oids) + "\n").encode("ascii"))
    offset, blobs = 0, {}
    for oid in ordered_oids:
        end = raw.find(b"\n", offset)
        if end < 0:
            raise ValueError("Incomplete Git blob response")
        header = raw[offset:end].decode("ascii").split()
        if len(header) != 3 or header[:2] != [oid, "blob"]:
            raise ValueError("Git blob identity response differs")
        size = int(header[2])
        start = end + 1
        data = raw[start:start + size]
        if size < 0 or len(data) != size or raw[start + size:start + size + 1] != b"\n":
            raise ValueError("Truncated Git blob response")
        if hashlib.sha1(b"blob " + str(size).encode() + b"\0" + data).hexdigest() != oid:
            raise ValueError("Git blob content hash differs")
        blobs[oid] = _text(data)
        offset = start + size + 1
    if offset != len(raw):
        raise ValueError("Unexpected trailing Git blob response")
    hashes = {}
    for relative in sorted(paths):
        data = _text((repo_root / relative).read_bytes())
        if data != blobs[blob_ids[relative]]:
            raise ValueError(f"Active source differs from selected Git commit: {relative}")
        hashes[relative] = hashlib.sha256(data).hexdigest()
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"source_kind": "git", "git_commit": head, "source_sha256": digest,
        "bundle_source_sha256": digest, "source_hashes": hashes, "git_blob_ids": blob_ids,
        "hash_measure": "LF-normalized UTF-8 source bytes; only CRLF is normalized",
        "scope": "declared algorithm/dependency/frozen-request files; unrelated dirty files allowed"}
