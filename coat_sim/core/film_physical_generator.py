"""Form metadata and pure adapters for the existing physical structure core.

No thickness law or optical solver lives here. All public nm prescriptions are
produced by film_physical_structures from the current session material provider.
"""
from copy import deepcopy
import math
from typing import Any, Callable, Mapping

from coat_sim.core.film_physical_structures import (
    FilmBraggStackSection, FilmChirpedSection, FilmCoupledCavitySection,
    FilmDigitalRugateSection, FilmOpticalLayerSpec,
    generate_physical_structure, structure_spec_from_mapping, structure_spec_to_mapping,
    FilmStructureSpec, FilmStructureSection,
)

MECHANISMS = (
    {'id': 'multistack', 'title': '多膜堆与独立匹配层', 'section_type': 'bragg'},
    {'id': 'coupled_cavity', 'title': '多腔与独立耦合镜', 'section_type': 'coupled_cavity'},
    {'id': 'chirped', 'title': '局部 Bragg / H:L 比例啁啾', 'section_type': 'chirped'},
    {'id': 'digital_rugate', 'title': '真实 H/L 数字 rugate', 'section_type': 'digital_rugate'},
    {'id': 'composite', 'title': '自由组合：匹配、膜堆、腔与渐变', 'section_type': 'layer'},
)


def _field(name, label, kind='number', *, optional=False, choices=(), help_text=''):
    return {'name': name, 'label': label, 'kind': kind, 'optional': optional, 'help': help_text,
            'choices': [{'id': value, 'label': title} for value, title in choices]}


_ROLES = [_field('high_role', '高折射率膜料', 'role'),
          _field('low_role', '低折射率膜料', 'role')]
FIELDS = {
    'layer': [_field('role', '本层用料', 'role'), _field('wavelength_nm', '构造真空波长 (nm)'),
              _field('optical_thickness_waves', '光学厚度 n·d / λ（无量纲）', help_text='0.25 为四分之一波长，0.5 为半波长；使用上方构造波长与所选材料的 n。'),
              _field('thickness_nm', '独立物理厚度 (nm；留空用光学厚度)', optional=True)],
    'bragg': [_field('wavelength_nm', '膜堆中心波长 (nm)'), _field('pairs', '膜对数 N（整数）', 'integer'),
              *_ROLES, _field('first_role', '靠近基片的首层用料', 'role'),
              _field('high_low_optical_ratio', '高/低折射率层光学厚度比（无量纲）'),
              _field('thickness_scale', '膜对厚度缩放（无量纲）'),
              _field('terminal_role', '末端匹配层用料（可不加）', 'role', optional=True),
              _field('terminal_optical_thickness_waves', '末端层光学厚度 n·d / λ（无量纲）', help_text='仅选择末端匹配层时生效；0.25 为四分之一波长。')],
    'chirped': [_field('start_wavelength_nm', '起始局部 Bragg 波长 (nm)'),
                _field('end_wavelength_nm', '终止局部 Bragg 波长 (nm)'),
                _field('pairs', '膜对数（至少 2）', 'integer'), *_ROLES,
                _field('first_role', '靠近基片的首层用料', 'role'),
                _field('ratio_start', '起始高/低折射率层光学厚度比（无量纲）'),
                _field('ratio_end', '终止高/低折射率层光学厚度比（无量纲）'),
                _field('spacing', '局部 Bragg 插值', 'choice', choices=(('linear','线性'),('log','对数'))),
                _field('thickness_scale', '膜对厚度缩放（无量纲）')],
    'digital_rugate': [_field('wavelength_nm', '材料取值参考波长 (nm)'),
        _field('period_nm', '物理调制周期 (nm)'), _field('periods','周期数','integer'),
        _field('samples_per_period','每周期数字分片数（整数）','integer'), *_ROLES,
        _field('mean_n','平均折射率 n（无量纲；可留空）',optional=True, help_text='留空取高、低折射率材料 n 的中点；实际输出仍为两种真实材料的单层。'),
        _field('modulation_n','折射率调制幅度 Δn（无量纲；可留空）',optional=True, help_text='留空使用核心默认调幅；0 表示均匀目标，真实层仍按数字近似构造。'),
        _field('phase_rad','初相位 (rad)'),
        _field('apodization','端部渐消','choice',choices=(('hann','Hann'),('none','无渐消')))],
    'coupled_cavity': [],
}


def physical_material_use_label(role: str) -> str:
    return {'H': '高折射率膜料（H）', 'L': '低折射率膜料（L）',
            'C': '独立腔层用料（C）'}.get(role, '附加用料（' + str(role) + '）')


