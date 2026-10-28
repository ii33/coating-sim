"""A deadline at the second admission check retains the audited thinning incumbent."""
from copy import deepcopy
from typing import Any

import pytest

from coat_sim.core import film_auto_synthesis as synthesis
from coat_sim.core import film_synthesis_audit as audit
from coat_sim.core import film_synthesis_thickness as thickness
from coat_sim.core.film_synthesis_archive import physical_design_hash
from tests.test_film_synthesis_thickness_no_hidden_budget import (
    assert_bound_evidence,
    improving_seed,
    total,
)


pytestmark = [pytest.mark.algorithm, pytest.mark.contract]


def test_second_admission_clock_deadline_keeps_audited_incumbent_without_new_audit(monkeypatch):
    service, request = improving_seed(max_seconds=10.)
    armed = False
    admission_clock_reads: list[float] = []
    trials: list[str] = []
    audits: list[str] = []
    late_audits: list[str] = []
    snapshots: list[dict[str, Any]] = []
    real_audit = audit.audit_frozen_synthesis_candidate

    def clock():
        if not armed:
            return 0.
        # _check_cancel's shared event sees time before the deadline. The
        # explicit admission clock immediately afterwards observes expiry.
        now = 9. if not admission_clock_reads else 11.
        admission_clock_reads.append(now)
        return now

    def track_audit(*args, **kwargs):
        candidate = args[1] if len(args) > 1 else kwargs['design']
        identity = physical_design_hash(candidate)
        audits.append(identity)
        if armed:
            late_audits.append(identity)
        return real_audit(*args, **kwargs)

    def backend(request, initial, *, evaluate, archive, cancel_check, **_kwargs):
        nonlocal armed
        assert total(initial['design']) == 130.
        assert not trials, 'a deadline cannot silently restart a local solve'
        candidate = deepcopy(initial['design'])
        candidate['front_layers'][0]['thickness']['value'] = 129.
        value = evaluate(candidate, jacobian=True)
        assert value['score']['nominal_pass']
        assert thickness.preserves_optical_quality(request, initial, value)
        trials.append(physical_design_hash(value['design']))
        archive(value, audit_eligible=False)
        armed = True
        cancel_check()
        pytest.fail('the explicit second admission check must stop the backend')

    def snapshot(design, training, validation, audit_metadata):
        snapshots.append(dict(design=deepcopy(design), training=deepcopy(training),
            validation=deepcopy(validation), audit=deepcopy(audit_metadata)))

    monkeypatch.setattr(synthesis, 'monotonic', clock)
    monkeypatch.setattr(audit, 'audit_frozen_synthesis_candidate', track_audit)
    monkeypatch.setattr(thickness, 'reduce_total_thickness', backend)
    result = synthesis.synthesize_film_design(service, request, snapshot_callback=snapshot)

    assert admission_clock_reads[:2] == [9., 11.]
    assert len(trials) == 1 and snapshots and audits
    assert not late_audits, 'expiry must not admit a new audit to repair evidence'
    assert result.termination_reason == 'time_limit'
    assert result.candidate_design is not None and total(result.candidate_design) == 130.
    incumbent_hash = physical_design_hash(result.candidate_design)
    assert incumbent_hash in audits and incumbent_hash != trials[0]
    assert physical_design_hash(snapshots[-1]['design']) == incumbent_hash
    assert_bound_evidence(result)
    detail = result.performance['search']['hybrid']['thickness_reduction']
    assert detail['status'] == detail['stop_reason'] == 'time_limit'
    assert not detail['promoted'] and not detail['independent_audit_performed']
    assert result.candidate_provenance['validation_matches_candidate'] is True
    assert result.candidate_provenance['frozen_at_time_limit'] is True
