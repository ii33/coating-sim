"""BBS is an attributed, append-only managed project resource, not a sidecar."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import threading
from typing import Any, cast

import pytest

from coat_sim.core.project_bbs import (BBS_KIND, MAX_BODY_BYTES, MAX_HISTORY,
                                       ProjectBbsService, normalize_bbs_post)
from coat_sim.core.project_store import ProjectError, ProjectStore
from coat_sim.project_management import ProjectManagementService
from coat_sim.project_package import export_project, import_project
from coat_sim.project_recovery import ProjectOwnerLease

pytestmark = pytest.mark.contract


@pytest.fixture
def project(tmp_path):
    return ProjectStore.create(tmp_path / "project", "BBS project")


def _post(service, post_type="journal"):
    return service.create_post(post_type, "工程日志", "阶段结果\n正文可保留 Markdown **原文**")


def _error(code, call):
    with pytest.raises(ProjectError) as error:
        call()
    assert error.value.code == code


def _args(store, **values):
    return {"root": str(store.project_file), "project_id": store.manifest["id"], **values}


def test_user_assistant_history_reply_archive_and_reload(project):
    user = ProjectBbsService(project)
    assistant = ProjectBbsService(project, actor="assistant")
    created = _post(user)
    assert created["revision"] == 1 and created["author"] == "user"
    updated = assistant.update_post(created["post_id"], "进度更新", "助手补充", expected_post_revision=1)
    assert updated["author"] == "user" and updated["editor"] == "assistant"
    assert updated["created_at"] == created["created_at"]
    assert updated["revision"] == 2 and updated["history_count"] == 2
    reply = assistant.reply(created["post_id"], "回复", "已复核", expected_post_revision=2)
    assert reply["author"] == "assistant" and reply["parent_id"] == created["post_id"]
    assert user.list_posts()["total"] == 1
    assert user.list_posts(parent_id=created["post_id"])["posts"][0]["post_id"] == reply["post_id"]
    assert user.read_post(created["post_id"])["reply_count"] == 1
    history = user.history(created["post_id"])
    assert [row["editor"] for row in history["revisions"]] == ["user", "assistant"]
    assert history["revisions"][0]["body"] == created["body"]
    history["revisions"][0]["body"] = "detached mutation"
    assert user.history(created["post_id"])["revisions"][0]["body"] == created["body"]
    _error("bbs_invalid_arguments", lambda: user.reply(reply["post_id"], "nested", "no", expected_post_revision=1))
    archived = user.archive_post(created["post_id"], True, expected_post_revision=2)
    assert archived["revision"] == 3 and user.list_posts()["total"] == 0
    assert user.list_posts(include_archived=True)["total"] == 1
    _error("bbs_archived", lambda: assistant.reply(created["post_id"], "late", "no", expected_post_revision=3))
    _error("bbs_archived", lambda: assistant.update_post(created["post_id"], "late", "no", expected_post_revision=3))
    restored = user.archive_post(created["post_id"], False, expected_post_revision=3)
    assert restored["revision"] == 4
    assert ProjectBbsService(ProjectStore.open(project.project_file)).read_post(created["post_id"]) == restored
    row = next(row for row in project.manifest["documents"] if row["id"] == created["post_id"])
    assert row["kind"] == BBS_KIND and row["path"].startswith("data/bbs/r")
    assert row["path"].endswith(".cbbs")


def test_types_search_pagination_and_noop(project):
    service = ProjectBbsService(project)
    for kind in ("journal", "progress", "research_report", "work_report"):
        _post(service, kind)
    assert service.list_posts()["total"] == 4
    assert service.list_posts(post_type="research_report")["total"] == 1
    assert service.list_posts(query="阶段结果")["total"] == 4
    assert service.list_posts(query="missing")["total"] == 0
    page = service.list_posts(offset=1, limit=2)
    assert len(page["posts"]) == 2 and "body" not in page["posts"][0]
    post = service.read_post(page["posts"][0]["post_id"])
    version = project.manifest["revision"]
    assert service.update_post(post["post_id"], post["title"], post["body"], expected_post_revision=1) == post
    assert service.archive_post(post["post_id"], False, expected_post_revision=1) == post
    assert project.manifest["revision"] == version


def test_project_isolation_and_identity(project, tmp_path):
    first = _post(ProjectBbsService(project))
    other = ProjectStore.create(tmp_path / "other", "Other")
    service = ProjectBbsService(other)
    assert service.list_posts()["total"] == 0
    for action in (lambda: service.read_post(first["post_id"]),
                   lambda: service.history(first["post_id"]),
                   lambda: service.list_posts(parent_id=first["post_id"]),
                   lambda: service.reply(first["post_id"], "x", "x", expected_post_revision=1)):
        _error("bbs_missing_post", action)
    manager = ProjectManagementService(authorized_roots=[project.root])
    _error("project_identity_mismatch", lambda: manager.execute("bbs_list", _args(project, project_id=other.manifest["id"])))
    _error("project_root_not_authorized", lambda: manager.execute("bbs_list", _args(other)))
    assert other.manifest["revision"] == 0


def test_revision_cas_concurrency_and_stale_store(project):
    service = ProjectBbsService(project)
    post = _post(service)
    obsolete = ProjectBbsService(ProjectStore.open(project.project_file))
    barrier = threading.Barrier(2)

    def edit(index):
        barrier.wait()
        try:
            return service.update_post(post["post_id"], "Concurrent", str(index), expected_post_revision=1)
        except ProjectError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, range(2)))
    assert sum(isinstance(row, dict) for row in results) == 1
    assert "stale_bbs_post" in results
    assert service.history(post["post_id"])["total"] == 2
    _error("stale_project", lambda: obsolete.create_post("journal", "Stale", "No overwrite"))
    assert len(project.manifest["documents"]) == 1


def test_owner_admission_and_owned_service_preserve_attribution(project, tmp_path):
    manager = ProjectManagementService()
    args = _args(project, operation_id="create", expected_revision=0, post_type="journal", title="Test", body="Test")
    with ProjectOwnerLease.acquire(project.project_file, project.manifest["id"]):
        _error("active_project_owner", lambda: manager.execute("bbs_create", args, actor="assistant"))
        result = manager.execute_owned("bbs_create", args, store=project, project_id=project.manifest["id"], actor="assistant")
    assert result["post"]["author"] == "assistant"
    assert result["revision"] == 1
    _error("stale_project", lambda: manager.execute("bbs_create", args))


def test_failed_publication_leaves_previous_history_and_readable_manifest(project, monkeypatch):
    service = ProjectBbsService(project)
    post = _post(service)
    manifest_before = project.project_file.read_bytes()
    old_paths = [project.root / row["path"] for row in project.manifest["documents"]]
    old_bytes = [path.read_bytes() for path in old_paths]
    from coat_sim.core import project_store
    replace = project_store.replace_with_retry

    def fail_manifest(source, target, *args, **kwargs):
        if Path(target) == project.project_file:
            raise OSError("injected interrupted manifest publication")
        return replace(source, target, *args, **kwargs)

    monkeypatch.setattr(project_store, "replace_with_retry", fail_manifest)
    _error("io_error", lambda: service.update_post(post["post_id"], "Failed", "No commit", expected_post_revision=1))
    assert project.project_file.read_bytes() == manifest_before
    assert [path.read_bytes() for path in old_paths] == old_bytes
    assert not (project.root / ".project-write.lock").exists()
    reopened = ProjectBbsService(ProjectStore.open(project.project_file))
    assert reopened.read_post(post["post_id"]) == post
    assert reopened.history(post["post_id"])["total"] == 1


def test_interrupted_orphan_generation_and_writer_lock_fail_closed(project):
    service = ProjectBbsService(project)
    post = _post(service)
    orphan = project.root / "data/bbs/r00000002"
    orphan.mkdir()
    marker = orphan / "leftover.cbbs"
    marker.write_text("uncommitted and unrelated")
    lock = project.root / ".project-write.lock"
    lock.write_text("unconfirmed owner")
    _error("project_locked", lambda: service.update_post(post["post_id"], "New", "New", expected_post_revision=1))
    assert lock.read_text() == "unconfirmed owner"
    assert marker.read_text() == "uncommitted and unrelated"
    assert ProjectBbsService(ProjectStore.open(project.project_file)).read_post(post["post_id"]) == post
    lock.unlink()  # Test-only recovery after the owner is known to be absent.
    service.update_post(post["post_id"], "New", "New", expected_post_revision=1)
    assert "r00000002-2" in project.manifest["documents"][0]["path"]
    assert marker.exists()


def test_package_and_document_export_retain_full_history_and_reply(project, tmp_path):
    service = ProjectBbsService(project)
    post = _post(service, "research_report")
    service.update_post(post["post_id"], "Research", "Revised evidence", expected_post_revision=1)
    reply = service.reply(post["post_id"], "Review", "Limitations recorded", expected_post_revision=2)
    package = tmp_path / "bbs.cproject"
    exported = export_project(project, package)
    assert exported["document_count"] == 2
    copy = import_project(package, tmp_path / "copy")
    assert copy.manifest["id"] != project.manifest["id"]
    copied = ProjectBbsService(copy)
    assert copied.history(post["post_id"]) == service.history(post["post_id"])
    assert copied.read_post(reply["post_id"]) == reply
    copied.update_post(post["post_id"], "Copy edit", "Independent", expected_post_revision=2)
    assert service.read_post(post["post_id"])["revision"] == 2
    out = project.export_document(reply["post_id"], tmp_path / "thread-export")
    assert len(out["documents"]) == 2
    serialized = json.loads((tmp_path / "thread-export" / (post["post_id"] + ".cbbs")).read_text())
    assert len(serialized["history"]) == 2


def test_generic_document_writes_and_delete_cannot_forge_or_erase_history(project):
    post = _post(ProjectBbsService(project))
    saved = project.snapshot_saved_inputs()["documents"][post["post_id"]]
    for action in (lambda: project.create_document(BBS_KIND, saved),
                   lambda: project.save_document(post["post_id"], saved),
                   lambda: project.save_documents({post["post_id"]: saved}),
                   lambda: project.write_documents(updates={post["post_id"]: saved}),
                   lambda: project.write_documents([(BBS_KIND, saved)]),
                   lambda: project.remove_document(post["post_id"])):
        _error("bbs_service_required", action)
    assert project.manifest["revision"] == 1


@pytest.mark.parametrize("args,code", [
    (("invalid", "title", "body"), "bbs_invalid_post"),
    (("journal", "", "body"), "bbs_invalid_post"),
    (("journal", "x" * 201, "body"), "capacity_exceeded"),
    (("journal", "line\nbreak", "body"), "capacity_exceeded"),
    (("journal", "title", ""), "bbs_invalid_post"),
    (("journal", "title", "x" * (MAX_BODY_BYTES + 1)), "capacity_exceeded"),
    (("journal", "title", "中" * (MAX_BODY_BYTES // 3 + 1)), "capacity_exceeded"),
    (("journal", "title", "\ud800"), "bbs_invalid_post"),
    (("journal", "title", "bad\x00text"), "bbs_invalid_post"),
])
def test_text_validation_before_publication(project, args, code):
    _error(code, lambda: ProjectBbsService(project).create_post(*args))
    assert project.manifest["revision"] == 0


@pytest.mark.parametrize("kwargs", [{"limit": True}, {"offset": -1}, {"limit": 51}, {"post_type": []},
                                     {"include_archived": 1}, {"query": "x" * 201}])
def test_list_validation(project, kwargs):
    _error("bbs_invalid_arguments", lambda: ProjectBbsService(project).list_posts(**kwargs))


def test_read_only_and_revision_validation(project):
    service = ProjectBbsService(project)
    post = _post(service)
    readonly = ProjectBbsService(project, actor="assistant", read_only=True)
    assert readonly.read_post(post["post_id"]) == post
    for action in (lambda: _post(readonly),
                   lambda: readonly.reply(post["post_id"], "x", "y", expected_post_revision=1),
                   lambda: readonly.update_post(post["post_id"], "x", "y", expected_post_revision=1),
                   lambda: readonly.archive_post(post["post_id"], True, expected_post_revision=1)):
        _error("discussion_read_only", action)
    for value in (True, 0, -1, "1", None):
        _error("bbs_invalid_arguments", lambda value=value: service.update_post(post["post_id"], "x", "y", expected_post_revision=value))
    _error("bbs_invalid_arguments", lambda: service.archive_post(post["post_id"], cast(Any, "true"), expected_post_revision=1))
    _error("bbs_invalid_arguments", lambda: ProjectBbsService(project, actor=cast(Any, "admin")))


def test_history_bound_integrity_and_malformed_envelopes(project, monkeypatch):
    service = ProjectBbsService(project)
    post = _post(service)
    saved = project.snapshot_saved_inputs()["documents"][post["post_id"]]
    for change in ({"author": "system"}, {"created_at": "not a date"}, {"version": True}, {"extra": True}):
        with pytest.raises(ProjectError):
            normalize_bbs_post(saved | change)
    for change in ({"revision": True}, {"editor": "user-supplied-name"}, {"archived": "false"},
                   {"edited_at": "2020-01-01T00:00:00+00:00"}):
        bad = deepcopy(saved)
        bad["history"][0].update(change)
        with pytest.raises(ProjectError):
            normalize_bbs_post(bad)
    assert MAX_HISTORY == 100
    monkeypatch.setattr("coat_sim.core.project_bbs.MAX_HISTORY", 1)
    _error("capacity_exceeded", lambda: service.update_post(post["post_id"], "new", "new", expected_post_revision=1))
    assert service.history(post["post_id"])["total"] == 1
    path = project.root / project.manifest["documents"][0]["path"]
    path.write_text(path.read_text().replace("工程日志", "篡改记录"))
    _error("integrity_error", lambda: service.read_post(post["post_id"]))


def test_management_requires_explicit_fields_and_rejects_authorship_payload(project):
    manager = ProjectManagementService()
    base = _args(project, operation_id="one", expected_revision=0, post_type="journal", title="Title", body="Body")
    for field, value in (("actor", "user"), ("author", "user"), ("created_at", "2020"), ("history", [])):
        _error("invalid_arguments", lambda field=field, value=value: manager.execute("bbs_create", base | {field: value}, actor="assistant"))
    for field in ("title", "body", "post_type"):
        _error("bbs_invalid_arguments", lambda field=field: manager.execute("bbs_create", {k: v for k, v in base.items() if k != field}))
    for invalid_revision in (True, None, -1):
        _error("revision_required", lambda invalid_revision=invalid_revision: manager.execute("bbs_create", base | {"expected_revision": invalid_revision}))
    created = manager.execute("bbs_create", base, actor="assistant")
    assert created["post"]["author"] == "assistant"
    assert manager.execute("bbs_read", _args(project, post_id=created["post"]["post_id"]))["post"] == created["post"]


def test_discussion_comment_is_narrow_append_only_exception(project):
    owner = ProjectBbsService(project)
    topic = _post(owner, "research_report")
    discussion = ProjectBbsService(project, actor="assistant", read_only=True, permit_comment=True)
    comment = discussion.comment("评论", "一个需要验证的想法")
    assert comment["post_type"] == "comment" and comment["author"] == "assistant"
    reply = discussion.comment("补充", "仍需实测", parent_id=topic["post_id"], expected_post_revision=1)
    assert reply["parent_id"] == topic["post_id"] and reply["post_type"] == "research_report"
    assert owner.read_post(topic["post_id"])["body"] == topic["body"]
    for action in (lambda: discussion.create_post("work_report", "禁止", "不可"),
                   lambda: discussion.update_post(comment["post_id"], "禁止", "不可", expected_post_revision=1),
                   lambda: discussion.archive_post(comment["post_id"], True, expected_post_revision=1)):
        _error("discussion_read_only", action)
    _error("bbs_invalid_arguments", lambda: discussion.comment("评论", "正文", expected_post_revision=1))
    _error("bbs_invalid_arguments", lambda: discussion.comment("评论", "正文", parent_id=topic["post_id"]))
    _error("discussion_read_only", lambda: ProjectBbsService(project, read_only=True).comment("禁止", "不可"))


def test_raw_data_tools_cannot_rewrite_remove_or_rename_managed_bbs(tmp_path, monkeypatch):
    from coat_sim.core import data_files
    monkeypatch.setattr(data_files, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(data_files, "DATA_ROOT", tmp_path / "data")
    store = ProjectStore.create(tmp_path / "data/projects/one", "Protected")
    post = _post(ProjectBbsService(store))
    post_path = store.root / store.manifest["documents"][0]["path"]
    before = store.snapshot_saved_inputs()
    attempts = [lambda: data_files.write_data_file(str(post_path), "{}", overwrite=True),
                lambda: data_files.write_data_file(str(store.project_file), "{}", overwrite=True),
                lambda: data_files.write_data_file(str(store.root / "untyped.json"), "{}"),
                lambda: data_files.delete_data_path(str(post_path)),
                lambda: data_files.delete_data_path(str(store.root), recursive=True),
                lambda: data_files.delete_data_path(str(store.root.parent), recursive=True),
                lambda: data_files.move_data_path(str(post_path), str(tmp_path / "data/moved.txt")),
                lambda: data_files.move_data_path(str(store.root.parent), str(tmp_path / "data/renamed")),
                lambda: data_files.make_data_directory(str(store.root / "injected")),
                lambda: data_files.write_data_file(str(tmp_path / "data/spoof.cbbs"), "{}")]
    for attempt in attempts:
        with pytest.raises(ValueError, match="Managed project"):
            attempt()
    assert store.snapshot_saved_inputs() == before
    assert ProjectBbsService(store).read_post(post["post_id"]) == post
    result = data_files.write_data_file("unrelated.txt", "Ordinary user data")
    assert Path(result["path"]).read_text() == "Ordinary user data"
    store.project_file.write_text("corrupt descriptor")
    with pytest.raises(ValueError, match="Managed project"):
        data_files.write_data_file(str(store.root / "injected.json"), "{}")
