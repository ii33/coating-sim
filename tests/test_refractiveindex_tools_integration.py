"""Offline CLI construction and real MCP reads share one immutable source owner."""
import hashlib
from io import StringIO
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from coat_sim.mcp import server
from coat_sim.refractiveindex_cli import main

pytestmark = pytest.mark.contract


def cli(arguments):
    stdout = StringIO()
    code = main(arguments, stdout=stdout)
    result = json.loads(stdout.getvalue())
    assert code == 0, result
    return result["result"]


@pytest.fixture
def sources(tmp_path):
    source, store = tmp_path / "downloaded-source", tmp_path / "reference-store"
    source.mkdir()
    (source / "LICENSE").write_text("CC0 1.0 Universal\n", encoding="utf-8")
    (source / "database/data/main/fixture/nk").mkdir(parents=True)
    (source / "database/data/main/fixture/n2").mkdir(parents=True)

    def write(relative, content):
        (source / relative).write_text(json.dumps(content), encoding="utf-8")

    def catalog(kind, pages):
        write(f"database/catalog-{kind}.yml", [{"SHELF": "main", "name": "Main", "content": [
            {"BOOK": "fixture", "name": "Fixture", "content": [
                {"PAGE": page, "name": page, "data": f"main/fixture/{kind}/{page}.yml"}
                for page in pages]}]}])

    catalog("nk", ["n-only", "k-only"])
    catalog("n2", ["nonlinear"])
    for relative, kind, data in [
        ("nk/n-only", "tabulated n", "0.4 1.5\n0.5 1.6\n0.7 1.7\n"),
        ("nk/k-only", "tabulated k", "0.4 0.01\n0.7 0.02\n"),
        ("n2/nonlinear", "tabulated n2", "0.4 1e-20\n0.7 2e-20\n"),
    ]:
        write(f"database/data/main/fixture/{relative}.yml", {
            "REFERENCES": "Synthetic offline adapter fixture", "CONDITIONS": {"temperature": 300},
            "COMMENTS": "Reference text only", "DATA": [{"type": kind, "data": data}]})
    return source, store


def tree_content(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def test_explicit_build_no_activate_then_real_read_only_mcp_workflow(sources, monkeypatch):
    source, store = sources
    args = ["--store", str(store), "build", "--source-dir", str(source), "--upstream-commit", "a" * 40]
    built = cli([*args, "--no-activate"])
    snapshot = built["snapshot_id"]
    assert built["activated"] is False
    assert not (store / "CURRENT.json").exists()
    assert cli(["--store", str(store), "status"])["status"] == "missing"
    assert cli(["--store", str(store), "--snapshot-id", snapshot, "status"])["status"] == "ready"
    assert cli(args)["activated"] is True

    monkeypatch.setenv("COAT_SIM_REFRACTIVEINDEX_STORE", str(store))
    before = tree_content(store)
    status = server.refractiveindex_status()
    assert status["snapshot_id"] == snapshot
    assert status["counts"]["indexed_entries"] == 3
    first = server.refractiveindex_search(shelf="main", book="fixture", limit=1, snapshot_id=snapshot)
    second = server.refractiveindex_search(shelf="main", book="fixture", limit=1,
                                          offset=1, snapshot_id=snapshot)
    assert first["total"] == 2 and first["has_more"] is True
    assert second["has_more"] is False
    assert first["entries"][0]["entry_id"] != second["entries"][0]["entry_id"]
    n_only = server.refractiveindex_search(query="n-only", capability="supported", snapshot_id=snapshot)["entries"][0]
    identity = n_only["entry_id"]
    detail = server.refractiveindex_detail(identity, raw_limit=8, snapshot_id=snapshot)
    assert detail["raw_yaml"]["truncated"] is True
    assert len(detail["raw_yaml"]["text"]) == 8
    assert detail["conditions"] == {"temperature": 300}
    preview = server.refractiveindex_preview(identity, max_points=2, snapshot_id=snapshot)
    assert preview["blocks"][0]["source_point_count"] == 3
    assert preview["blocks"][0]["display_point_count"] == 2
    candidate = server.refractiveindex_convert(identity, "chosen-source", snapshot_id=snapshot)
    assert candidate["optical_set"]["k"] is None
    assert len(candidate["optical_set"]["wavelengths"]) == 3
    assert candidate["provenance"]["snapshot_id"] == snapshot
    assert candidate["missing"] and candidate["assumptions"] == []
    nonlinear = server.refractiveindex_search(catalog="n2", snapshot_id=snapshot)["entries"][0]
    nonlinear_preview = server.refractiveindex_preview(nonlinear["entry_id"], snapshot_id=snapshot)
    assert nonlinear_preview["blocks"][0]["quantity"] == "n2"
    with pytest.raises(ToolError):
        server.refractiveindex_convert(nonlinear["entry_id"], "not-k", snapshot_id=snapshot)
    assert tree_content(store) == before
    assert not list(store.rglob("*-journal")) and not list(store.rglob("*-wal"))


def test_mcp_reads_remain_pinned_after_cli_activates_new_source(sources, monkeypatch):
    source, store = sources
    args = ["--store", str(store), "build", "--source-dir", str(source), "--upstream-commit", "a" * 40]
    original = cli(args)["snapshot_id"]
    monkeypatch.setenv("COAT_SIM_REFRACTIVEINDEX_STORE", str(store))
    row = server.refractiveindex_search(query="n-only", snapshot_id=original)["entries"][0]
    path = source / "database/data/main/fixture/nk/n-only.yml"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["DATA"][0]["data"] = "0.4 2.5\n0.5 2.6\n0.7 2.7\n"
    path.write_text(json.dumps(document), encoding="utf-8")
    updated = cli(args)["snapshot_id"]
    assert updated != original
    assert server.refractiveindex_status()["snapshot_id"] == updated
    old = server.refractiveindex_convert(row["entry_id"], "old-data", snapshot_id=original)
    new = server.refractiveindex_convert(row["entry_id"], "new-data", snapshot_id=updated)
    assert old["optical_set"]["n"] == [1.5, 1.6, 1.7]
    assert new["optical_set"]["n"] == [2.5, 2.6, 2.7]
