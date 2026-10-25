"""Non-optical provenance for candidate warm restarts.

This record belongs in immutable JobSpec.inputs, never mutable queue metadata.
It identifies a managed resource by its service contract, not by parsing paths.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import re
from typing import Any, Mapping

from coat_sim.scheduler.jobs import JobSpec, canonical

SOURCE_INPUT_KEY = 'film_optimization_source'


def canonical_design_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(dict(value)).encode('utf-8')).hexdigest()


def build_source_binding(service, snapshot, request, *, editable_snapshot=None) -> dict:
    from coat_sim.core.film_optimization_continuation import canonical_request_digest
    identity = None
    resolver = getattr(service, 'managed_film_identity', None)
    if callable(resolver) and snapshot.path:
        # A claimed managed handle that no longer resolves is stale, not an
        # anonymous draft. Never weaken its identity after a lookup failure.
        identity = resolver(snapshot.path)
    frozen = getattr(request, 'frozen_material_snapshot', None)
    value = {
        'schema_version': 1, 'kind': 'coat_sim.film_optimization_source',
        'managed_film': deepcopy(identity),
        'source_design_sha256': canonical_design_hash(snapshot.design),
        'source_design_revision': int(snapshot.design_revision),
        'frozen_request_sha256': canonical_request_digest(request),
        'frozen_material_sha256': (canonical_design_hash(frozen) if frozen is not None else None),
        'synthesis_editable_delta': None,
    }
    if editable_snapshot is not None:
        if not isinstance(frozen, Mapping):
            raise ValueError("合成继续缺少冻结材料快照。")
        from coat_sim.core.film_continuation_editable_snapshot import encode_editable_snapshot_delta
        value['synthesis_editable_delta'] = encode_editable_snapshot_delta(frozen, editable_snapshot)
    return value


def validate_source_binding(value, request) -> dict:
    from coat_sim.core.film_optimization_continuation import canonical_request_digest
    required = {'schema_version', 'kind', 'managed_film', 'source_design_sha256',
        'source_design_revision', 'frozen_request_sha256', 'frozen_material_sha256',
        'synthesis_editable_delta'}
    if (not isinstance(value, dict) or set(value) != required
            or value.get('schema_version') != 1
            or type(value.get('schema_version')) is not int
            or value.get('kind') != 'coat_sim.film_optimization_source'
            or type(value.get('source_design_revision')) is not int
            or value['source_design_revision'] < 0
            or not isinstance(value.get('source_design_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', value['source_design_sha256'])):
        raise ValueError('历史作业缺少当前版本的可信来源绑定，只可查看。')
    identity = value['managed_film']
    if identity is not None and (not isinstance(identity, dict)
            or set(identity) != {'project_id', 'document_id', 'revision'}
            or any(not isinstance(identity.get(key), str) or not identity[key] for key in identity)
            or not re.fullmatch(r'[0-9a-f]{64}', identity['revision'])):
        raise ValueError('历史膜系资源身份无效，只可查看。')
    frozen = getattr(request, 'frozen_material_snapshot', None)
    if (value['frozen_request_sha256'] != canonical_request_digest(request)
            or value['frozen_material_sha256'] != (
                canonical_design_hash(frozen) if frozen is not None else None)):
        raise ValueError('历史来源与冻结请求或材料摘要不匹配，只可查看。')
    return deepcopy(value)


def verified_history_source(root, row, payload) -> dict:
    """Re-read exact local immutable spec, checking authoritative row binding."""
    from pathlib import Path
    from coat_sim.scheduler.worker import _film_reply_matches_payload
    job_id = row.get('job_id')
    if (root is None or not isinstance(job_id, str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', job_id)):
        raise ValueError('历史作业没有可验证的输入身份。')
    root = Path(root).resolve()
    directory = root / 'artifacts/runtime/scheduler/jobs' / job_id
    path = directory / 'spec.json'
    if (directory.is_symlink() or path.is_symlink()
            or not path.resolve().is_relative_to(directory.resolve())
            or not directory.resolve().is_relative_to(root)):
        raise ValueError('历史输入文件不属于本项目。')
    spec = JobSpec.parse_frozen_json(path.read_text(encoding='utf-8'))
    if (row.get('state') not in {'cancelled', 'succeeded', 'failed'}
            or spec.executor != row.get('kind')
            or spec.fingerprint != row.get('input_digest')
            or not _film_reply_matches_payload(payload, spec.parameters.get('payload'))):
        raise ValueError('历史结果与不可变作业规范不匹配。')
    return {'binding': deepcopy(spec.inputs.get(SOURCE_INPUT_KEY)),
            'source_job_id': job_id, 'source_spec_fingerprint': spec.fingerprint}
