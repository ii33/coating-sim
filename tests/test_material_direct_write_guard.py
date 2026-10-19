"""Direct data-file writes must not publish an unreadable material record.

The assistant's generic file writer may create material JSON, but it must obey
the same material schema the library enforces on read. A record carrying an
unsupported ``category`` value (or any unknown field) used to be published with
success and then made the whole material library unreadable. This regression
uses temporary data only and distinguishes the write-guard fix from the
separate stale-editor-cache bug.
"""
from __future__ import annotations

import json

import pytest

from coat_sim.core import data_files
from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import default_material_profile
from coat_sim.core.schemas import MaterialRecord


@pytest.fixture
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setattr(data_files, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(data_files, "DATA_ROOT", tmp_path / "data")
    return tmp_path


def _profile(**overrides):
    profile = default_material_profile("Direct write", "direct_write")
    profile.update(overrides)
    return profile


def test_direct_write_rejects_unsupported_category_and_keeps_library_readable(isolated_data):
    malformed = _profile()
    malformed["category"] = "optical"  # not film / substrate / other
    with pytest.raises(ValueError) as error:
        data_files.write_data_file("materials/direct_write.json", json.dumps(malformed, ensure_ascii=False))
    assert "分类" in str(error.value)
    assert not (isolated_data / "data/materials/direct_write.json").exists()
    # The library still reads cleanly because nothing malformed was published.
    library = MaterialLibrary(directory=isolated_data / "data/materials")
    listing = library.list()
    assert all(item["id"] != "direct_write" for item in listing["items"])


def test_direct_write_rejects_extra_unknown_field(isolated_data):
    malformed = _profile(category_extra="optical")
    with pytest.raises(ValueError) as error:
        data_files.write_data_file("materials/extra.json", json.dumps(malformed, ensure_ascii=False))
    assert "未知参数" in str(error.value)
    assert not (isolated_data / "data/materials/extra.json").exists()


def test_direct_write_accepts_supported_category_and_reads_back(isolated_data):
    valid = _profile()
    valid["category"] = "other"
    data_files.write_data_file("materials/direct_write.json", json.dumps(valid, ensure_ascii=False))
    library = MaterialLibrary(directory=isolated_data / "data/materials")
    record = library.read("direct_write")["record"]
    assert record["category"] == "other"
    # The typed schema the assistant surface uses agrees with the library.
    assert MaterialRecord.model_validate(record).category == "other"


def test_move_into_materials_rejects_malformed_category(isolated_data):
    source = "materials/pending.json"
    malformed = _profile()
    malformed["category"] = "optical"
    (isolated_data / "data/materials").mkdir(parents=True)
    (isolated_data / "data/materials/pending.json").write_text(
        json.dumps(malformed, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError) as error:
        data_files.move_data_path(source, "materials/final.json")
    assert "分类" in str(error.value)
    assert not (isolated_data / "data/materials/final.json").exists()
