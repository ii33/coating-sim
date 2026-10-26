"""Small UI binding for durable structural continuation, without optical replay.

Only a task reference is saved in the existing optimization configuration.
Frozen requests, candidates and the frontier stay in the existing task store.
The controller retains its ordinary submission/apply/edit-epoch/undo owners.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path
import re

from coat_sim.core.film_synthesis_continuation import (
    MAX_SEARCH_STATE_BYTES, continue_synthesis_request, load_search_state,
    search_fingerprint, validate_search_state,
)


def _root_id(service):
    return hashlib.sha256(str(Path(service.root).resolve()).casefold().encode('utf-8')).hexdigest()


def settings_bundle(configuration):
    """Persist editor semantics once, without variable rows or display settings."""
    synthesis = configuration['synthesis']
    options = deepcopy(synthesis['options'])
    for key in ('max_cycles', 'max_evaluations', 'max_seconds', 'max_working_set_bytes',
                'parallel_batch_size', 'enable_thickness_reduction'):
        options.pop(key, None)
    return dict(objectives=deepcopy(configuration['objectives']),
        materials=[{k:r[k] for k in ('material_id','optical_set_id')} for r in synthesis['material_rows']],
        constraints={k:options.pop(k, None) for k in ('minimum_nonzero_thickness_nm',
            'max_layer_thickness_nm', 'max_total_thickness_nm', 'max_layers')},
        search=options, assume_unknown_k_zero=synthesis['assume_unknown_k_zero'],
        acceptance={k:configuration['solver'][k] for k in ('acceptable_error_percent','validation_samples_multiplier')})


def make_reference(service, request, job_id, spec_fingerprint, settings):
    return dict(version=1, kind='coat_sim.synthesis_search_reference', root_id=_root_id(service),
        design_id=request.environment['id'], job_id=str(job_id), spec_fingerprint=str(spec_fingerprint),
        settings=deepcopy(settings))


def check_reference(service, reference, snapshot):
    keys = {'version','kind','root_id','design_id','job_id','spec_fingerprint','settings'}
    if (not isinstance(reference, dict) or set(reference) != keys or type(reference.get('version')) is not int
            or reference['version'] != 1 or reference['kind'] != 'coat_sim.synthesis_search_reference'):
        raise ValueError('搜索进度引用版本不支持或已损坏；原任务保留，请重新开始搜索。')
    if (not isinstance(reference['job_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', reference['job_id'])
            or not isinstance(reference['spec_fingerprint'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', reference['spec_fingerprint']) or not isinstance(reference['settings'], dict)):
        raise ValueError('搜索进度任务身份无效；原任务保留。')
    if reference['root_id'] != _root_id(service) or reference['design_id'] != snapshot.design['id']:
        raise ValueError('搜索进度属于另一项目或膜系；原任务保留，请重新开始搜索。')


def settings_reason(reference, current):
    previous = reference['settings']
    if previous.get('objectives') != current['objectives'] or previous.get('acceptance') != current['acceptance']:
        return '目标、角度、采样或验收设置已改变，需重新开始搜索；原进度保留在任务历史。'
    if previous.get('materials') != current['materials'] or previous.get('assume_unknown_k_zero') != current['assume_unknown_k_zero']:
        return '候选材料或吸收假设已改变，需重新开始搜索；原进度保留在任务历史。'
    if previous.get('constraints') != current['constraints']:
        return '层数或膜厚制造约束已改变，需重新开始搜索；原进度保留在任务历史。'
    if previous.get('search') != current['search']:
        return '结构搜索参数已改变，需重新开始搜索；原进度保留在任务历史。'
    return ''


def load_record(service, reference, snapshot):
    """Load only bounded task-owned JSON and its immutable source binding."""
    check_reference(service, reference, snapshot)
    from coat_sim.scheduler.jobs import JobSpec
    from coat_sim.scheduler.store import SchedulerStore
    from coat_sim.core.film_synthesis_jobs import deserialize_job_payload
    from coat_sim.core.film_continuation_editable_snapshot import restore_editable_snapshot_delta
    from coat_sim.ui.film_optimization_continue_source import SOURCE_INPUT_KEY, validate_source_binding
    root = Path(service.root).resolve()
    folder = root/'artifacts/runtime/scheduler/jobs'/reference['job_id']
    spec_path = folder/'spec.json'
    if folder.is_symlink() or spec_path.is_symlink() or not spec_path.resolve().is_relative_to(root):
        raise ValueError('搜索进度文件不属于当前任务目录。')
    if not spec_path.is_file():
        raise ValueError('搜索进度对应的任务文件已不存在；不会从旧冠军冒充续接。')
    with spec_path.open('rb') as stream:
        data = stream.read(MAX_SEARCH_STATE_BYTES + 1)
    if len(data) > MAX_SEARCH_STATE_BYTES:
        raise ValueError('搜索任务 JSON 超出既有64 MiB传输限制。')
    spec = JobSpec.parse_frozen_json(data.decode('utf-8'))
    row = SchedulerStore(root).status(reference['job_id'])
    if (row.get('state') not in {'succeeded','cancelled','failed'} or spec.executor != 'film_synthesis'
            or row.get('input_digest') != spec.fingerprint or reference['spec_fingerprint'] != spec.fingerprint):
        raise ValueError('搜索任务尚未结束或不可变输入身份不匹配；请先核对任务状态。')
    _, request, _ = deserialize_job_payload(spec.parameters['payload'])
    binding = validate_source_binding(spec.inputs.get(SOURCE_INPUT_KEY), request)
    identity = binding['managed_film']
    if identity is not None:
        actual = service.managed_film_identity(snapshot.path)
        if actual is None or any(actual.get(k) != identity[k] for k in ('project_id','document_id')):
            raise ValueError('搜索进度的来源项目或膜系资源已切换。')
    path = folder/'best-candidate.search-state.json'
    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError('搜索进度文件不属于当前任务。')
    payload = row.get('result') or {}
    state = (load_search_state(path) if path.is_file() else
             (payload.get('result') or {}).get('search_checkpoint') or request.search_checkpoint)
    if state is None:
        raise ValueError('此任务没有结构搜索检查点；不能用候选暖启动冒充继续搜索。')
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    request = freeze_film_synthesis_request(None, request)
    state = validate_search_state(request, state)
    assert request.frozen_material_snapshot is not None
    editable = restore_editable_snapshot_delta(request.frozen_material_snapshot, binding['synthesis_editable_delta'])
    return dict(structural_resume=True, mode='synthesis', request=request, checkpoint=state,
        editable_snapshot=editable, source_binding=binding, job_id=reference['job_id'],
        spec_fingerprint=spec.fingerprint, result=None, synthesis_payload=None, history=True)


def build_request(controller, record):
    """Freshly freeze the current material closure; keep compatible exploration."""
    from coat_sim.core.film_auto_synthesis import freeze_film_synthesis_request
    current = controller.session.snapshot()
    old = record['request']
    environment = deepcopy(current.design)
    environment['front_layers'], environment['back_layers'] = [], []
    environment['expression'] = {'text':'', 'symbols':{}, 'in_sync':True}
    environment['assume_lossless'] = controller._synthesis_assume_unknown_k_zero
    seed = deepcopy(current.design)
    seed['assume_lossless'] = environment['assume_lossless']
    options = controller._synthesis_options
    candidate = replace(old, environment=environment, design_revision=current.design_revision,
        seed_designs=(seed, *old.seed_designs), warm_start_seed_index=0,
        frozen_material_snapshot=None, search_checkpoint=None,
        max_cycles=options.get('max_cycles'), max_evaluations=options.get('max_evaluations'),
        max_seconds=options.get('max_seconds'), cpu_workers=controller._synthesis_cpu_workers,
        forward_backend=controller._synthesis_forward_backend, forward_device=deepcopy(controller._synthesis_forward_device),
        max_working_set_bytes=options.get('max_working_set_bytes', 0), resource_guard_resolution=None,
        enable_thickness_reduction=options.get('enable_thickness_reduction', True))
    candidate = freeze_film_synthesis_request(controller.service, candidate)
    assert candidate.frozen_material_snapshot is not None
    if search_fingerprint(candidate) != record['checkpoint']['fingerprint']:
        if candidate.frozen_material_snapshot['materials'] != old.frozen_material_snapshot['materials']:
            raise ValueError('材料 n/k 或可编辑材料闭包已改变，需重新开始搜索；原进度保留。')
        raise ValueError('基片、介质或冻结约束已改变，需重新开始搜索；原进度保留。')
    return continue_synthesis_request(candidate, record['checkpoint'])


def phase_label(message):
    stage = str(message).split('|',1)[0].strip()
    if stage.startswith(('local_margin','canonical_margin')):
        return '局部优化'
    if stage.startswith(('thickness_reduction','nominal_thickness_reduction')):
        return '减薄'
    if stage in {'completed','cancelled','failed','not_found'}:
        return '本轮完成'
    return None
