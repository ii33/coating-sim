"""Project BBS transport identity, attribution and bounded wire contracts."""
from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.assistant_process import AssistantProcessClient
from coat_sim.core.api import CoatService
from coat_sim.core.project_store import ProjectStore
from coat_sim.mcp import server
from coat_sim.project_cli import main

pytestmark = pytest.mark.contract


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "service", CoatService(tmp_path))
    monkeypatch.setattr(AssistantProcessClient, "list_clients", lambda _: [])
    return ProjectStore.create(tmp_path / "project", "BBS")


def _target(project):
    return {"root": str(project.project_file), "project_id": project.manifest["id"]}


def _payload(result):
    if isinstance(result, tuple):
        return result[1]
    if isinstance(result, dict):
        return result
    value = getattr(result, "structured_content", None)
    return value if value is not None else json.loads(result.content[0].text)


def test_mcp_real_call_attributes_assistant_and_exposes_mutation_effect(project):
    created = _payload(asyncio.run(server.mcp.call_tool("project_bbs_write", {
        **_target(project), "operation": "create", "operation_id": "create-one", "expected_revision": 0,
        "parameters": {"post_type": "work_report", "title": "Work", "body": "Actual completed work"}})))
    post = created["post"]
    assert post["author"] == "assistant"
    assert server.project_bbs_read(**_target(project), post_id=post["post_id"])["post"] == post
    assert server.project_bbs_list(**_target(project))["posts"][0]["post_id"] == post["post_id"]
    assert server.project_bbs_history(**_target(project), post_id=post["post_id"])["total"] == 1
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    for name in ("project_bbs_list", "project_bbs_read", "project_bbs_history"):
        assert tools[name].annotations.read_only_hint is True
    for name in ("project_bbs_write", "project_bbs_comment"):
        assert tools[name].annotations.read_only_hint is False


def test_mcp_comment_cannot_select_a_formal_post_type_or_edit(project):
    comment = _payload(asyncio.run(server.mcp.call_tool("project_bbs_comment", {
        **_target(project), "operation_id": "comment", "expected_revision": 0,
        "title": "评论", "body": "讨论内容"})))
    assert comment["post"]["post_type"] == "comment" and comment["post"]["author"] == "assistant"
    reply = server.project_bbs_comment(**_target(project), operation_id="reply", expected_revision=1,
        title="回复", body="补充", parent_id=comment["post"]["post_id"], expected_post_revision=1)
    assert reply["post"]["parent_id"] == comment["post"]["post_id"]
    for extra in ({"post_type": "work_report"}, {"operation": "update"}, {"author": "user"}, {"archived": True}):
        with pytest.raises(ToolError):
            asyncio.run(server.mcp.call_tool("project_bbs_comment", {
                **_target(project), "operation_id": "inject", "expected_revision": 2,
                "title": "no", "body": "no", **extra}))


def test_mcp_rejects_authorship_revision_coercion_and_unbounded_inputs(project):
    base = {**_target(project), "operation": "create", "operation_id": "one", "expected_revision": 0,
            "parameters": {"post_type": "journal", "title": "Title", "body": "Body"}}
    with pytest.raises(ToolError):
        asyncio.run(server.mcp.call_tool("project_bbs_write", base | {"expected_revision": True}))
    invalid_fields: tuple[dict[str, Any], ...] = ({"author": "user"}, {"actor": "user"}, {"created_at": "forged"}, {"history": []})
    for extra in invalid_fields:
        with pytest.raises(ToolError, match="invalid_arguments"):
            server.project_bbs_write(**(base | {"parameters": base["parameters"] | extra}))
    with pytest.raises(ToolError, match="bbs_invalid_arguments"):
        server.project_bbs_list(**_target(project), limit=51)
    with pytest.raises(ToolError):
        asyncio.run(server.mcp.call_tool("project_bbs_list", _target(project) | {"include_archived": "false"}))
    with pytest.raises(ToolError, match="project_identity_mismatch"):
        server.project_bbs_list(**(_target(project) | {"project_id": "wrong"}))
    assert ProjectStore.open(project.project_file).manifest["revision"] == 0


