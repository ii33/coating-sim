"""Source selection must retain full-catalog change detection and isolation."""
from copy import deepcopy
import os

import pytest

from tests.film_structure_fixtures import fixture


@pytest.mark.parametrize('double', [False, True])
def test_selected_freeze_is_identical_to_legacy_noarg_provider(tmp_path, monkeypatch, double):
    service, library, design = fixture(tmp_path)
    expected_ids = {'acceptance-glass', 'acceptance-film'}
    if double:
        back_material = deepcopy(library.read('acceptance-film')['record'])
        back_material.update(id='acceptance-back', name='Distinct back film')
        library.write(back_material)
        design['substrate'].update(sides='double', mode='incoherent')
        design['back_layers'] = [{**deepcopy(design['front_layers'][0]), 'id': 'back-layer',
                                  'material_id': 'acceptance-back'}]
        expected_ids.add('acceptance-back')
    materials = service.materials
    original = materials._records
    selected_calls = []
    def trace(material_ids=None):
        selected_calls.append(material_ids)
        return original(material_ids=material_ids)
    monkeypatch.setattr(materials, '_records', trace)
    fast = service.freeze_design(design)
    assert selected_calls == [expected_ids]
    class LegacyProvider:
        def _records(self):
            return original()
    service.materials = LegacyProvider()
    old = service.freeze_design(design)
    assert fast == old
    assert fast['sha256'] == old['sha256']


def test_get_returns_isolated_copy_and_unknown_id_keeps_error(tmp_path, monkeypatch):
    service, _library, _design = fixture(tmp_path)
    materials = service.materials
    original = materials._records
    calls = []
    def trace(material_ids=None):
        calls.append(material_ids)
        return original(material_ids=material_ids)
    monkeypatch.setattr(materials, '_records', trace)
    first = materials.get('acceptance-film')
    first['optical_sets'][0]['n'][0] = 7.77
    assert materials.get('acceptance-film')['optical_sets'][0]['n'][0] != 7.77
    assert calls == [('acceptance-film',), ('acceptance-film',)]
    for unknown in ('missing-material', ['unhashable']):
        with pytest.raises(ValueError, match='找不到材料'):
            materials.get(unknown)


def test_same_size_and_mtime_edit_invalidates_get_and_frozen_payload(tmp_path):
    service, library, design = fixture(tmp_path)
    before_record = service.materials.get('acceptance-film')
    before_payload = service.freeze_design(design)
    path = library.directory / 'acceptance-film.json'
    old_stat = path.stat()
    original = path.read_bytes()
    old = str(before_record['optical_sets'][0]['n'][0]).encode()
    new = b'1.24' if old == b'1.23' else b'1.22'
    assert len(old) == len(new) and old in original
    changed = original.replace(old, new, 1)
    assert changed != original and len(changed) == len(original)
    path.write_bytes(changed)
    os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    assert path.stat().st_size == old_stat.st_size and path.stat().st_mtime_ns == old_stat.st_mtime_ns
    after_record = service.materials.get('acceptance-film')
    after_payload = service.freeze_design(design)
    assert after_record['optical_sets'][0]['n'][0] != before_record['optical_sets'][0]['n'][0]
    assert after_payload['sha256'] != before_payload['sha256']


def test_lightweight_picker_matches_full_options_order_and_isolation(tmp_path):
    service, _library, _design = fixture(tmp_path)
    materials = service.materials
    purposes = ('film', 'substrate', 'medium')
    full = materials.options_many(purposes)
    light = materials.picker_options_many(purposes)
    fields = ('id', 'name', 'design_ready', 'roles', 'category')
    for purpose in purposes:
        expected = [{**{key: row[key] for key in fields if key in row},
                     'optical_sets': [{key: optical[key] for key in ('id', 'name') if key in optical}
                                      for optical in row.get('optical_sets', [])]}
                    for row in full[purpose]]
        assert light[purpose] == expected
        for row in light[purpose]:
            assert set(row) <= set(fields) | {'optical_sets'}
            assert all(set(optical) <= {'id', 'name'} for optical in row['optical_sets'])
    target = next(row for row in light['film'] if row['id'] == 'acceptance-film')
    target['name'] = 'corrupted client copy'
    target['optical_sets'][0]['id'] = 'corrupted'
    refreshed = next(row for row in materials.picker_options_many(('film',))['film'] if row['id'] == 'acceptance-film')
    assert refreshed['name'] != 'corrupted client copy'
    assert refreshed['optical_sets'][0]['id'] == 'film-set'
    assert materials.get('acceptance-film')['optical_sets'][0]['n']


def test_lightweight_picker_usage_changes_detected_even_same_size_and_mtime(tmp_path):
    service, _library, _design = fixture(tmp_path)
    materials = service.materials
    path = materials.usage_path
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"schema_version":1,"usage":{"acceptance-film":{"film":true }}}'
    changed = original.replace(b'true ', b'false')
    assert len(original) == len(changed)
    path.write_bytes(original)
    old = path.stat()
    assert 'acceptance-film' in {row['id'] for row in materials.picker_options_many(('film',))['film']}
    path.write_bytes(changed)
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert path.stat().st_size == old.st_size and path.stat().st_mtime_ns == old.st_mtime_ns
    assert 'acceptance-film' not in {row['id'] for row in materials.picker_options_many(('film',))['film']}
    assert 'acceptance-film' not in {row['id'] for row in materials.options_many(('film',))['film']}
