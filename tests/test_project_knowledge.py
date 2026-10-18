"""Unified discovery reuses saved stores without writes or cross-project scans."""
import json
from unittest.mock import patch

import pytest

from coat_sim.core import project_knowledge as knowledge
from coat_sim.core.project_bbs import ProjectBbsService
from coat_sim.core.project_dependencies import ProjectError
from coat_sim.core.project_research import ProjectResearchStore
from coat_sim.core.project_store import ProjectStore

pytestmark = pytest.mark.contract


@pytest.fixture
def project(tmp_path):
    return ProjectStore.create(tmp_path / "chosen", "Chosen")


def searcher(project):
    return knowledge.ProjectKnowledge(project.project_file, project.manifest["id"])


def evidence(project, **kwargs):
    return ProjectResearchStore(project.project_file, project.manifest["id"]).save({
        "title": "Reference", "content": "optical evidence", "source": "test_source",
        "original_url": "https://example.org/paper", "doi": "10.1234/test",
        "license": "CC-BY", "parse_status": "parsed", **kwargs})


def snapshot(project):
    return {str(path.relative_to(project.root)): path.read_bytes() for path in project.root.rglob("*") if path.is_file()}


def test_absent_sources_are_explicit_and_reads_never_create_storage(project):
    before = snapshot(project)
    result = searcher(project).search()
    assert result["results"] == [] and result["coverage"] == "complete"
    assert result["sources"]["bbs"]["state"] == "empty"
    assert result["sources"]["research"]["state"] == "not_saved"
    assert result["has_more"] is False
    assert snapshot(project) == before
    assert not (project.root / "data/research").exists()


def test_union_covers_bbs_replies_and_preserves_evidence_provenance(project):
    bbs = ProjectBbsService(project, actor="assistant")
    post = bbs.create_post("research_report", "Optical notes", "Measured claim")
    reply = bbs.reply(post["post_id"], "reply", "optical reply", expected_post_revision=1)
    saved = evidence(project, statement_kind="assistant_inference")
    before = snapshot(project)
    result = searcher(project).search("optical")
    assert result["coverage"] == "complete"
    assert {row["id"] for row in result["results"]} == {post["post_id"], reply["post_id"], saved["evidence_id"]}
    evidence_row = next(row for row in result["results"] if row["source_type"] == "research")
    assert evidence_row["license"] == "CC-BY"
    assert evidence_row["statement_kind"] == "assistant_inference"
    assert evidence_row["citation"]["content_sha256"] == saved["content_sha256"]
    assert evidence_row["read_tool"] == "project_research_read"
    note_row = next(row for row in result["results"] if row["id"] == reply["post_id"])
    assert note_row["author"] == "assistant" and note_row["parent_id"] == post["post_id"]
    assert note_row["citation"]["revision"] == 1
    assert note_row["read_tool"] == "project_bbs_read"
    assert all(row["untrusted_reference"] is True for row in result["results"])
    assert snapshot(project) == before


def test_default_excludes_archived_current_revision_and_old_body(project):
    bbs = ProjectBbsService(project)
    post = bbs.create_post("journal", "old title", "old body")
    bbs.update_post(post["post_id"], "new title", "new body", expected_post_revision=1)
    assert searcher(project).search("old body")["results"] == []
    assert searcher(project).search("new body")["results"][0]["revision"] == 2
    bbs.archive_post(post["post_id"], True, expected_post_revision=2)
    assert searcher(project).search()["results"] == []
    assert searcher(project).search(include_archived=True)["results"][0]["archived"] is True


def test_project_identity_and_records_never_cross_projects(project, tmp_path):
    other = ProjectStore.create(tmp_path / "other", "Other")
    evidence(other, title="secret other project")
    assert searcher(project).search()["results"] == []
    with pytest.raises(ProjectError) as error:
        knowledge.ProjectKnowledge(project.project_file, other.manifest["id"])
    assert error.value.code == "project_identity_mismatch"
    with pytest.raises(ProjectError) as error:
        knowledge.ProjectKnowledge(project.root, project.manifest["id"])
    assert error.value.code == "invalid_project_file"


def test_inventory_pages_continue_after_zero_matches_without_duplicates(project, monkeypatch):
    bbs = ProjectBbsService(project)
    bbs.create_post("journal", "no hit", "body")
    expected = evidence(project, title="needle")
    monkeypatch.setattr(knowledge, "MAX_SCAN_RECORDS", 1)
    first = searcher(project).search("needle")
    assert first["results"] == [] and first["next_offset"] == 1
    assert first["coverage"] == "partial"
    assert first["budget"]["stopped_reason"] == "record_budget"
    second = searcher(project).search("needle", offset=first["next_offset"], expected_version=first["version"])
    assert second["results"][0]["id"] == expected["evidence_id"]
    assert second["next_offset"] is None
    assert second["coverage"] == "partial"  # A suffix page is not the entire search.


def test_result_limit_and_snapshot_changes_are_explicit(project):
    first_saved = evidence(project)
    evidence(project, title="second")
    result = searcher(project).search(limit=1)
    assert len(result["results"]) == 1 and result["has_more"]
    second = searcher(project).search(offset=result["next_offset"], expected_version=result["version"])
    assert len({row["id"] for row in result["results"] + second["results"]}) == 2
    assert first_saved["evidence_id"] in {row["id"] for row in result["results"] + second["results"]}
    evidence(project, title="third")
    with pytest.raises(ProjectError) as error:
        searcher(project).search(offset=result["next_offset"], expected_version=result["version"])
    assert error.value.code == "knowledge_version_changed"