def test_mcp_active_owner_routes_existing_project_manage_without_actor_payload(project, monkeypatch):
    calls = []

    def request(_, operation, **kwargs):
        calls.append((operation, kwargs))
        return {"routed": True}

    monkeypatch.setattr(AssistantProcessClient, "request", request)
    result = server.project_bbs_write("create", **_target(project), operation_id="owned", expected_revision=0,
        parameters={"post_type": "journal", "title": "Title", "body": "Body"}, session_generation="verified-generation")
    assert result == {"routed": True}
    assert calls[0][0] == "project_manage"
    assert calls[0][1]["project_operation"] == "bbs_create"
    assert calls[0][1]["session_generation"] == "verified-generation"
    assert not {"actor", "author"} & calls[0][1]["arguments"].keys()


def test_cli_manage_bbs_is_user_attributed_and_preserves_errors(project, tmp_path):
    args = tmp_path / "request.json"
    args.write_text(json.dumps({**_target(project), "operation_id": "cli-create", "expected_revision": 0,
        "post_type": "journal", "title": "User log", "body": "Saved through CLI"}))
    stdout, stderr = io.StringIO(), io.StringIO()
    assert main(["manage", "bbs_create", str(args)], stdout=stdout, stderr=stderr) == 0
    value = json.loads(stdout.getvalue())
    assert value["post"]["author"] == "user"
    assert stderr.getvalue() == ""
    output, error = io.StringIO(), io.StringIO()
    assert main(["manage", "bbs_create", str(args)], stdout=output, stderr=error) == 2
    assert json.loads(error.getvalue())["error"]["code"] == "stale_project"


def test_bbs_core_cli_mcp_import_and_write_do_not_load_qt(tmp_path):
    source = Path(__file__).resolve().parents[1]
    code = '''import importlib.abc, json, sys
class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("PySide6"):
            raise AssertionError("BBS must remain headless")
sys.meta_path.insert(0, NoQt())
from coat_sim.core.project_store import ProjectStore
from coat_sim.core.project_bbs import ProjectBbsService
from coat_sim.mcp import server
from coat_sim.core.api import CoatService
from coat_sim.assistant_process import AssistantProcessClient
server.service = CoatService(sys.argv[1])
AssistantProcessClient.list_clients = lambda self: []
s = ProjectStore.create(sys.argv[1] + "/p", "Headless")
p = server.project_bbs_comment(str(s.project_file), s.manifest["id"], "comment", 0, "Title", "Body")
assert p["post"]["author"] == "assistant"
print(json.dumps({"ok": True, "qt_loaded": any(name.startswith("PySide6") for name in sys.modules)}))
'''
    completed = subprocess.run([sys.executable, "-c", code, str(tmp_path)], cwd=source,
        env={**os.environ, "PYTHONPATH": str(source)}, capture_output=True, text=True, timeout=20, check=False)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"ok": True, "qt_loaded": False}


def test_mcp_rich_comment_references_managed_asset_without_implicit_file_import(project):
    source = project.root / "source.txt"
    source.write_text("Reference evidence", encoding="utf-8")
    imported = _payload(asyncio.run(server.mcp.call_tool("project_bbs_import_attachment", {
        **_target(project), "operation_id": "import-evidence", "expected_revision": 0,
        "file": str(source)})))
    descriptor = imported["attachment"]
    assert imported["revision"] == 1 and "post" not in imported
    source.unlink()
    comment = _payload(asyncio.run(server.mcp.call_tool("project_bbs_comment", {
        **_target(project), "operation_id": "cite-evidence", "expected_revision": 1,
        "title": "Evidence", "body": "**Reference**", "body_format": "markdown", "attachments": [descriptor]})))
    assert comment["post"]["body_format"] == "markdown"
    assert comment["post"]["attachments"] == [descriptor]
    with pytest.raises(ToolError):
        asyncio.run(server.mcp.call_tool("project_bbs_comment", {
            **_target(project), "operation_id": "cannot-import", "expected_revision": 2,
            "title": "No", "body": "No", "file": str(source)}))
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert tools["project_bbs_import_attachment"].annotations.read_only_hint is False
