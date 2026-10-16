"""Check the fixed-reference chapter, data lineage and offline HTML contract."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from verify_film_design_generator_docs import DocumentParser


def main():
    path = ROOT / "docs/algorithms.html"
    text = path.read_text(encoding="utf-8")
    parser = DocumentParser()
    parser.feed(text)
    anchor = "film-reference-fixed-recipes"
    assert parser.ids.count(anchor) == parser.fragment_links.count(anchor) == 1
    section = re.search(r'<section id="film-reference-fixed-recipes">(.*?)</section>', text, re.S)
    assert section is not None
    body = section.group(1)
    assert body.count("<svg") == body.count("<figcaption>") == 1
    assert not parser.remote_resources
    assert not re.search(r'@import\s|url\(\s*["\']?(?:https?:|//)', text, re.I)
    for token in ("2026-10-12", "2026-10-11", "本项目约定", "substrate_outward", "nm", "Δn", "k=0",
                  "7700", "12300", "10263.39", "400–800", "81点", "20001", "不继承",
                  "comsol_ideal_single_ar", "comsol_qq_mgf2_cef3", "comsol_qhq_mgf2_zro2_cef3",
                  "tfcalc_growing_ar_13", "Ge", "MgF₂", "固定", "schema v1", "参考配方契约",
                  "Δk", "保留真实k", "入射端口须透明", "N=n+i k", "null",
                  "不是整片透射率", "进入基片界面的功率", "A仅表示膜层吸收"):
        assert token in body, token
    assert "hulinks.co.jp" in body and "doc.comsol.com" in body
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/algorithms.html#film-reference-fixed-recipes" in readme
    assert "docs/film-design-reference-library-contract.md" in readme
    contract = ROOT / "docs/film-design-reference-library-contract.md"
    assert contract.exists()
    result = {"status": "passed", "target": anchor, "offline": True,
              "inline_diagram_with_caption": 1, "toc_entry_and_section_unique": True,
              "existing_duplicate_ids": [key for key, count in Counter(parser.ids).items() if count > 1],
              "algorithms_sha256_at_check_time": sha256(path.read_bytes()).hexdigest(),
              "contract_sha256": sha256(contract.read_bytes()).hexdigest(),
              "scope": "Dedicated reference chapter requirements plus whole-document offline resources."}
    output = ROOT / "artifacts/film_design_reference_library_validation/documentation-validation.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "passed", "output": str(output)}))


if __name__ == "__main__":
    main()
