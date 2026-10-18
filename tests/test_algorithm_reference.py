"""Fixed shipped-document scope, inert parsing and lossless bounded pagination."""
from pathlib import Path

import pytest

from coat_sim.core import algorithm_reference as reference
from coat_sim.core.project_dependencies import ProjectError

pytestmark = pytest.mark.contract


@pytest.fixture
def document(tmp_path, monkeypatch):
    path = tmp_path / "algorithms.html"
    monkeypatch.setattr(reference, "REFERENCE_PATH", path)
    path.write_text('''<!doctype html><html><head><title>Algorithms &amp; formulas</title>
    <style>SECRET_STYLE</style><script>SECRET_SCRIPT</script></head><body>
    <span class="pill">版本：v7</span><span class="pill">更新：2026-10-18</span>
    <nav>DO_NOT_INDEX_NAV</nav><section id="first"><h2>First <em>formula</em></h2>
    <p>alpha n<sub>i</sub><sup>2</sup> &amp; beta</p>
    <script>SECRET_IN_SECTION</script><template>SECRET_TEMPLATE</template>
    <p><a href="https://doi.org/10.1234/test">Reference</a>
    <a href="javascript:alert(1)">inert label</a>
    <a href="https://user:pass@example.org/">hidden target</a></p>
    <svg><text>diagram label</text><script>SVG_SCRIPT</script></svg></section>
    <section id="second"><h2>Second</h2><p>beta gamma</p></section></body></html>''', encoding="utf-8")
    return path


def test_inert_parser_preserves_citations_metadata_and_formula_labels(document):
    catalog = reference.algorithm_reference_catalog(limit=1)
    assert catalog["source"] == "docs/algorithms.html"
    assert catalog["document_title"] == "Algorithms & formulas"
    assert catalog["document_version"] == "版本：v7"
    assert catalog["document_date"] == "2026-10-18"
    assert catalog["total"] == 2 and catalog["next_offset"] == 1
    assert catalog["sections"][0]["title"] == "First formula"
    row = reference.algorithm_reference_read("first", expected_version=catalog["version"])
    assert "n_(i)^(2) & beta" in row["text"]
    assert "https://doi.org/10.1234/test" in row["text"]
    assert "diagram label" in row["text"]
    assert all(text not in row["text"] for text in ("SECRET", "SVG_SCRIPT", "javascript:", "user:pass", "DO_NOT_INDEX_NAV"))
    assert row["untrusted_reference"] is True
    assert row["citation"] == "docs/algorithms.html#first"


def test_keyword_search_is_literal_and_all_terms_with_result_pagination(document):
    result = reference.algorithm_reference_search("BETA", limit=1)
    assert result["total"] == 2 and result["next_offset"] == 1
    second = reference.algorithm_reference_search("beta", offset=result["next_offset"],
                                                  expected_version=result["version"])
    assert second["results"][0]["section_id"] == "second"
    assert second["has_more"] is False
    assert reference.algorithm_reference_search("alpha gamma")["results"] == []
    assert reference.algorithm_reference_search("SECRET")["results"] == []
    assert reference.algorithm_reference_search(".*")["results"] == []
    assert reference.algorithm_reference_search("formula alpha")["total"] == 1


def test_unicode_read_pages_reconstruct_every_character_and_version_change_rejects(document):
    document.write_text('<section id="long"><h2>中文章节</h2><p>' + "中文字β" * 4000 + '</p></section>', encoding="utf-8")
    parts, offset = [], 0
    version = reference.algorithm_reference_catalog()["version"]
    while True:
        result = reference.algorithm_reference_read("long", offset, 113, version)
        assert len(result["text"]) <= 113
        parts.append(result["text"])
        if result["next_offset"] is None:
            break
        offset = result["next_offset"]
    assert "".join(parts) == "中文章节\n" + "中文字β" * 4000
    assert len("".join(parts)) == result["total_chars"]
    document.write_text(document.read_text() + "<!-- changed -->", encoding="utf-8")
    with pytest.raises(ProjectError, match="changed") as error:
        reference.algorithm_reference_read("long", expected_version=version)
    assert error.value.code == "reference_version_changed"


