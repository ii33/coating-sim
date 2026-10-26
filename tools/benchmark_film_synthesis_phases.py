"""Offline empty-start real-n/k phase benchmark, without a project/editor.

Use --repo-root to run the same harness against an isolated baseline checkout.
Only numerical summaries are written; no user's original project is an input.
"""
from copy import deepcopy
from dataclasses import asdict
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--resume-request', type=Path)
    parser.add_argument('--save-continuation', type=Path)
    parser.add_argument('--seconds', type=float, default=90.)
    args = parser.parse_args()
    root = args.repo_root.resolve()
    sys.path.insert(0, str(root))
    from coat_sim.core.film_auto_synthesis import FilmSynthesisRequest, synthesize_film_design
    from coat_sim.core.film_design import new_design, validate_design
    from coat_sim.core.film_design_service import FilmDesignService
    from coat_sim.core.film_objective_criteria import FilmObjectiveConstraint
    from coat_sim.core.film_optimization import FilmOptimizationObjective
    from coat_sim.core.film_optimization_needle import FilmNeedleMaterialReference
    from coat_sim.core.film_synthesis_archive import physical_design_hash

    catalog_bytes = (root / 'data/optical_catalog.json').read_bytes()
    catalog = json.loads(catalog_bytes)
    rows = [dict(id=r['builtin_material_id'], name=r['material_id'], design_ready=True,
                 optical_sets=deepcopy(r['optical_constants'])) for r in catalog['materials']]
    class Materials:
        def _records(self):
            return deepcopy(rows)
        def get(self, identifier):
            return deepcopy(next(r for r in rows if r['id'] == identifier))
    class Service:
        validate = staticmethod(validate_design)
        freeze_design = FilmDesignService.freeze_design
        materials = Materials()
    if args.resume_request:
        from dataclasses import replace
        from coat_sim.core.film_synthesis_io import film_synthesis_request_from_payload
        _, request, _ = film_synthesis_request_from_payload(json.loads(args.resume_request.read_text()))
        request = replace(request, max_seconds=args.seconds)
    else:
        env = new_design(design_id='empty-real-nk-ar-phase-benchmark')
        env['substrate'].update(material_id='builtin_research_SiO2_film', optical_set_id='ri_sio2_rodriguez_de_marcos')
        env['substrate']['mode'] = 'semi_infinite'
        refs = (FilmNeedleMaterialReference('builtin_research_SiO2_film', 'ri_sio2_rodriguez_de_marcos'),
                FilmNeedleMaterialReference('builtin_research_TiO2_film', 'ri_tio2_jolivet_amorphous'))
        objective = FilmOptimizationObjective(400., 700., 301,
            pointwise=FilmObjectiveConstraint(lower_bound=None, upper_bound=.004))
        request = FilmSynthesisRequest(env, (objective,), refs, cpu_workers=1, parallel_batch_size=1,
            max_seconds=args.seconds, structure_search_seed=args.seed,
            acceptable_normalized_error=0., validation_samples_multiplier=4)
    def summary(design, score):
        layers = design['front_layers']
        return dict(physical_hash=physical_design_hash(design), layers=len(layers),
            total_nm=sum(r['thickness']['value'] for r in layers),
            minimum_nm=min((r['thickness']['value'] for r in layers), default=None),
            worst_R=max((e['actual'] if isinstance(e, dict) else e.actual for e in score['errors']), default=None),
            nominal_pass=score.get('nominal_pass'), max_normalized_error=score['max_error'])
    start = time.perf_counter()
    first = None
    stages = {}
    def progress(row):
        stages.setdefault(row.stage, time.perf_counter() - start)
    def snapshot(design, training, validation, audit):
        nonlocal first
        if first is None and training.get('nominal_pass') and validation.get('nominal_pass'):
            first = dict(elapsed_seconds=time.perf_counter() - start, **summary(design, validation))
    result = synthesize_film_design(Service(), request, progress_callback=progress, snapshot_callback=snapshot)
    report = dict(source_sha256=hashlib.sha256((root/'coat_sim/core/film_auto_synthesis.py').read_bytes()).hexdigest(),
        catalog_sha256=hashlib.sha256(catalog_bytes).hexdigest(), seed=args.seed,
        scope='400-700nm, 301 training points, original independent dense/offset/extremum audit; actual SiO2 Rodriguez-de Marcos and TiO2 Jolivet amorphous n/k; no implicit mean or min layer thickness',
        state=result.state, termination=result.termination_reason, targets_met=result.targets_met,
        elapsed_seconds=time.perf_counter()-start, evaluations=result.evaluations,
        first_feasible=first, stage_seconds=stages,
        final=None if result.candidate_design is None else summary(result.candidate_design, result.validation_score or result.training_score),
        local=None, phase_details=result.performance['search'].get('hybrid', {}))
    report['physical_work'] = {key: result.performance.get(key) for key in (
        'training_physical_evaluations', 'training_wavelength_points', 'training_evaluator_invocations')}
    report['physical_work'].update(audit_physical_evaluations=result.audit_physical_evaluations,
                                  audit_wavelength_points=result.audit_wavelength_points)
    report['phase_measurements'] = {name: {k:v for k,v in report['phase_details'].get(name, {}).items()
        if k in ('elapsed_seconds', 'work', 'evaluations_used', 'evaluations_used_including_audit',
                 'initial_layer_count', 'fixed_layer_ids', 'initial_physical_hash', 'status', 'canonical_preparation',
                 'total_evaluations_used', 'total_elapsed_seconds')}
        for name in ('first_feasible', 'local_margin', 'thickness_reduction', 'continuation_session')}
    report['structural_sequence'] = [json.loads(t.message)['physical_hash'] for t in result.trace
        if t.stage == 'structure_proposal' and 'physical_hash' in json.loads(t.message)]
    local = getattr(result, 'local_optimum', None)
    if local is not None:
        report['local'] = dict(**summary(local['design'], local['validation_score']),
                               termination=local['termination'], signed_margin=local['signed_margin'])
    if getattr(result, 'search_checkpoint', None) is not None:
        state = result.search_checkpoint
        report['search_state'] = dict(cursors=state['cursors'], rotations=state['rotations'],
            tried=len(state['tried']), pool=len(state['pool']), pending=len(state['pending']))
        if args.save_continuation is not None and result.candidate_design is not None:
            from coat_sim.core.film_synthesis_continuation import continue_synthesis_request
            from coat_sim.core.film_synthesis_io import film_synthesis_request_to_payload
            from dataclasses import replace
            continued = continue_synthesis_request(result.frozen_request, state,
                current_design=result.candidate_design, max_seconds=args.seconds)
            args.save_continuation.write_text(json.dumps(film_synthesis_request_to_payload('phase-resume', continued),
                allow_nan=False, ensure_ascii=False), encoding='utf-8')
    args.output.write_text(json.dumps(report, default=lambda o: asdict(o), ensure_ascii=False,
                                     indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('phase_details', 'stage_seconds')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
