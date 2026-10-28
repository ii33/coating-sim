"""A real worker must retain a failed reduction envelope and its failure state."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
from coat_sim.core.film_optimization import FilmOptimizationObjective, FilmOptimizationRequest, FilmThicknessVariable
from coat_sim.core.film_reduction import (
    FilmReductionRequest, film_reduction_result_from_payload, film_reduction_result_to_payload, reduce_film_design,
)
from coat_sim.scheduler.jobs import canonical, freeze_spec
from coat_sim.scheduler.store import SchedulerStore
from tests.film_structure_fixtures import fixture

pytestmark = pytest.mark.contract


def test_real_worker_preserves_algorithm_failure_instead_of_reporting_success(tmp_path):
    service, _, design = fixture(tmp_path)
    request = FilmReductionRequest(FilmOptimizationRequest(design, 1,
        (FilmThicknessVariable('front', design['front_layers'][0]['id'], 0., 200.),),
        (FilmOptimizationObjective(550., 552., 3,
            pointwise=FilmObjectiveConstraint(mode='range', lower_bound=0., upper_bound=1.)),),
        max_iterations=2, max_evaluations=8))
    audited = reduce_film_design(service, request)
    failed = replace(audited, base_result=replace(audited.base_result, state='failed',
        termination_reason='failed', message='controlled reduction failure'))
    payload = film_reduction_result_to_payload(failed.frozen_request, failed)
    decoded = film_reduction_result_from_payload(payload)
    assert decoded.state == 'failed' and decoded.candidate_verified
    spec = freeze_spec(service, 'film_optimization', {'request': failed.frozen_request})
    job_id, instance = uuid4().hex, uuid4().hex
    directory = tmp_path / 'artifacts/runtime/scheduler/jobs' / job_id
    directory.mkdir(parents=True)
    SchedulerStore(tmp_path).submit(spec, job_id=job_id)
    (directory / 'spec.json').write_text(canonical(spec), encoding='utf-8')
    (directory / 'launch.json').write_text(json.dumps({'instance': instance}), encoding='utf-8')
    stored = directory / 'controlled-reply.json'
    stored.write_text(json.dumps(payload), encoding='utf-8')
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(repo), PYTHONDONTWRITEBYTECODE='1')
    code = ('from coat_sim.scheduler import worker; import json,sys; '
            'payload=json.load(open(sys.argv[5],encoding="utf-8")); '
            'worker._film_optimization=lambda *args,**kwargs:payload; '
            'raise SystemExit(worker.run(sys.argv[1],sys.argv[2],int(sys.argv[3]),sys.argv[4]))')
    completed = subprocess.run([sys.executable, '-c', code, str(tmp_path), job_id,
        str(os.getpid()), instance, str(stored)], cwd=repo, env=env,
        capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    outcome = json.loads((directory / 'outcome.json').read_text(encoding='utf-8'))
    assert outcome['state'] == 'failed'
    assert outcome['error_code'] == 'algorithm_failed'
    assert outcome['error'] == 'controlled reduction failure'
    assert outcome['result'] == payload