def physical_required_material_uses(sections) -> list[str]:
    """Collect used binding IDs; identifiers never imply distinct materials."""
    required: list[str] = []
    def visit(section):
        if not isinstance(section, Mapping):
            return
        for key in ('role', 'high_role', 'low_role', 'first_role', 'terminal_role'):
            role = section.get(key)
            if isinstance(role, str) and role and role not in required:
                required.append(role)
        for key in ('substrate_mirror', 'incident_mirror'):
            visit(section.get(key))
        for key in ('cavities', 'coupling_mirrors'):
            for child in section.get(key, ()):
                visit(child)
    for section in sections:
        visit(section)
    return required


def physical_section_fields(kind: str) -> list[dict[str, Any]]:
    if kind not in FIELDS:
        raise ValueError('未知物理结构段类型')
    return deepcopy(FIELDS[kind])


def physical_section_defaults(kind: str, wavelength_nm: float) -> dict[str, Any]:
    wave = float(wavelength_nm)
    if not math.isfinite(wave) or wave <= 0:
        raise ValueError('构造参考波长必须有限且为正')
    constructors: dict[str, Callable[[], FilmStructureSection]] = {
        'layer': lambda: FilmOpticalLayerSpec('L', wave),
        'bragg': lambda: FilmBraggStackSection(wave),
        'chirped': lambda: FilmChirpedSection(.9*wave, 1.1*wave),
        # This is an editable example period, not an index-based synthesis law.
        'digital_rugate': lambda: FilmDigitalRugateSection(wave, wave/4),
        'coupled_cavity': lambda: FilmCoupledCavitySection(
            FilmBraggStackSection(wave, first_role='L'),
            (FilmOpticalLayerSpec('H', wave, .5),), (),
            FilmBraggStackSection(wave, first_role='H')),
    }
    if kind not in constructors:
        raise ValueError('未知物理结构段类型')
    section = constructors[kind]()
    # Use the core serializer so nested section types have the same public keys.
    return structure_spec_to_mapping(FilmStructureSpec('composite', (), (section,)))['sections'][0]


def _number(value, label, *, optional=False, integer=False):
    if optional and (value is None or value == ''):
        return None
    if isinstance(value, bool):
        raise ValueError(f'{label} 必须是有效数字')
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f'{label} 必须是有效数字') from exc
    if not math.isfinite(number) or (integer and not number.is_integer()):
        raise ValueError(f'{label} 必须是有限' + ('整数' if integer else '数字'))
    return int(number) if integer else number


def _section(raw):
    if not isinstance(raw, Mapping) or raw.get('type') not in FIELDS:
        raise ValueError('结构段必须选择已支持的类型')
    value = deepcopy(dict(raw))
    if value['type'] == 'coupled_cavity':
        for key in ('substrate_mirror', 'incident_mirror'):
            value[key] = _section(value.get(key))
        for key in ('cavities', 'coupling_mirrors'):
            if not isinstance(value.get(key), (list, tuple)):
                raise ValueError(f'{key} 必须是结构列表')
            value[key] = [_section(row) for row in value[key]]
    else:
        for field in FIELDS[value['type']]:
            name = field['name']
            if name not in value:
                continue  # Core dataclass owns defaults and required-field checks.
            if field['kind'] in ('integer', 'number'):
                value[name] = _number(value[name], field['label'],
                    optional=field['optional'], integer=field['kind']=='integer')
            elif field['optional'] and value[name] == '':
                value[name] = None
    return value


