"""Independent, bounded reader of an explicitly supported plaintext TFD subset.

No vendor software is loaded and no formula or record is executed. Numerical
column semantics and ordering are explicit import choices, never inferred from
material names or optical constants. See docs/tfcalc-import-contract.md.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import Path
import re
import uuid

from .film_design import MAX_LAYERS, validate_design
from .plugins import PLUGIN_API_VERSION, PluginRegistry, PluginSpec

PLUGIN_NAME = "tfcalc_plaintext"
MAX_TFD_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 20000
MAX_LINE_LENGTH = 20000
_TAG = re.compile(r"^[A-Z][A-Z0-9_-]{0,63}$")


@dataclass(frozen=True)
class TfcalcLayer:
    number: int
    material: str
    values: tuple[float, float]
    extra_fields: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class TfcalcDocument:
    version: int
    layers: tuple[TfcalcLayer, ...]
    secondary_layers: tuple[TfcalcLayer, ...]
    formulas: tuple[str, ...]
    environment_records: tuple[tuple[str, ...], ...]
    report: tuple[str, ...]


def _positive(value, label):
    if isinstance(value, bool):
        raise ValueError(label + " 必须为有限正数")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(label + " 必须为有限正数") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(label + " 必须为有限正数")
    return number


def _integer(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise ValueError(label + " 必须为非负整数")
    return int(value)


def parse_tfcalc_text(text: str) -> TfcalcDocument:
    """Read VERSION 1 delimited records; preserve unsupported semantics in report."""
    if not isinstance(text, str):
        raise TypeError("TFCalc 输入必须是明文字符串")
    if len(text.encode("utf-8")) > MAX_TFD_BYTES or "\x00" in text:
        raise ValueError("TFCalc 明文超过 2 MiB 或含二进制 NUL")
    records = []
    for line_number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if len(line) > MAX_LINE_LENGTH or not line.endswith("*"):
            raise ValueError(f"第 {line_number} 行超限或记录未以 * 结束")
        fields = tuple(item.strip() for item in line[:-1].split("*"))
        if not _TAG.fullmatch(fields[0]):
            raise ValueError(f"第 {line_number} 行记录名无效")
        records.append((line_number, fields))
        if len(records) > MAX_RECORDS:
            raise ValueError("TFCalc 记录数量超限")
    if not records or records[0][1] != ("VERSION", "1"):
        raise ValueError("仅支持首记录 VERSION*1* 的明文 TFCalc 子集")
    if records[-1][1] != ("EOF",):
        raise ValueError("TFCalc 文件缺少末记录 EOF*")
    tables: list[list[TfcalcLayer]] = [[], []]
    counts: dict[int, int] = {}
    active = None
    formulas, environments = [], []
    ignored: Counter[str] = Counter()
    tail_fields = 0
    for line_number, fields in records[1:-1]:
        tag = fields[0]
        if tag in ("VERSION", "EOF"):
            raise ValueError(f"第 {line_number} 行重复 VERSION/EOF 记录")
        if tag in ("LAYERS", "LAYERS2"):
            active = 0 if tag == "LAYERS" else 1
            if active in counts or len(fields) != 2:
                raise ValueError(f"第 {line_number} 行重复或无效 {tag}")
            count = _integer(fields[1], tag)
            if count > MAX_LAYERS:
                raise ValueError("TFCalc 层数超过项目上限")
            counts[active] = count
        elif tag in ("LAYER", "LAYER2"):
            if active is None or len(fields) < 5:
                raise ValueError(f"第 {line_number} 行层记录缺少声明或字段")
            if tag == "LAYER2" and active != 1:
                raise ValueError(f"第 {line_number} 行 LAYER2 不在第二层表")
            number = _integer(fields[1], "层号")
            if number != len(tables[active]) + 1 or number > counts[active]:
                raise ValueError(f"第 {line_number} 行层号不连续或超过声明层数")
            alias = fields[2]
            if not alias or len(alias) > 128:
                raise ValueError(f"第 {line_number} 行材料名称引用为空或超限")
            values = (_positive(fields[3], f"第 {line_number} 行第3字段"),
                      _positive(fields[4], f"第 {line_number} 行第4字段"))
            tables[active].append(TfcalcLayer(number, alias, values, fields[5:], line_number))
            tail_fields += len(fields[5:])
        elif tag in ("FORMULA", "FORMULA2"):
            formulas.append("*".join(fields) + "*")
        elif tag in ("ENVIRON", "ENVIRON2", "ENVIRON3", "ENVIRN"):
            environments.append(fields)
        else:
            ignored[tag] += 1
    if 0 not in counts:
        raise ValueError("TFCalc 文件缺少 LAYERS 声明")
    for index, count in counts.items():
        if count != len(tables[index]):
            raise ValueError("TFCalc 声明层数与实际层记录不一致")
    if sum(map(len, tables)) > MAX_LAYERS:
        raise ValueError("TFCalc 双层表合计超过项目层数上限")
    report = ["只导入已保存的显式层表；原始表达式仅作参考，不执行。",
              "不从名称引用补造 n/k；每个材料必须显式选择项目材料与数据集。",
              "环境、目标、绘图、优化及尾部 flag 未解释，不导入模拟设置。"]
    if environments:
        report.append(f"保留 {len(environments)} 条原始环境记录供核对。")
    if tail_fields:
        report.append(f"未解释层记录尾部字段：{tail_fields} 个。")
    if ignored:
        report.append("未解释记录：" + "、".join(f"{tag}×{count}" for tag, count in sorted(ignored.items())))
    return TfcalcDocument(1, tuple(tables[0]), tuple(tables[1]), tuple(formulas),
                          tuple(environments), tuple(report))


def register_tfcalc_plugin(registry: PluginRegistry) -> None:
    """Explicit host registration; never scans directories or entry points."""
    registry.register(PluginSpec("film_importer", PLUGIN_NAME, PLUGIN_API_VERSION,
                                parse_tfcalc_text, "TFCalc VERSION 1 明文层表（受限子集）"),
                      trusted=True)


def read_tfcalc_file(path: Path, registry: PluginRegistry) -> TfcalcDocument:
    if path.suffix.lower() != ".tfd":
        raise ValueError("请选择 .tfd 明文文件")
    with path.open("rb") as source:
        data = source.read(MAX_TFD_BYTES + 1)
    if len(data) > MAX_TFD_BYTES:
        raise ValueError("TFCalc 明文超过 2 MiB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("仅支持 UTF-8/ASCII 明文；未自动猜测文件编码") from exc
    return registry.invoke("film_importer", PLUGIN_NAME, text)


def build_tfcalc_design(document: TfcalcDocument, options: Mapping,
                        base_design: Mapping, material_options) -> dict:
    """Build an isolated candidate after explicit semantic and dataset mapping."""
    if not isinstance(document, TfcalcDocument) or not isinstance(options, Mapping):
        raise ValueError("导入需要有效层表和显式映射对象")
    allowed = {"source_table", "thickness_field", "thickness_unit", "source_order",
               "target_side", "reference_wavelength_nm", "preserve_environment_confirmed", "mapping"}
    if set(options) - allowed:
        raise ValueError("未知 TFCalc 导入选项")
    table = options.get("source_table")
    if table not in ("primary", "secondary"):
        raise ValueError("请显式选择原始层表")
    layers = document.layers if table == "primary" else document.secondary_layers
    if not layers:
        raise ValueError("所选原始层表为空")
    field = options.get("thickness_field")
    unit, order, side = (options.get(key) for key in ("thickness_unit", "source_order", "target_side"))
    if type(field) is not int or field not in (3, 4) or unit not in ("nm", "um"):
        raise ValueError("请明确物理厚度字段（3/4）及单位（nm/um）")
    if order not in ("substrate_outward", "incident_first") or side not in ("front", "back"):
        raise ValueError("请明确源层序及导入目标面")
    if options.get("preserve_environment_confirmed") is not True:
        raise ValueError("须确认沿用当前基片、介质、入射面及计算设置")
    result = validate_design(deepcopy(dict(base_design)))
    if side == "back" and result["substrate"]["sides"] != "double":
        raise ValueError("请先将当前基片设为双面，再导入背面")
    reference = _positive(options.get("reference_wavelength_nm"), "参考波长 nm")
    mapping = options.get("mapping")
    aliases = {layer.material for layer in layers}
    if not isinstance(mapping, Mapping) or set(mapping) != aliases:
        raise ValueError("必须逐一显式映射所选层表全部材料别名")
    catalog = {row["id"]: row for row in material_options}
    for alias, choice in mapping.items():
        if not isinstance(choice, Mapping) or set(choice) != {"material_id", "optical_set_id"}:
            raise ValueError(f"材料 {alias} 需要 material_id 和 optical_set_id")
        material = catalog.get(choice["material_id"])
        if not material or choice["optical_set_id"] not in {item["id"] for item in material.get("optical_sets", [])}:
            raise ValueError(f"材料 {alias} 的项目材料/所属光学数据集无效")
    ordered = layers if order == "substrate_outward" else tuple(reversed(layers))
    physical_nm = [row.values[field - 3] * (1000.0 if unit == "um" else 1.0) for row in ordered]
    try:
        finite_total = math.isfinite(math.fsum(physical_nm))
    except OverflowError:
        finite_total = False
    if not all(math.isfinite(value) and value > 0 for value in physical_nm) or not finite_total:
        raise ValueError("所选物理厚度换算为 nm 或总厚度超出有限数值范围")
    result[side + "_layers"] = [
        {"id": uuid.uuid4().hex, **dict(mapping[layer.material]),
         "thickness": {"value": layer.values[field - 3], "unit": unit},
         "note": f"TFCalc 原始层号 {layer.number}；名称引用 {layer.material}"}
        for layer in ordered]
    result["reference_wavelength_nm"] = reference
    # An import of one face must not discard an unrelated expression draft.
    # Neither the old draft nor the source formula rebuilds the imported table.
    result["expression"]["in_sync"] = False
    note = "TFCalc 明文层表导入；物理厚度字段 " + str(field) + "，单位 " + unit + "; 源层序 " + order
    if document.formulas:
        note += "\n原始表达式（只作参考，不执行）：\n" + "\n".join(document.formulas)
    result["note"] = (result["note"] + "\n" + note).strip()
    return validate_design(result)
