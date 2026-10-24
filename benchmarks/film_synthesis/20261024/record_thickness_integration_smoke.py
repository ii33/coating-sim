"""Source-checkout harness: real hybrid opt-out/default comparison, not from-zero proof."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from tests.test_film_synthesis_thickness_integration import request_case, run_case, total

rows = []
for family in ('HR', 'AR', 'mixed_RT'):
    for mode in ('mean', 'pointwise', 'both'):
        service, request, initial = request_case(family, mode)
        disabled = run_case(service, replace(request, enable_thickness_reduction=False))
        enabled = run_case(service, request)
        record = enabled.performance['search']['hybrid']['thickness_reduction']
        row = dict(family=family, criteria=mode, layers_before=len(initial['design']['front_layers']),
            layers_after=len(enabled.candidate_design['front_layers']),
            disabled_total_nm=total(disabled), enabled_total_nm=total(enabled),
            reduction_nm=total(disabled)-total(enabled), disabled_evaluations=disabled.evaluations,
            enabled_evaluations=enabled.evaluations, additional_evaluations=enabled.evaluations-disabled.evaluations,
            disabled_targets_met=disabled.targets_met, enabled_targets_met=enabled.targets_met,
            status=record['status'], training_calls=record['training_evaluations_used'],
            proposed_total_nm=record['proposed_total_thickness_nm'],
            audit_performed=record['independent_audit_performed'], promoted=record['promoted'])
        rows.append(row)
        print(json.dumps(row), flush=True)
report = dict(recorded_at_utc=datetime.now(timezone.utc).isoformat(), current_main='aa91eca',
    source_sha256=hashlib.sha256(Path('coat_sim/core/film_auto_synthesis.py').read_bytes()).hexdigest(),
    scope='real default hybrid; identical feasible synthetic-material seeds, goals, cap300, workers1; not from-zero complex-multispectral comparison',
    stage_policy='at most32 parent evaluation calls and16 SLSQP iterations; one original independent audit before promotion',
    rows=rows)
Path('benchmarks/film_synthesis/20261024/thickness-integration-smoke.json').write_text(
    json.dumps(report, indent=2, allow_nan=False)+'\n')
