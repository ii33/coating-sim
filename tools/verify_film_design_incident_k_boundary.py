"""Observe template and actual service boundaries using isolated synthetic records.

These explicitly labeled test records are held in memory; they do not modify
or substitute constants for a user's real selected material datasets.
"""
from copy import deepcopy
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / 'artifacts' / 'film_design_reference_library_validation'
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_film_design_templates import FilmDesignTemplatesTests
from coat_sim.core.film_design import new_design
from coat_sim.core.film_design_service import FilmDesignService
from coat_sim.core.film_design_templates import build_preview


class MemoryFixtureLibrary:
    def __init__(self, options):
        self.rows = {row['id']: deepcopy(row) for group in options.values() for row in group}

    def _records(self):
        return deepcopy(list(self.rows.values()))

    def get(self, identifier):
        return deepcopy(self.rows[identifier])


def observe():
    QA.mkdir(parents=True, exist_ok=True)
    cases = []
    with tempfile.TemporaryDirectory(prefix='incident-k-', dir=QA) as directory:
        task_root = Path(directory).resolve()
        assert task_root.is_relative_to(QA.resolve())
        for k in (0.0, 1e-15, 1e-14, 1.00001e-14, 1e-10, 1e-9, 1.01e-9, 0.01):
            fixture = FilmDesignTemplatesTests()
            fixture.setUp()
            fixture.options['medium'][0]['optical_sets'][0]['k'] = [k, k, k]
            service = FilmDesignService(task_root, restrict_to_root=True)
            service.materials = MemoryFixtureLibrary(fixture.options)
            case = {'selected_incident_k': k, 'template_id': 'ar_single_qw'}
            try:
                preview = build_preview(inputs=fixture.inputs('ar_single_qw'),
                                        base_design=new_design(), material_options=fixture.options)
                selected = next(row for row in preview['materials'] if row['role'] == '入射介质')
                case.update(template='accepted', selected_k_retained=selected['k'])
            except ValueError as error:
                case.update(template='rejected', template_error=str(error))
                cases.append(case)
                continue
            try:
                spectrum = service.simulate(preview['candidate_design'], {
                    'wavelength_min_nm': 549.0, 'wavelength_max_nm': 551.0, 'samples': 3,
                    'angle_deg': 0.0, 'polarization': 'unpolarized', 'extrapolation_policy': 'error'})
                case.update(service='accepted', points=len(spectrum['wavelength_nm']),
                            definition=spectrum['definition'])
            except ValueError as error:
                case.update(service='rejected', service_error=str(error))
            cases.append(case)
    return {'timestamp_utc': datetime.now(timezone.utc).isoformat(),
            'scope': 'Observed isolated synthetic boundary; no user material edits, no implicit k clearing.',
            'cases': cases,
            'accepted_but_not_simulatable': [case['selected_incident_k'] for case in cases
                                           if case['template'] == 'accepted' and case.get('service') == 'rejected']}


def contracts():
    """Run existing contracts with all test storage confined to this QA tree."""
    with tempfile.TemporaryDirectory(prefix='parameter-contract-', dir=QA) as directory:
        task_root = Path(directory).resolve()
        assert task_root.is_relative_to(QA.resolve())
        previous_tempdir = tempfile.tempdir
        previous_env = {key: os.environ.get(key) for key in ('TEMP', 'TMP')}
        tempfile.tempdir = str(task_root)
        os.environ['TEMP'] = os.environ['TMP'] = str(task_root)
        try:
            suite = unittest.defaultTestLoader.loadTestsFromNames([
                'test_film_design_templates', 'test_film_design_generator_session'])
            log_path = QA / 'parameter-contract-isolated-unittest.log'
            with log_path.open('w', encoding='utf-8') as log:
                result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
            report = {'run': result.testsRun, 'failures': len(result.failures),
                      'errors': len(result.errors), 'skipped': len(result.skipped),
                      'exclusive_root_checked_inside_qa': True,
                      'status': 'passed' if result.wasSuccessful() else 'failed'}
            assert result.wasSuccessful() and result.testsRun == 12 and not result.skipped, report
            return report
        finally:
            tempfile.tempdir = previous_tempdir
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--expect-closed', action='store_true')
    parser.add_argument('--contracts', action='store_true')
    args = parser.parse_args()
    result = observe()
    if args.expect_closed:
        assert result['cases'][0]['service'] == 'accepted'
        assert all(case['template'] == 'rejected' for case in result['cases'][1:])
        assert not result['accepted_but_not_simulatable']
        result['status'] = 'passed'
    if args.contracts:
        result['contracts'] = contracts()
    path = QA / ('incident-k-boundary-validation.json' if args.expect_closed
                 else 'incident-k-boundary-observation.json')
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