@pytest.mark.parametrize("arguments", [{"offset": True}, {"offset": 1.0}, {"offset": -1},
    {"limit": False}, {"limit": 51}, {"limit": "3"}, {"expected_version": "path"}])
def test_catalog_arguments_are_strict_before_file_access(document, arguments):
    with pytest.raises(ProjectError) as error:
        reference.algorithm_reference_catalog(**arguments)
    assert error.value.code == "invalid_arguments"


@pytest.mark.parametrize("query", ["", "  ", "a" * 201, "a " * 13, None, "a\0"])
def test_query_bounds(document, query):
    with pytest.raises(ProjectError) as error:
        reference.algorithm_reference_search(query)
    assert error.value.code == "invalid_arguments"


@pytest.mark.parametrize("section", ["../../secret", "/tmp/secret", "https://example.org", ""])
def test_no_arbitrary_path_or_url_read(document, section):
    with pytest.raises(ProjectError) as error:
        reference.algorithm_reference_read(section)
    assert error.value.code == "invalid_arguments"


def test_missing_unknown_oversized_and_malformed_source_are_explicit(document, monkeypatch):
    with pytest.raises(ProjectError) as error:
        reference.algorithm_reference_read("absent")
    assert error.value.code == "reference_section_not_found"
    monkeypatch.setattr(reference, "MAX_SOURCE_BYTES", 10)
    with pytest.raises(ProjectError) as error:
        reference.algorithm_reference_catalog()
    assert error.value.code == "reference_capacity"
    monkeypatch.setattr(reference, "MAX_SOURCE_BYTES", 100000)
    document.write_text('<section id="a"><h2>bad</h2>', encoding="utf-8")
    with pytest.raises(ProjectError) as error:
        reference.algorithm_reference_catalog()
    assert error.value.code == "reference_invalid"
    document.write_text('<section id="a"></section><section id="a"></section>', encoding="utf-8")
    with pytest.raises(ProjectError) as error:
        reference.algorithm_reference_catalog()
    assert error.value.code == "reference_invalid"
    document.unlink()
    with pytest.raises(ProjectError) as error:
        reference.algorithm_reference_catalog()
    assert error.value.code == "reference_unavailable"


def test_links_and_directories_are_rejected(document, tmp_path):
    real = tmp_path / "private.txt"
    real.write_text("private data")
    document.unlink()
    document.symlink_to(real)
    with pytest.raises(ProjectError) as error:
        reference.algorithm_reference_catalog()
    assert error.value.code == "unsafe_path"
    document.unlink()
    document.mkdir()
    with pytest.raises(ProjectError) as error:
        reference.algorithm_reference_catalog()
    assert error.value.code == "reference_unsafe"


def test_shipped_reference_has_complete_catalog_and_readable_last_sections():
    assert Path(reference.REFERENCE_PATH).name == "algorithms.html"
    rows, offset, version = [], 0, None
    while True:
        result = reference.algorithm_reference_catalog(offset, 17, version)
        version = result["version"]
        rows.extend(result["sections"])
        if not result["has_more"]:
            break
        offset = result["next_offset"]
    assert len(rows) == result["total"] >= 140
    assert len({row["section_id"] for row in rows}) == len(rows)
    assert reference.algorithm_reference_read(rows[-1]["section_id"], expected_version=version)["text"]
    # Longest chapters are recoverable beyond the default first 6000 characters.
    longest = max(rows, key=lambda row: row["total_chars"])
    assert longest["total_chars"] > 6000
    assert reference.algorithm_reference_read(longest["section_id"], offset=6000,
                                               expected_version=version)["text"]


def test_catalog_titles_are_bounded_without_losing_full_heading_in_reads(document):
    title = "长标题" * 1000
    document.write_text('<section id="large"><h2>' + title + '</h2><p>body</p></section>', encoding="utf-8")
    row = reference.algorithm_reference_catalog()["sections"][0]
    assert len(row["title"]) == 500 and row["title_truncated"] is True
    assert reference.algorithm_reference_read("large")["text"] == title + "\nbody"


def test_hardlinked_document_does_not_expand_fixed_source_policy(document, tmp_path):
    import os
    os.link(document, tmp_path / "alias.html")
    with pytest.raises(ProjectError) as error:
        reference.algorithm_reference_catalog()
    assert error.value.code == "reference_unsafe"
