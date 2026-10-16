"""Check the generator's offline algorithm documentation and preserve QA evidence."""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


class DocumentParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.remote_resources = []
        self.section_ids = []
        self.fragment_links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.append(attrs["id"])
            if tag == "section":
                self.section_ids.append(attrs["id"])
        if tag == "a" and attrs.get("href", "").startswith("#"):
            self.fragment_links.append(attrs["href"][1:])
        if tag in {"script", "img", "iframe", "link", "source"}:
            resource = attrs.get("src") or attrs.get("href") or ""
            if resource.startswith(("http:", "https:", "//")):
                self.remote_resources.append(resource)


def main():
    html_path = ROOT / "docs/algorithms.html"
    text = html_path.read_text(encoding="utf-8")
    parser = DocumentParser()
    parser.feed(text)
    target = "film-parametric-generator"
    duplicates = [value for value, count in Counter(parser.ids).items() if count > 1]
    assert parser.ids.count(target) == 1
    assert parser.fragment_links.count(target) == 1
    section_match = re.search(
        r'<section\s+id="film-parametric-generator">(.*?)</section>', text, re.S)
    assert section_match is not None
    section = section_match.group(1)
    assert section.count("<svg") == section.count("<figcaption>") == 5
    assert "2026-10-12" in section and "本项目约定" in section
    assert "入射端口另须已知k=0" in section and "不将微小已知k清零" in section
    assert "substrate_outward" in section and 'substrate.sides="single"' in section
    assert not parser.remote_resources
    assert not re.search(r'@import\s|url\(\s*["\']?(?:https?:|//)', text, re.I)
    assert not re.search(r"<script|<img", section, re.I)
    assert "docs/algorithms.html" in (ROOT / "README.md").read_text(encoding="utf-8")
    documents = ["docs/film-design-generator-contract.md",
                 "docs/film-design-generator-guide.md",
                 "docs/film-design-generator-stage-report-20261011.md"]
    for path in documents:
        assert (ROOT / path).is_file()
    result = {
        "status": "passed", "algorithm_section": target,
        "svg_diagrams_with_captions": 5, "single_section_and_toc_entry": True,
        "remote_runtime_resources": parser.remote_resources,
        "readme_algorithm_entry": True,
        "all_section_ids_at_check_time": parser.section_ids,
        "existing_duplicate_ids": duplicates,
        "algorithms_sha256_at_check_time": sha256(html_path.read_bytes()).hexdigest(),
        "scope": "Generator section assertions; document-wide offline resource check; existing sections retained.",
    }
    output = ROOT / "artifacts/film_design_generator_validation/documentation-validation.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "diagrams": 5,
                      "offline": True, "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
