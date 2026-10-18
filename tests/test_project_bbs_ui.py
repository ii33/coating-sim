"""Real offscreen BBS publication, ownership fencing and retained-input checks."""
from __future__ import annotations

import time
from threading import Event
from unittest.mock import patch

import pytest
from PySide6.QtCore import QObject, QCoreApplication, QPointF
from PySide6.QtTest import QTest

from coat_sim.core.project_bbs import ProjectBbsService
from tests.test_project_runtime_information import information_owner as _information_owner
from tests.test_project_runtime_management import (
    _eval, _find_item, _click,
)

information_owner = _information_owner
pytestmark = [pytest.mark.qt, pytest.mark.contract]


@pytest.fixture
def bbs_qml(information_owner):
    from coat_sim.core.project_film_session import ProjectFilmWorkspace
    owner, _stores, sessions = information_owner
    workspace = ProjectFilmWorkspace.for_project(owner.host, sessions[0].binding)
    owner.desktop.open_project_module(workspace, "project_bbs")
    view = next(view for route, view in owner.desktop._views.items() if route.module_key == "project_bbs")
    window, _controller, engine, _context, _component = view
    window.resize(800, 620)
    QTest.qWait(40)
    _wait(lambda: not owner._bbs.busy)
    try:
        yield engine, window
    finally:
        from shiboken6 import isValid
        if isValid(window) and isValid(engine):
            _eval(engine, window, "dismissDialogs()")
        QTest.qWait(120)
        QCoreApplication.processEvents()


def _wait(predicate, seconds=6):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        QCoreApplication.processEvents()
        QTest.qWait(10)
    assert predicate()


def _bbs_click(window, root, name):
    item = _find_item(root, name)
    scroll = root.findChild(QObject, "projectBbsScroll")
    flickable = scroll.property("contentItem")
    point = item.mapToItem(flickable.property("contentItem"), QPointF(0, 0))
    flickable.setProperty("contentY", max(0, min(point.y() - 20,
        flickable.property("contentHeight") - flickable.property("height"))))
    QCoreApplication.processEvents()
    _click(window, item)


def _request(owner, binding, operation, payload):
    results = []
    def finished(*args):
        results.append(args)
    owner._bbs.completed.connect(finished)
    try:
        token = owner._bbs.request(binding.project_id, binding.generation, operation, payload)
        assert token, owner._bbs.error
        _wait(lambda: bool(results))
        assert results[-1][0] == token
        return results[-1][3:]
    finally:
        owner._bbs.completed.disconnect(finished)


def test_coordinator_crud_attribution_stale_revision_and_other_project(information_owner):
    owner, stores, sessions = information_owner
    binding = sessions[0].binding
    result, error = _request(owner, binding, "create", {"post_type": "journal", "title": "工程日志", "body": "正文 <script>原样保存</script>", "actor": "assistant"})
    assert not error and result["author"] == "user"
    post_id = result["post_id"]
    assert ProjectBbsService(stores[1]).list_posts()["posts"] == []
    reply, error = _request(owner, binding, "reply", {"post_id": post_id, "expected_post_revision": 1, "title": "回复", "body": "讨论内容"})
    assert not error and reply["parent_id"] == post_id
    updated, error = _request(owner, binding, "update", {"post_id": post_id, "expected_post_revision": 1, "title": "新标题", "body": "新正文"})
    assert not error and updated["revision"] == 2
    _, error = _request(owner, binding, "update", {"post_id": post_id, "expected_post_revision": 1, "title": "失效覆盖", "body": "不得覆盖"})
    assert error and ProjectBbsService(stores[0]).read_post(post_id)["title"] == "新标题"
    assert not owner._bbs.request(binding.project_id, "old-generation", "list", {})
    history, error = _request(owner, binding, "history", {"post_id": post_id})
    assert not error and len(history["revisions"]) == 2
    archived, error = _request(owner, binding, "archive", {"post_id": post_id, "expected_post_revision": 2, "archived": True})
    assert not error and archived["archived"]