def test_corrupt_record_is_failure_not_empty_validated_search(project):
    saved = evidence(project)
    path = project.root / "data/research" / project.manifest["id"] / (saved["evidence_id"] + ".json")
    value = json.loads(path.read_bytes())
    value["record"]["content"] = "tampered"
    path.write_text(json.dumps(value), encoding="utf-8")
    result = searcher(project).search()
    assert result["results"] == [] and result["coverage"] == "partial"
    assert result["failures"] == [{"source_type": "research", "id": saved["evidence_id"], "code": "evidence_integrity"}]


def test_source_failure_still_reports_other_source_and_capacity(project):
    post = ProjectBbsService(project).create_post("journal", "note", "body")
    instance = searcher(project)
    with patch.object(instance.research, "_paths", side_effect=ProjectError("capacity_exceeded", "too large")):
        result = instance.search()
    assert result["results"][0]["id"] == post["post_id"]
    assert result["sources"]["research"]["state"] == "unavailable"
    assert result["sources"]["research"]["error"] == "capacity_exceeded"
    assert result["coverage"] == "partial"


def test_unsafe_research_path_fails_closed_instead_of_becoming_missing(project, tmp_path):
    evidence(project)
    directory = project.root / "data/research" / project.manifest["id"]
    (directory / "unsafe.json").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ProjectError) as error:
        searcher(project).search()
    assert error.value.code == "unsafe_path"


def test_oversized_record_skips_with_report_and_progress(project, monkeypatch):
    saved = evidence(project)
    monkeypatch.setattr(knowledge, "MAX_SCAN_BYTES", 1)
    result = searcher(project).search()
    assert result["budget"]["scanned_bytes"] == 0
    assert result["failures"] == [{"source_type": "research", "id": saved["evidence_id"], "code": "record_budget_exceeded"}]
    assert not result["has_more"] and result["coverage"] == "partial"


def test_deadline_returns_resumable_empty_page(project, monkeypatch):
    evidence(project)
    clock = iter([0.0, 4.0])
    monkeypatch.setattr(knowledge.time, "monotonic", lambda: next(clock))
    result = searcher(project).search()
    assert result["budget"]["stopped_reason"] == "time_budget"
    assert result["next_offset"] == 0 and result["results"] == []


def test_inflight_manifest_change_rejects_mixed_page(project):
    ProjectBbsService(project).create_post("journal", "note", "body")
    instance = searcher(project)
    original = instance._bbs_result
    def changing(*args, **kwargs):
        result = original(*args, **kwargs)
        fresh = ProjectStore.open(project.project_file)
        ProjectBbsService(fresh).create_post("journal", "later", "body")
        return result
    with patch.object(instance, "_bbs_result", side_effect=changing):
        with pytest.raises(ProjectError) as error:
            instance.search()
    assert error.value.code == "stale_project"


@pytest.mark.parametrize("arguments", [{"offset": True}, {"offset": -1}, {"offset": 1.0},
    {"limit": "1"}, {"limit": 0}, {"limit": 51}, {"include_archived": 1},
    {"query": "x" * 201}, {"expected_version": "invalid"}])
def test_search_strict_arguments(project, arguments):
    with pytest.raises(ProjectError) as error:
        searcher(project).search(**arguments)
    assert error.value.code == "invalid_arguments"


def test_byte_budget_stops_before_record_and_resumes_it(project, monkeypatch):
    evidence(project, title="first")
    evidence(project, title="second")
    instance = searcher(project)
    entries = instance._inventory()[0]
    monkeypatch.setattr(knowledge, "MAX_SCAN_BYTES", max(entry.size for entry in entries))
    first = instance.search()
    assert len(first["results"]) == 1 and first["budget"]["stopped_reason"] == "byte_budget"
    second = searcher(project).search(offset=first["next_offset"], expected_version=first["version"])
    assert len(second["results"]) == 1 and second["next_offset"] is None
    assert first["results"][0]["id"] != second["results"][0]["id"]


def test_missing_bbs_file_reports_failure_with_other_source_still_visible(project):
    post = ProjectBbsService(project).create_post("journal", "note", "body")
    saved = evidence(project)
    manifest_entry = next(row for row in project.manifest["documents"] if row["id"] == post["post_id"])
    (project.root / manifest_entry["path"]).unlink()
    result = searcher(project).search()
    assert [row["id"] for row in result["results"]] == [saved["evidence_id"]]
    assert result["failures"] == [{"source_type": "bbs", "id": post["post_id"], "code": "record_unavailable"}]
    assert result["coverage"] == "partial"


def test_inflight_research_append_rejects_mixed_inventory(project):
    evidence(project)
    instance = searcher(project)
    original = instance._research_result
    def changing(*args, **kwargs):
        row = original(*args, **kwargs)
        evidence(project, title="later")
        return row
    with patch.object(instance, "_research_result", side_effect=changing):
        with pytest.raises(ProjectError) as error:
            instance.search()
    assert error.value.code == "knowledge_version_changed"