def normalize_physical_form(inputs: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(inputs, Mapping):
        raise ValueError('物理结构参数必须为对象')
    data = deepcopy(dict(inputs))
    if not isinstance(data.get('sections'), (list, tuple)):
        raise ValueError('请添加至少一个物理结构段')
    data['sections'] = [_section(row) for row in data['sections']]
    return structure_spec_to_mapping(structure_spec_from_mapping(data))


def physical_material_snapshot(material_options, references):
    """Capture only selected optical records; labels are not physical facts."""
    rows: dict[str, dict[str, Any]] = {}
    for material_id, optical_id in sorted(references):
        matches = [(row, optical) for purpose in ('film', 'substrate', 'medium')
            for row in material_options.get(purpose, ()) if row.get('id') == material_id
            for optical in row.get('optical_sets', ()) if optical.get('id') == optical_id]
        if not matches:
            raise ValueError(f'当前工程缺少材料/数据集：{material_id}/{optical_id}')
        material, optical = matches[0]
        def physical(record):
            return {key: deepcopy(value) for key, value in record.items()
                    if key not in ('name', 'notes', 'note', 'source', 'description')}
        if any(physical(other) != physical(optical) for _, other in matches[1:]):
            raise ValueError(f'当前工程同 ID 数据集冲突：{material_id}/{optical_id}')
        row = rows.setdefault(material_id, {'id': material_id, 'name': material.get('name', material_id),
            'design_ready': material.get('design_ready'), 'optical_sets': []})
        row['optical_sets'].append(deepcopy(optical))
    return {'materials': list(rows.values())}


def physical_materials_equal(left, right):
    def facts(value):
        if isinstance(value, Mapping):
            return {key: facts(item) for key, item in value.items()
                    if key not in ('name', 'notes', 'note', 'source', 'description')}
        if isinstance(value, (list, tuple)):
            return [facts(item) for item in value]
        return value
    return facts(left) == facts(right)


def build_physical_structure_preview(*, inputs, base_design, material_options):
    """Adapt the session environment/catalog to the unchanged physical core."""
    data = normalize_physical_form(inputs)
    spec = structure_spec_from_mapping(data)
    environment = deepcopy(dict(base_design))
    if environment['back_layers'] or environment['substrate']['sides'] != 'single' or environment['first_surface'] != 'front':
        raise ValueError('当前物理结构生成仅支持单面、正面入射且无背膜；请先显式调整设计环境。')
    if not environment['use_k'] or environment['assume_lossless']:
        raise ValueError('物理结构生成要求当前设计 use_k=true、assume_lossless=false；未知 k 不回退无损。')
    # Only the front prescription is being replaced. No environment field changes.
    environment['front_layers'] = []
    refs = {(item.material_id, item.optical_set_id) for item in spec.bindings}
    refs.add((environment['substrate']['material_id'], environment['substrate']['optical_set_id']))
    if environment['incident_medium']['kind'] == 'material':
        medium = environment['incident_medium']
        refs.add((medium['material_id'], medium['optical_set_id']))
    snapshot = physical_material_snapshot(material_options, refs)
    result = generate_physical_structure(environment, spec, snapshot)
    candidate = dict(result.candidate_design)
    candidate['name'] = base_design['name']
    metadata = result.metadata
    titles = {row['id']:row['title'] for row in MECHANISMS}
    return {'candidate_design':candidate, '_physical_material_snapshot': snapshot, 'mechanism':spec.mechanism,
        'title':titles[spec.mechanism], 'parameters':data,
        'layer_count':metadata['layer_count'],
        'total_physical_thickness_nm':metadata['total_thickness_nm'],
        'layer_order':'substrate_outward',
        'layers_substrate_order':deepcopy(metadata['layers']),
        'material_samples':deepcopy(metadata['material_samples']),
        'limitations':('结构种子，不保证光谱达标或可制造。只支持 R/T 验收；phase/GD/GDD 未支持。'
                       '数字 rugate 是实际 H/L 层，薄 cell 等效仅为近似。'),
        'references':deepcopy(metadata['references'])}


def freeze_synthesis_editable_snapshot(service, source_design, active_task_snapshot):
    """Capture the editable closure at submission without evaluating inactive n/k.

    The numerical job keeps its original active snapshot. This extra GUI-owned
    snapshot is used only when explicitly applying or saving the candidate.
    Full provider records preserve dataset membership and optional metadata.
    source_design is the prospective editable skeleton (environment plus the
    retained workflow), excluding old layers/symbols that N=0 discards.
    """
    from .film_process_import import _sha256, validate_frozen_design_snapshot
    from .project_dependencies import document_material_references, normalize_optical_material
    # The numerical task intentionally omits inactive references. Validate its
    # transport digest now and its complete editable shape after raw capture.
    required = {'schema_version', 'kind', 'design', 'settings', 'materials', 'sha256'}
    if not isinstance(active_task_snapshot, Mapping) or set(active_task_snapshot) != required:
        raise ValueError('合成任务冻结快照字段无效')
    active = deepcopy(dict(active_task_snapshot))
    if active['sha256'] != _sha256(active):
        raise ValueError('合成任务冻结快照摘要不匹配')
    if not isinstance(active['materials'], list):
        raise ValueError('合成任务冻结材料必须为数组')
    provider = service.materials
    references = set(document_material_references('film', dict(source_design)))
    references.update((row['id'], optical['id']) for row in active['materials']
                      for optical in row['optical_sets'])
    records = {}
    for material_id, optical_id in sorted(references):
        if material_id not in records:
            raw = provider.get(material_id)
            record = {key: deepcopy(value) for key, value in raw.items()
                      if key in {'id', 'name', 'notes', 'optical_sets', 'design_ready'}}
            records[material_id] = normalize_optical_material(record)
        if optical_id not in {row['id'] for row in records[material_id]['optical_sets']}:
            raise ValueError('提交时缺少可编辑材料数据集：' + material_id + '/' + optical_id)
    for row in active['materials']:
        captured = records[row['id']]
        if row.get('design_ready') != captured.get('design_ready'):
            raise ValueError('提交时材料就绪状态已改变：' + row['id'])
        available = {optical['id']: optical for optical in captured['optical_sets']}
        for optical in row['optical_sets']:
            if not physical_materials_equal(optical, available[optical['id']]):
                raise ValueError('提交时材料数据已改变：' + row['id'] + '/' + optical['id'])
    if provider is not service.materials:
        raise ValueError('提交时材料来源已改变，请重新准备任务。')
    active['materials'] = list(records.values())
    active['sha256'] = _sha256(active)
    return validate_frozen_design_snapshot(active)