def test_coordinator_io_is_async_and_duplicate_submit_is_rejected(information_owner):
    owner, _stores, sessions = information_owner
    binding = sessions[0].binding
    started, release = Event(), Event()
    original = ProjectBbsService.list_posts
    def delayed(service, **kwargs):
        started.set()
        assert release.wait(5)
        return original(service, **kwargs)
    with patch.object(ProjectBbsService, "list_posts", delayed):
        before = time.monotonic()
        token = owner._bbs.request(binding.project_id, binding.generation, "list", {})
        assert token and time.monotonic() - before < 0.2
        assert started.wait(2)
        try:
            assert not owner._bbs.request(binding.project_id, binding.generation, "list", {})
            # GUI timer processing is independent of the intentionally stalled IO.
            QCoreApplication.processEvents()
            assert owner._bbs.busy
        finally:
            release.set()
        _wait(lambda: not owner._bbs.busy)


def test_unsaved_bbs_close_review_and_signature_fence(information_owner):
    owner, _stores, sessions = information_owner
    binding = sessions[0].binding
    assert owner._bbs.stageDraft(binding.project_id, binding.generation, {"mode": "create", "title": "未发布", "body": "保留文字"})
    assert owner.project_closer._preflight(sessions[0])[0]
    assert not owner.closeProject(binding.project_id)
    assert owner._bbs.pending_draft(binding)["body"] == "保留文字"
    assert owner._bbs.stageDraft(binding.project_id, binding.generation, {"mode": "create", "title": "未发布", "body": "确认期间新文字"})
    assert not owner.confirmCloseProject(binding.project_id)
    assert owner._bbs.pending_draft(binding)["body"] == "确认期间新文字"
    owner.cancelCloseProject(binding.project_id)
    assert not owner._bbs.stageDraft(binding.project_id, "other-session", {"body": "错误目标"})
    assert owner._bbs.pending_draft(binding)["body"] == "确认期间新文字"


def test_real_qml_publish_edit_history_reply_and_session_retention(bbs_qml, information_owner):
    engine, window = bbs_qml
    root = window
    owner, stores, sessions = information_owner
    binding, other = sessions[0].binding, sessions[1].binding
    root.setProperty("projectBridge", owner)
    page = _find_item(root, "projectBbsPage")
    try:
        _eval(engine, page, f"openFor('{binding.project_id}', '{binding.generation}')")
        _wait(lambda: not owner._bbs.busy)
        _bbs_click(window, root, "projectBbsNew")
        title = _find_item(root, "projectBbsTitle")
        body = _find_item(root, "projectBbsBody")
        title.setProperty("text", "Qt 发布日志")
        body.setProperty("text", "纯文本 <b>不是 HTML</b>")
        assert page.property("dirty")
        assert body.property("text") == "纯文本 <b>不是 HTML</b>"
        _eval(engine, page, f"openFor('{other.project_id}', '{other.generation}')")
        assert body.property("text") == ""
        _eval(engine, page, f"openFor('{binding.project_id}', '{binding.generation}')")
        assert body.property("text") == "纯文本 <b>不是 HTML</b>"
        _eval(engine, page, "dismissDialogs()")
        _eval(engine, page, f"openFor('{binding.project_id}', '{binding.generation}')")
        assert body.property("text") == "纯文本 <b>不是 HTML</b>"
        _bbs_click(window, root, "projectBbsPublish")
        _wait(lambda: not owner._bbs.busy)
        posts = ProjectBbsService(stores[0]).list_posts()["posts"]
        assert len(posts) == 1 and posts[0]["author"] == "user"
        assert not page.property("dirty")
        assert not owner.pending_project_bbs(binding)
        view = _find_item(root, "projectBbsBodyView")
        assert "&lt;b&gt;" in view.property("text")
        page.setProperty("richPreview", False)
        assert view.property("text") == "纯文本 <b>不是 HTML</b>"
        assert _eval(engine, view, "Number(textFormat) === 0")
        _bbs_click(window, root, "projectBbsReadingFocus")
        assert page.property("readingFocus")
        assert not _find_item(root, "projectBbsList").isVisible()
        _bbs_click(window, root, "projectBbsReadingFocus")
        assert not page.property("readingFocus")
        _bbs_click(window, root, "projectBbsEdit")
        title.setProperty("text", "Qt 修改标题")
        body.setProperty("text", "新正文")
        _bbs_click(window, root, "projectBbsPublish")
        _wait(lambda: not owner._bbs.busy)
        _bbs_click(window, root, "projectBbsHistory")
        _wait(lambda: not owner._bbs.busy)
        assert page.property("historyVisible")
        assert page.property("historyTotal") == 2
        _bbs_click(window, root, "projectBbsReply")
        title.setProperty("text", "Qt 回复")
        body.setProperty("text", "回复内容")
        _bbs_click(window, root, "projectBbsPublish")
        _wait(lambda: not owner._bbs.busy)
        replies = ProjectBbsService(stores[0]).list_posts(parent_id=posts[0]["post_id"])["posts"]
        assert len(replies) == 1 and replies[0]["author"] == "user"
    finally:
        _eval(engine, root, "dismissDialogs()")
        QTest.qWait(120)
        QCoreApplication.processEvents()


