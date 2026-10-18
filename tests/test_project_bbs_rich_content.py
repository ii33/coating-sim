"""Rich BBS source and managed attachment references survive every revision."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from coat_sim.core.project_bbs import ProjectBbsService, normalize_bbs_post
from coat_sim.core.project_bbs_attachments import import_attachment
from coat_sim.core.project_store import ProjectError, ProjectStore
from coat_sim.project_management import ProjectManagementService
from coat_sim.project_package import export_project, import_project

pytestmark = pytest.mark.contract


@pytest.fixture
def project(tmp_path):
    return ProjectStore.create(tmp_path / "project", "Rich BBS")


def _import(store, tmp_path, name="report.txt", content="实验来源和结果"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return import_attachment(store, path)


def test_markdown_is_explicit_source_plain_history_remains_compatible(project):
    service = ProjectBbsService(project)
    raw = "# 研究\n\n**重要**\n\n<script>alert(1)</script>\n![remote](https://invalid.example/a.png)"
    post = service.create_post("research_report", "Report", raw, body_format="markdown")
    assert post["body"] == raw and post["body_format"] == "markdown"
    assert post["attachments"] == []
    updated = service.update_post(post["post_id"], "Report", "Next body", expected_post_revision=1)
    assert updated["body_format"] == "markdown"
    plain = service.update_post(post["post_id"], "Plain", "**literal**", expected_post_revision=2, body_format="plain")
    assert plain["body_format"] == "plain"
    assert [row["body_format"] for row in service.history(post["post_id"])["revisions"]] == ["markdown", "markdown", "plain"]
    with pytest.raises(ProjectError, match="plain or markdown"):
        service.create_post("journal", "Unsafe format", "raw", body_format="html")
    legacy = service.create_post("journal", "Legacy", "**literal old text**")
    records, data = project._read_all()
    row = data[legacy["post_id"]]["history"][0]
    del row["body_format"]
    del row["attachments"]
    assert normalize_bbs_post(data[legacy["post_id"]]) == data[legacy["post_id"]]
    project._publish(records, data)  # Construct the exact pre-rich-content on-disk schema.
    reopened = ProjectBbsService(ProjectStore.open(project.project_file))
    assert reopened.read_post(legacy["post_id"])["body_format"] == "plain"
    assert reopened.history(legacy["post_id"])["revisions"][0]["attachments"] == []
    before = reopened.store.manifest["revision"]
    reopened.update_post(legacy["post_id"], legacy["title"], legacy["body"], expected_post_revision=1)
    assert reopened.store.manifest["revision"] == before


def test_attached_files_are_owned_and_history_dependencies_survive_detach_and_export(project, tmp_path):
    attachment = _import(project, tmp_path)
    service = ProjectBbsService(project)
    post = service.create_post("research_report", "Evidence", "See attachment", body_format="markdown", attachments=[attachment])
    assert post["attachments"] == [attachment]
    source = tmp_path / "report.txt"
    source.unlink()
    next_post = service.update_post(post["post_id"], "Evidence", "Updated source", expected_post_revision=1)
    assert next_post["attachments"] == [attachment]
    detached = service.update_post(post["post_id"], "Evidence", "No current attachment", expected_post_revision=2, attachments=[])
    assert detached["attachments"] == []
    row = next(row for row in project.manifest["documents"] if row["id"] == post["post_id"])
    assert row["dependencies"] == [attachment["asset_id"]]
    history = service.history(post["post_id"])["revisions"]
    assert history[0]["attachments"] == [attachment] and history[-1]["attachments"] == []
    with pytest.raises(ProjectError, match="Referenced"):
        project.remove_document(attachment["asset_id"])
    exported = project.export_document(post["post_id"], tmp_path / "post-export")
    assert {row["id"] for row in exported["documents"]} == {post["post_id"], attachment["asset_id"]}
    package = tmp_path / "rich.cproject"
    export_project(project, package)
    imported = import_project(package, tmp_path / "copy")
    assert ProjectBbsService(imported).history(post["post_id"]) == service.history(post["post_id"])
    from coat_sim.core.project_bbs_attachments import read_attachment
    assert read_attachment(imported, attachment)[1].decode() == "实验来源和结果"


def test_attachment_refs_reject_other_projects_tampering_paths_and_duplicates(project, tmp_path):
    attachment = _import(project, tmp_path)
    service = ProjectBbsService(project)
    unrelated = ProjectStore.create(tmp_path / "other", "Other")
    for descriptor in (attachment | {"asset_id": "f" * 32}, attachment | {"sha256": "0" * 64},
                       attachment | {"size": attachment["size"] + 1}, attachment | {"name": "../../secret"},
                       attachment | {"name": "renamed.txt"}, attachment | {"mime": "image/png"},
                       attachment | {"path": "/etc/passwd"}):
        with pytest.raises(ProjectError):
            service.create_post("journal", "Bad", "Invalid attachment", attachments=[descriptor])
    with pytest.raises(ProjectError):
        ProjectBbsService(unrelated).create_post("journal", "Wrong project", "No", attachments=[attachment])
    with pytest.raises(ProjectError):
        service.create_post("journal", "Repeated", "No", attachments=[attachment, attachment])
    with pytest.raises(ProjectError, match="eight"):
        service.create_post("journal", "Too many", "No", attachments=[attachment] * 9)
    assert len(project.manifest["documents"]) == 1
    assert unrelated.manifest["revision"] == 0


def test_attachment_import_requires_authorized_root_revision_and_is_separate_from_comment(project, tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("input")
    manager = ProjectManagementService(authorized_roots=[project.root])
    args = {"root": str(project.project_file), "project_id": project.manifest["id"],
            "expected_revision": 0, "operation_id": "attachment", "file": str(source)}
    with pytest.raises(ProjectError) as outside:
        manager.execute("bbs_import_attachment", args, actor="assistant")
    assert outside.value.code == "project_root_not_authorized"
    manager = ProjectManagementService(authorized_roots=[tmp_path])
    imported = manager.execute("bbs_import_attachment", args, actor="assistant")
    assert imported["revision"] == 1 and "post" not in imported
    with pytest.raises(ProjectError) as stale:
        manager.execute("bbs_import_attachment", args, actor="assistant")
    assert stale.value.code == "stale_project"
    bound = ProjectStore.open(project.project_file)
    discussion = ProjectBbsService(bound, actor="assistant", read_only=True, permit_comment=True)
    comment = discussion.comment("Evidence comment", "Already imported asset", attachments=[imported["attachment"]], body_format="markdown")
    assert comment["post_type"] == "comment" and comment["attachments"] == [imported["attachment"]]
    params = {"root": str(project.project_file), "project_id": project.manifest["id"], "expected_revision": 2,
              "operation_id": "not-import", "title": "No hidden import", "body": "No", "file": str(source)}
    with pytest.raises(ProjectError) as unknown:
        manager.execute("bbs_comment", params, actor="assistant")
    assert unknown.value.code == "invalid_arguments"


def test_attachment_content_and_format_are_part_of_retained_revision(project, tmp_path):
    first = _import(project, tmp_path, "one.txt", "first source")
    second = _import(project, tmp_path, "two.txt", "second source")
    service = ProjectBbsService(project)
    post = service.create_post("work_report", "Report", "Same body", attachments=[first])
    updated = service.update_post(post["post_id"], "Report", "Same body", expected_post_revision=1,
                                  attachments=[second], body_format="markdown")
    assert updated["revision"] == 2
    revisions = service.history(post["post_id"])["revisions"]
    assert revisions[0]["attachments"] == [first]
    assert revisions[1]["attachments"] == [second]
    captured = deepcopy(revisions)
    revisions[0]["attachments"][0]["name"] = "mutable-response"
    assert service.history(post["post_id"])["revisions"] == captured
    row = next(row for row in project.manifest["documents"] if row["id"] == post["post_id"])
    assert set(row["dependencies"]) == {first["asset_id"], second["asset_id"]}
    saved = json.loads((project.root / row["path"]).read_text())
    assert saved["history"][1]["body_format"] == "markdown"
