"""Freeze reference-library deliverables and actual read-only dependencies."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from record_film_design_generator_manifest import ROOT, DEPENDENCIES, digest

QA = ROOT / 'artifacts' / 'film_design_reference_library_validation'
OWNED = (
    'coat_sim/core/film_design_reference_recipes.py',
    'coat_sim/core/film_design_templates.py',
    'coat_sim/ui/film_quick_controller.py',
    'coat_sim/ui/qml/FilmDesignQuick.qml',
    'coat_sim/ui/qml/FilmDesignReferencePanel.qml',
    'tests/test_film_design_reference_recipes.py',
    'tests/test_film_design_reference_bridge.py',
    'tests/test_film_design_reference_qt.py',
    'tests/test_film_design_templates.py',
    'docs/film-design-reference-library-contract.md',
    'docs/film-design-reference-library-stage-report-20261011.md',
    'docs/film-design-generator-contract.md',
    'docs/film-design-generator-guide.md',
    'docs/film-design-generator-stage-report-20261011.md',
    'docs/algorithms.html', 'README.md',
    'tasks/film-design-generator-ownership-20261011.md',
    'tools/verify_film_design_reference_library.py',
    'tools/verify_film_design_reference_docs.py',
    'tools/verify_film_design_generator_docs.py',
    'tools/verify_film_design_reference_storage.py',
    'tools/verify_film_design_incident_k_boundary.py',
    'tools/record_film_design_reference_manifest.py',
)


def record_material_fingerprints():
    """Snapshot exact selected optical records, without writing material data."""
    sys.path.insert(0, str(ROOT))
    from coat_sim.core.film_design_service import FilmDesignService
    options = FilmDesignService(ROOT).material_options_many(('film', 'substrate', 'medium'))
    records = {row['id']: row for group in options.values() for row in group}
    selected = (
        ('builtin_ideal_air', 'ideal_air_n1_k0'),
        ('builtin_research_Ge_film', 'ri_ge_amotchkina_film'),
        ('builtin_zns_literature_20261004', 'ri_zns_amotchkina_n'),
        ('builtin_research_YbF3_film', 'ri_ybf3_amotchkina_n'),
        ('builtin_research_Ge_substrate', 'ri_ge_burnett2016_table1_vacuum'),
        ('builtin_research_fused_silica_method_demonstrator', 'ri_sio2_malitson'),
        ('film_substrate_malitson_silica', 'malitson_silica_sellmeier'),
        ('builtin_research_Cu_film', 'ri_cu_mcpeak'),
    )
    rows = []
    for material_id, optical_set_id in selected:
        material = records[material_id]
        optical = next(row for row in material['optical_sets'] if row['id'] == optical_set_id)
        def canonical_hash(value):
            data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':'), allow_nan=False).encode('utf-8')
            return hashlib.sha256(data).hexdigest()
        rows.append({'material_id': material_id, 'optical_set_id': optical_set_id,
                     'material_name': material['name'], 'optical_set_name': optical['name'],
                     'material_record_sha256': canonical_hash(material),
                     'optical_set_sha256': canonical_hash(optical),
                     'wavelength_unit': optical.get('wavelength_unit'),
                     'wavelength_min': optical.get('wavelength_min'),
                     'wavelength_max': optical.get('wavelength_max')})
    result = {'timestamp_utc': datetime.now(timezone.utc).isoformat(), 'read_only': True,
              'canonicalization': 'UTF-8 JSON, sorted keys, compact separators, allow_nan=False',
              'scope': 'Handoff-time fingerprints of real datasets used by numerical, bridge and Qt validation; '
                       'not a reproduction of source datasets or a new material registration.', 'datasets': rows}
    (QA / 'selected-material-dataset-fingerprints.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    record_material_fingerprints()
    evidence = {str(path.relative_to(ROOT)).replace('\\', '/'): digest(path)
                for path in sorted(QA.iterdir()) if path.is_file() and path.name != 'manifest.json'}
    legacy_qa = ROOT / 'artifacts' / 'film_design_generator_validation'
    for name in ('generator-qt-unittest.log', 'generator-qt-validation.json',
                 'existing-controller-targeted-regressions.log', 'qt-regression-validation.json',
                 'documentation-validation.json'):
        path = legacy_qa / name
        evidence[str(path.relative_to(ROOT)).replace('\\', '/')] = digest(path)
    readonly = tuple(DEPENDENCIES) + (
        'coat_sim/core/film_design_session.py', 'coat_sim/core/optical_constants.py',
        'data/optical_catalog.json',
        'coat_sim/ui/qml/FilmDesignGeneratorPanel.qml',
        'coat_sim/ui/qml/FilmDesignExpressionPanel.qml',
        'coat_sim/ui/qml/FilmDesignLayerRow.qml',
        'tests/test_film_design_generator_qt.py',
        'tests/test_film_quick_controller.py',
        'tools/verify_film_design_generator_qt.py',
        'tools/verify_film_design_generator_qt_regressions.py')
    report = {'timestamp_utc': datetime.now(timezone.utc).isoformat(),
              'scope': 'Finite four-reference-library handoff. Read-only dependency snapshots '
                       'do not claim file ownership; individual reports retain execution timestamps.',
              'owned_or_shared_document_files': {path: digest(ROOT / path) for path in OWNED},
              'read_only_dependency_snapshot': {path: digest(ROOT / path) for path in readonly},
              'evidence_files': evidence, 'commit_push_publish_performed': False,
              'user_design_or_material_data_modified': False}
    (QA / 'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'manifest': str(QA / 'manifest.json'), 'files': len(OWNED),
                      'dependencies': len(readonly), 'evidence': len(evidence)}))


if __name__ == '__main__':
    main()