def test_real_qml_failed_save_preserves_exact_text(bbs_qml, information_owner):
    engine, window = bbs_qml
    root = window
    owner, stores, sessions = information_owner
    binding = sessions[0].binding
    post = ProjectBbsService(stores[0]).create_post("research_report", "研究记录", "原正文")
    root.setProperty("projectBridge", owner)
    page = _find_item(root, "projectBbsPage")
    try:
        _eval(engine, page, f"openFor('{binding.project_id}', '{binding.generation}')")
        _wait(lambda: not owner._bbs.busy)
        assert _eval(engine, page, f"readPost('{post['post_id']}')")
        _wait(lambda: not owner._bbs.busy)
        assert _eval(engine, page, "beginEdit('update')")
        title, body = _find_item(root, "projectBbsTitle"), _find_item(root, "projectBbsBody")
        title.setProperty("text", "保留标题")
        body.setProperty("text", "保留正文")
        ProjectBbsService(stores[0], actor="assistant").update_post(post["post_id"], "助手新版本", "助手正文", expected_post_revision=1)
        assert _eval(engine, page, "publish()")
        _wait(lambda: not owner._bbs.busy)
        assert page.property("errorMessage")
        assert title.property("text") == "保留标题" and body.property("text") == "保留正文"
        assert page.property("dirty")
        _bbs_click(window, root, "projectBbsDiscard")
        dialog = root.findChild(QObject, "projectBbsDiscardDialog")
        assert dialog is not None and dialog.property("visible")
        _eval(engine, dialog, "reject()")
        assert body.property("text") == "保留正文"
    finally:
        _eval(engine, root, "dismissDialogs()")
        QTest.qWait(120)
        QCoreApplication.processEvents()


def test_oversized_unsaved_text_survives_project_switch(bbs_qml, information_owner):
    engine, window = bbs_qml
    root = window
    owner, _stores, sessions = information_owner
    binding, other = sessions[0].binding, sessions[1].binding
    root.setProperty("projectBridge", owner)
    page = _find_item(root, "projectBbsPage")
    try:
        _eval(engine, page, f"openFor('{binding.project_id}', '{binding.generation}')")
        _eval(engine, page, "beginEdit('create')")
        _find_item(root, "projectBbsTitle").setProperty("text", "保留超限输入")
        body = _find_item(root, "projectBbsBody")
        original = "字" * 65537
        body.setProperty("text", original)
        _eval(engine, page, f"openFor('{other.project_id}', '{other.generation}')")
        _eval(engine, page, f"openFor('{binding.project_id}', '{binding.generation}')")
        assert body.property("text") == original
        assert _eval(engine, page, "publish()")
        _wait(lambda: not owner._bbs.busy)
        assert page.property("errorMessage") and body.property("text") == original
    finally:
        _eval(engine, root, "dismissDialogs()")
        QTest.qWait(120)
        QCoreApplication.processEvents()


