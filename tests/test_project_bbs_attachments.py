"""Owned attachment bytes, safe renderer capabilities and bounded PDF text pages."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QSize, QUrl
from PySide6.QtGui import QImage
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickItem

from coat_sim.core.project_bbs_attachments import (
    MAX_PREVIEW_TEXT_BYTES, export_attachment, import_attachment, normalize_attachment,
    preview_attachment, read_attachment,
)
from coat_sim.core.project_binary_assets import MAX_BBS_ATTACHMENT_BYTES
from coat_sim.core.project_store import ProjectError, ProjectStore
from coat_sim.ui.project_bbs_attachments import ProjectBbsAttachmentBridge, ProjectBbsImageProvider, _selected_file
from coat_sim.ui.theme_service import ThemeEngineBridge
from coat_sim.ui.theme_tokens import build_theme_snapshot

pytestmark = [pytest.mark.qt, pytest.mark.contract]


@pytest.fixture
def project(tmp_path):
    store = ProjectStore.create(tmp_path / "project", "Attachments")
    return store, SimpleNamespace(project_id=store.manifest["id"], generation="a" * 32)


def _attach(store, tmp_path, name, raw):
    source = tmp_path / name
    source.write_bytes(raw)
    return import_attachment(store, source)


def test_text_csv_is_literal_bounded_and_independent_of_source(project, tmp_path):
    store, _ = project
    raw = b'=HYPERLINK("https://invalid.example","literal")\n<script>alert(1)</script>\n' + b"x" * 70000
    attachment = _attach(store, tmp_path, "formulas.csv", raw)
    (tmp_path / "formulas.csv").unlink()
    result = preview_attachment(store, attachment)
    assert result["kind"] == "text" and result["truncated"]
    assert len(result["text"].encode()) <= MAX_PREVIEW_TEXT_BYTES
    assert result["text"].startswith("=HYPERLINK") and "<script>" in result["text"]
    target = tmp_path / "export.csv"
    export_attachment(store, attachment, target)
    assert target.read_bytes() == raw
    with pytest.raises(ProjectError, match="already exists"):
        export_attachment(store, attachment, target)
    with pytest.raises(ProjectError, match="managed project"):
        export_attachment(store, attachment, store.root / "bad.csv")


@pytest.mark.parametrize("name,raw", [("image.png", b"<svg/>"), ("paper.pdf", b"not a PDF"),
    ("code.py", b"\x00binary"), ("text.txt", b"\xff\xfe"), ("image.jpg", b"GIF89aabc")])
def test_extension_signature_and_utf8_mismatches_never_publish(project, tmp_path, name, raw):
    store, _ = project
    with pytest.raises(ProjectError):
        _attach(store, tmp_path, name, raw)
    assert store.manifest["revision"] == 0


def test_symlink_special_remote_and_oversized_files_are_rejected(project, tmp_path):
    store, _ = project
    source = tmp_path / "source.txt"
    source.write_text("source")
    link = tmp_path / "link.txt"
    link.symlink_to(source)
    for path in (link, tmp_path, "https://invalid.example/file.txt"):
        with pytest.raises(ProjectError):
            import_attachment(store, path)
    oversized = tmp_path / "large.bin"
    with oversized.open("wb") as stream:
        stream.truncate(MAX_BBS_ATTACHMENT_BYTES + 1)
    with pytest.raises(ProjectError, match="32 MiB"):
        import_attachment(store, oversized)
    assert store.manifest["revision"] == 0


@pytest.mark.parametrize("url", ["https://invalid.example/a.png", "file://server/share/a.txt",
    "file:///tmp/x?secret=true", "file:///tmp/x#fragment", "../file.txt", "data:image/png;base64,aA=="])
def test_picker_url_boundary_rejects_remote_and_nonlocal_sources(url):
    with pytest.raises(ProjectError):
        _selected_file(url)


def test_unknown_html_svg_archive_are_opaque_not_unpacked(project, tmp_path):
    store, binding = project
    helper = ProjectBbsAttachmentBridge()
    for name, raw in (("unsafe.html", b'<img src="https://invalid.example/x">'),
                      ("unsafe.svg", b'<svg onload="evil()"/>'),
                      ("bomb.zip", b"PK\x03\x04not-expanded")):
        attachment = _attach(store, tmp_path, name, raw)
        result = helper.execute(store, binding, "attachment_preview", {"attachment": attachment})
        assert result["kind"] == "unsupported" and result["image_source"] == ""
        assert "bytes" not in result and result["text"] == ""


def test_descriptor_cannot_select_a_path_or_forge_metadata(project, tmp_path):
    store, _ = project
    attachment = _attach(store, tmp_path, "notes.txt", b"data")
    for change in ({"asset_id": "../../etc/passwd"}, {"path": "/etc/passwd"},
                   {"name": "../escape"}, {"mime": "text/html"}):
        with pytest.raises(ProjectError):
            normalize_attachment(attachment | change)
    for metadata_change in ({"name": "image.png"}, {"mime": "image/png"}, {"size": 1}, {"sha256": "0" * 64}):
        with pytest.raises(ProjectError):
            read_attachment(store, attachment | metadata_change)
    document = store._read_all()[1][attachment["asset_id"]]
    (store.root / document["payload_path"]).write_bytes(b"evil")
    with pytest.raises(ProjectError, match="SHA-256"):
        preview_attachment(store, attachment)


def _png(tmp_path):
    image = QImage(200, 100, QImage.Format.Format_ARGB32)
    image.fill(0xFF334455)
    path = tmp_path / "image.png"
    assert image.save(str(path))
    return path


def test_image_capabilities_are_bound_revocable_and_never_paths(project, tmp_path):
    store, binding = project
    attachment = import_attachment(store, _png(tmp_path))
    helper = ProjectBbsAttachmentBridge()
    result = helper.execute(store, binding, "attachment_preview", {"attachment": attachment})
    assert "bytes" not in result and result["image_source"].startswith("image://projectbbs/")
    token = result["image_source"].rsplit("/", 1)[1]
    assert not helper.images.get(token).isNull()
    assert helper.images.get("../../image.png").isNull()
    helper.forget(binding)
    assert helper.images.get(token).isNull()
    with pytest.raises(ProjectError, match="closed"):
        helper.execute(store, binding, "attachment_preview", {"attachment": attachment})


def test_shutdown_cannot_resurrect_an_inflight_image(project, tmp_path, monkeypatch):
    store, binding = project
    attachment = import_attachment(store, _png(tmp_path))
    helper = ProjectBbsAttachmentBridge()
    from coat_sim.ui import project_bbs_attachments as module
    original = module._decode_image
    entered, release = Event(), Event()
    def paused(raw):
        entered.set()
        assert release.wait(5)
        return original(raw)
    monkeypatch.setattr(module, "_decode_image", paused)
    with ThreadPoolExecutor() as executor:
        future = executor.submit(helper.execute, store, binding, "attachment_preview", {"attachment": attachment})
        assert entered.wait(2)
        helper.clear()
        release.set()
        with pytest.raises(ProjectError, match="closed"):
            future.result(timeout=5)
    assert not helper.images.values


def _pdf(*, pages=1, encrypted=False):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    for number in range(pages):
        page = writer.add_blank_page(width=200, height=200)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 10 100 Td (Page {number + 1} verified text) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    if encrypted:
        writer.encrypt("secret")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_text_preview_uses_real_isolated_parser_and_pages(project, tmp_path):
    store, binding = project
    attachment = _attach(store, tmp_path, "pages.pdf", _pdf(pages=2))
    result = ProjectBbsAttachmentBridge().execute(store, binding, "attachment_preview", {"attachment": attachment, "page": 1})
    assert result["kind"] == "pdf_text" and result["page_count"] == 2 and result["page"] == 1
    assert "Page 2 verified text" in result["text"] and "不保留页面版式" in result["message"]
    assert "bytes" not in result and result["image_source"] == ""


@pytest.mark.parametrize("raw", [b"%PDF-truncated", _pdf(encrypted=True), _pdf(pages=201)])
def test_invalid_encrypted_or_excess_pages_pdf_fails_to_safe_export(project, tmp_path, raw):
    store, binding = project
    attachment = _attach(store, tmp_path, "unsupported.pdf", raw)
    result = ProjectBbsAttachmentBridge().execute(store, binding, "attachment_preview", {"attachment": attachment})
    assert result["kind"] == "unsupported" and "导出" in result["message"]
    assert result["image_source"] == "" and not result["text"]


def test_qml_component_loads_and_rejects_all_nonprovider_image_urls(project, tmp_path):
    store, binding = project
    attachment = import_attachment(store, _png(tmp_path))
    engine = QQmlEngine()
    theme = ThemeEngineBridge(build_theme_snapshot("light"), 0, "light", "light", "qa", "QA", {}, parent=engine)
    engine.rootContext().setContextProperty("Theme", theme)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] /
        "coat_sim/ui/qml/ProjectBbsAttachmentPreview.qml")))
    assert not component.isError(), component.errorString()
    root = component.createWithInitialProperties({"themeTokens": theme.snapshot, "attachment": attachment})
    assert isinstance(root, QQuickItem), component.errorString()
    helper = ProjectBbsAttachmentBridge()
    try:
        assert helper.install(root)
        assert helper.install(root)
        for url in ("https://invalid.example/x.png", "file:///etc/passwd", "data:image/png;base64,aA==", "image://projectbbs/../secret"):
            root.setProperty("preview", {"asset_id": attachment["asset_id"], "kind": "image", "image_source": url})
            assert root.property("safeImageSource") == ""
        preview = helper.execute(store, binding, "attachment_preview", {"attachment": attachment})
        root.setProperty("preview", preview)
        assert root.property("safeImageSource") == preview["image_source"]
        image = root.findChild(QObject, "projectBbsAttachmentImage")
        assert image is not None and image.property("source").toString() == preview["image_source"]
        provider = engine.imageProvider("projectbbs")
        assert isinstance(provider, ProjectBbsImageProvider)
        assert not provider.requestImage(preview["image_source"].rsplit("/", 1)[1], QSize(), QSize()).isNull()
    finally:
        helper.clear()
        root.deleteLater()
        engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
