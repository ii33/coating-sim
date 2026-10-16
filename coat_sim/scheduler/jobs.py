"""JSON-only execution contracts. No GUI objects or callable cross IPC."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from coat_sim.appearance.limits import MAX_APPEARANCE_SPEC_BYTES

PROTOCOL_VERSION = 1
MAX_RAYTRACE_SPEC_BYTES = 20 * 1024 * 1024
EXECUTORS = frozenset({'process', 'process_recipe', 'single_source', 'mask_optimization',
                       'film_optimization', 'mask_weights', 'mask_field', 'point_weights',
                       'geometric_raytrace', 'appearance_render', 'python_script', 'probe'})

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

@dataclass(frozen=True)
class ParsedFrozenJobSpec:
    """A validated spec plus the exact canonical bytes used for its digest."""
    executor: str
    parameters: dict
    inputs: dict
    protocol_version: int
    canonical_json: str
    fingerprint: str

    def to_spec(self):
        return JobSpec(self.executor, deepcopy(self.parameters), deepcopy(self.inputs),
                       self.protocol_version)

@dataclass(frozen=True)
class JobSpec:
    executor: str
    parameters: dict
    inputs: dict
    protocol_version: int = PROTOCOL_VERSION

    def to_dict(self):
        return json.loads(canonical(self.__dict__))

    @classmethod
    def parse(cls, value):
        return cls.parse_frozen(value).to_spec()

    @classmethod
    def parse_frozen(cls, value):
        serialized = canonical(value)
        parsed = json.loads(serialized)
        return cls._validated_frozen(parsed, serialized)

    @classmethod
    def parse_frozen_json(cls, serialized):
        """Parse already serialized worker input without re-encoding it."""
        if not isinstance(serialized, str):
            raise ValueError('invalid_job_spec')
        parsed = json.loads(serialized)
        return cls._validated_frozen(parsed, serialized)

    @staticmethod
    def _validated_frozen(value, serialized):
        if not isinstance(value, dict):
            raise ValueError('invalid_job_spec')
        if set(value) != {'protocol_version', 'executor', 'parameters', 'inputs'}:
            raise ValueError('invalid_job_spec: unexpected or missing fields')
        if type(value['protocol_version']) is not int or value['protocol_version'] != PROTOCOL_VERSION:
            raise ValueError('unsupported_protocol_version')
        if value['executor'] not in EXECUTORS or not isinstance(value['parameters'], dict) or not isinstance(value['inputs'], dict):
            raise ValueError('invalid_job_spec')
        if (value['executor'] == 'geometric_raytrace'
                and len(serialized.encode('utf-8')) > MAX_RAYTRACE_SPEC_BYTES):
            raise ValueError('geometric_raytrace_request_exceeds_20_mib')
        if (value['executor'] == 'appearance_render'
                and len(serialized.encode('utf-8')) > MAX_APPEARANCE_SPEC_BYTES):
            raise ValueError('appearance_frozen_spec_exceeds_60_mb')
        return ParsedFrozenJobSpec(**value, canonical_json=serialized,
                                   fingerprint=hashlib.sha256(serialized.encode('utf-8')).hexdigest())

    @property
    def fingerprint(self):
        return hashlib.sha256(canonical(self.to_dict()).encode('utf-8')).hexdigest()

def freeze_spec(service, kind, parameters):
    """Capture every referenced design once; original paths are provenance only."""
    values = deepcopy(parameters)
    if kind == 'geometric_raytrace':
        if not isinstance(values, dict) or set(values) - {'request', 'deposition_artifact'} or 'request' not in values:
            raise ValueError('geometric_raytrace requires request and optional deposition_artifact')
        from coat_sim.core.ray_trace_job import freeze_raytrace_spec
        return freeze_raytrace_spec(values['request'], values.get('deposition_artifact'))
    if kind == 'film_optimization':
        if set(values) != {'request'}:
            raise ValueError('film_optimization requires exactly one FilmOptimizationRequest')
        from coat_sim.core.film_optimization_job import freeze_job_payload
        payload = freeze_job_payload(service, values['request'])
        return JobSpec(kind, {'payload': payload}, {}).to_dict()
    if kind == 'appearance_render':
        if set(values) != {'frozen'} or not isinstance(values.get('frozen'), dict):
            raise ValueError('appearance_render requires one frozen request envelope')
        # The shared adapter already captured the source asset and raw render
        # job. Preserve it byte-for-byte; rebuild only inside the worker.
        from coat_sim.core.appearance_compute import validate_frozen
        envelope = validate_frozen(service, values['frozen'])
        return JobSpec(kind, {'frozen': envelope}, {}).to_dict()
    if kind in ('process', 'process_recipe', 'mask_optimization'):
        if kind in ('process', 'process_recipe'):
            # Recipe jobs retain their actual per-step deposition maps for later
            # appearance workflows; this does not change the computed total field.
            values.setdefault('include_layer_fields', True)
        path = values['process_path']
        document = service.load('process', path)
        if document.issues:
            raise ValueError('dependency_error: ' + '; '.join(document.issues))
        snapshot = (deepcopy(path) if isinstance(path, dict) else
                    service.load_simulation_asset(document.path)['input_snapshot']
                    if Path(document.path).suffix.lower() == '.coatasset' else
                    service.freeze_process(document.data, base_dir=Path(document.path).parent))
        # Do not retain an editable file reference in the executable parameters.
        values['process_path'] = snapshot
        return JobSpec(kind, values, {'snapshot': snapshot}).to_dict()
    if kind == 'probe':
        return JobSpec(kind, values, {}).to_dict()
    records, copied = [], {}
    from coat_sim.core.design_files import design_extension
    def capture(design_kind, path):
        target = service._path(path)
        key = (design_kind, str(target).casefold())
        if key in copied:
            return copied[key]
        document = service.load(design_kind, target)
        if document.issues:
            raise ValueError('dependency_error: ' + '; '.join(document.issues))
        name = f'{len(records):04d}-{design_kind}{design_extension(design_kind)}'
        copied[key] = name
        record = {'kind': design_kind, 'path': name, 'data': deepcopy(document.data),
                  'original_path': str(target)}
        records.append(record)
        if design_kind == 'machine':
            for collection, child_kind in (('sources', 'source'), ('masks', 'mask')):
                for entry in record['data'][collection]:
                    entry['file'] = capture(child_kind, service.resolve_reference(target, entry['file']))
        record['sha256'] = hashlib.sha256(canonical(record['data']).encode('utf-8')).hexdigest()
        return name
    for field, design_kind in (('machine_path', 'machine'), ('substrate_path', 'substrate'), ('source_path', 'source')):
        if values.get(field):
            values[field] = capture(design_kind, values[field])
    if values.get('mask_paths'):
        values['mask_paths'] = [capture('mask', path) for path in values['mask_paths']]
    if kind == 'python_script':
        # The registered sandbox executor retains its OS sandbox; never execute code here.
        from coat_sim.agent.sandbox import _authorized_input
        import base64
        total = 0
        for index, path in enumerate(values.get('files') or []):
            target = _authorized_input(path)
            content = target.read_bytes()
            total += len(content)
            if total > 32*1024*1024: raise ValueError('脚本输入总量最多32MiB（既有sandbox契约）')
            records.append({'kind': 'attachment', 'path': f'{index}-{target.name}',
                            'data_base64': base64.b64encode(content).decode('ascii')})
    return JobSpec(kind, values, {'files': records}).to_dict()