def test_real_qml_markdown_image_attachment_preview_publish_and_reopen(bbs_qml, information_owner, tmp_path):
    from PySide6.QtGui import QImage, QColor
    engine, window = bbs_qml
    root = window
    owner, stores, sessions = information_owner
    binding = sessions[0].binding
    image_path = tmp_path / "sample.png"
    image = QImage(12, 12, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    assert image.save(str(image_path))
    root.setProperty("projectBridge", owner)
    page = _find_item(root, "projectBbsPage")
    try:
        _eval(engine, page, f"openFor('{binding.project_id}', '{binding.generation}')")
        _wait(lambda: not owner._bbs.busy)
        _eval(engine, page, "beginEdit('create')")
        _find_item(root, "projectBbsTitle").setProperty("text", "图文研究报告")
        _find_item(root, "projectBbsBody").setProperty("text", "# 研究目标\n**重点**\n<script>不执行</script>")
        assert _eval(engine, page, f"importAttachment('{image_path.as_uri()}')")
        _wait(lambda: not owner._bbs.busy)
        assert _eval(engine, page, "editAttachments.length") == 1
        assert _eval(engine, page, "previewAttachment(editAttachments[0], 0)")
        _wait(lambda: not owner._bbs.busy)
        image_view = _find_item(root, "projectBbsAttachmentImage")
        _wait(lambda: _eval(engine, image_view, "Number(status) === 1"))
        assert image_view.property("source").toString().startswith("image://projectbbs/")
        _eval(engine, page, "dismissDialogs()")
        page.setProperty("editorPreview", True)
        preview = _find_item(root, "projectBbsEditorPreviewView")
        assert "研究目标" in preview.property("text") and "&lt;script&gt;" in preview.property("text")
        assert _eval(engine, page, "publish()")
        _wait(lambda: not owner._bbs.busy)
        post = ProjectBbsService(stores[0]).read_post(ProjectBbsService(stores[0]).list_posts()["posts"][0]["post_id"])
        assert post["body_format"] == "markdown" and len(post["attachments"]) == 1
        assert post["attachments"][0]["name"] == "sample.png"
        assert post["author"] == "user"
    finally:
        _eval(engine, root, "dismissDialogs()")
        QTest.qWait(120)
        QCoreApplication.processEvents()


def test_attachment_picker_results_cannot_cross_project_switch(bbs_qml, information_owner, tmp_path):
    engine, window = bbs_qml
    root = window
    owner, stores, sessions = information_owner
    binding, other = sessions[0].binding, sessions[1].binding
    source = tmp_path / "note.txt"
    source.write_text("private chosen file", encoding="utf-8")
    root.setProperty("projectBridge", owner)
    page = _find_item(root, "projectBbsPage")
    try:
        _eval(engine, page, f"openFor('{binding.project_id}', '{binding.generation}')")
        _eval(engine, page, "beginEdit('create')")
        _eval(engine, page, "attachmentImportTarget = {projectId: projectId, generation: generation, mode: mode, postId: editPostId, revision: editRevision}")
        _eval(engine, page, f"openFor('{other.project_id}', '{other.generation}')")
        _eval(engine, page, "beginEdit('create')")
        assert not _eval(engine, page, f"acceptAttachmentImport('{source.as_uri()}')")
        assert not owner._bbs.busy
        assert all(not store.manifest["documents"] for store in stores)
    finally:
        _eval(engine, root, "dismissDialogs()")
        QTest.qWait(120)
        QCoreApplication.processEvents()


def test_standalone_route_reuse_two_projects_and_read_only_snapshot(bbs_qml, information_owner):
    from types import SimpleNamespace
    from PySide6.QtCore import Qt
    from coat_sim.core.project_film_session import ProjectFilmWorkspace
    from coat_sim.control_errors import UiControlError
    from coat_sim.ui.project_application_client import ProjectApplicationClient
    from tests.test_project_bbs_async_ipc import Bridge

    engine, window = bbs_qml
    owner, stores, sessions = information_owner
    workspace = ProjectFilmWorkspace.for_project(owner.host, sessions[0].binding)
    other = ProjectFilmWorkspace.for_project(owner.host, sessions[1].binding)
    first_id = owner.desktop.open_project_module(workspace, "project_bbs")
    window.showMinimized()
    QTest.qWait(20)
    assert owner.desktop.open_project_module(workspace, "project_bbs") == first_id
    assert not window.windowState() & Qt.WindowState.WindowMinimized
    assert len(owner.desktop._views) == 1
    assert window.property("title") == "工程 BBS — 工程甲"
    selector = window.findChild(QObject, "projectModuleSelectorRoot")
    assert _eval(engine, selector, "toolModules().some(function(row) { return row.key === 'project_bbs' })")
    owner.desktop.open_project_module(other, "project_bbs")
    QTest.qWait(30)
    _wait(lambda: not owner._bbs.busy)
    assert len(owner.desktop._views) == 2
    page = _find_item(window, "projectBbsPage")
    _eval(engine, page, "beginEdit('create')")
    _find_item(window, "projectBbsTitle").setProperty("text", "只属于甲")
    _find_item(window, "projectBbsBody").setProperty("text", "未发布的详细内容")
    assert owner.pending_project_bbs(sessions[0].binding)
    assert not owner.pending_project_bbs(sessions[1].binding)

    bridge = Bridge()
    bridge.project_runtime = owner
    bridge.project_lifecycle = None
    client = ProjectApplicationClient(bridge, SimpleNamespace(dispatch=lambda *_: {"windows": []}), role="project_host")
    args = {"project_id": sessions[0].binding.project_id, "session_generation": sessions[0].binding.generation,
            "client_generation": client.generation, "module_key": "project_bbs",
            "project_root": str(stores[0].root), "project_file": str(stores[0].project_file)}
    try:
        client.admission.registration_succeeded()
        rows = client.dispatch("list_windows", {})["windows"]
        assert len(rows) == 2 and {row["project_id"] for row in rows} == {s.binding.project_id for s in sessions}
        assert client.dispatch("open_window", args)["instance_id"] == first_id
        state = client.dispatch("project_editor_snapshot", args)
        assert state["available"] and state["summary"]["unsaved"]
        assert state["summary"]["editor_title"] == "只属于甲"
        assert state["summary"]["editor_body_length"] == len("未发布的详细内容")
        assert "未发布的详细内容" not in str(state)
        _find_item(window, "projectBbsBody").setProperty("text", "相同长度的新内容")
        with pytest.raises(UiControlError, match="重新读取"):
            client.dispatch("project_editor_snapshot", args | {"expected_snapshot_id": state["snapshot_id"]})
        assert not ProjectBbsService(stores[0]).list_posts()["posts"]
        assert not ProjectBbsService(stores[1]).list_posts()["posts"]
    finally:
        client.stop()
        client.deleteLater()
        bridge.deleteLater()


def test_standalone_nonlast_close_retains_then_reopens_and_last_close_reviews(bbs_qml, information_owner):
    from coat_sim.core.project_film_session import ProjectFilmWorkspace
    engine, window = bbs_qml
    owner, _stores, sessions = information_owner
    binding = sessions[0].binding
    workspace = ProjectFilmWorkspace.for_project(owner.host, binding)
    owner.desktop.open_project_module(workspace, "film_design")
    page = _find_item(window, "projectBbsPage")
    _eval(engine, page, "beginEdit('create')")
    _find_item(window, "projectBbsTitle").setProperty("text", "留在工程会话")
    _find_item(window, "projectBbsBody").setProperty("text", "关闭后继续")
    assert not window.close()
    dialog = window.findChild(QObject, "projectBbsKeepCloseDialog")
    assert dialog.property("visible")
    _eval(engine, dialog, "close()")
    assert window.isVisible()
    assert not window.close()
    _eval(engine, window, "keepAndClose()")
    _wait(lambda: all(route.module_key != "project_bbs" for route in owner.desktop._views))
    assert owner.pending_project_bbs(binding)["body"] == "关闭后继续"
    owner.desktop.open_project_module(workspace, "project_bbs")
    new_window, _controller, new_engine, *_ = next(view for route, view in owner.desktop._views.items() if route.module_key == "project_bbs")
    assert _find_item(new_window, "projectBbsBody").property("text") == "关闭后继续"
    film = next(view[0] for route, view in owner.desktop._views.items() if route.module_key == "film_design")
    film.setProperty("allowClose", True)
    assert film.close()
    _wait(lambda: len(owner.desktop._views) == 1)
    reviews: list[str] = []
    owner.projectCloseConfirmationRequested.connect(reviews.append)
    assert not new_window.close()
    _wait(lambda: bool(reviews))
    assert reviews == [binding.project_id] and new_window.isVisible()
    owner.cancelCloseProject(binding.project_id)
    assert owner.pending_project_bbs(binding)
    assert not new_window.close()
    _wait(lambda: len(reviews) == 2)
    owner.returnToSaveProject(binding.project_id)
    assert new_window.isVisible() and owner.pending_project_bbs(binding)
    # Leave cleanup to the owner; the original fixture's window has closed.
    _eval(new_engine, new_window, "dismissDialogs()")
