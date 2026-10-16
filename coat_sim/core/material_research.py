"""Immutable, sourced research snapshots; intentionally no property-to-physics inference.

A record is evidence, not a calibrated source or a temperature/optical solver.
Unknown and quarantined values remain present and cannot enable a model.
"""
from collections.abc import Mapping
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path

ROLES = ("film", "substrate", "medium", "component", "element", "boat")
STATUSES = ("verified", "conditional", "unknown", "quarantined")
_RESOURCE = Path(__file__).with_name("resources") / "researched_materials.json"


def _canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@lru_cache(maxsize=1)
def _catalog_strings():
    catalog = json.loads(_RESOURCE.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != 1 or not catalog.get("dataset_id"):
        raise ValueError("研究材料目录版本或标识无效")
    records = {}
    for row in catalog["materials"]:
        identity = row["id"]
        if identity in records:
            raise ValueError("研究材料目录 id 重复")
        roles = row.get("roles", [])
        if not roles or any(role not in ROLES for role in roles) or len(set(roles)) != len(roles):
            raise ValueError("研究材料用途无效")
        sources = {source["id"] for source in row["sources"]}
        if len(sources) != len(row["sources"]):
            raise ValueError("研究材料来源 id 重复")
        props = set()
        for prop in row["properties"]:
            if prop["id"] in props or prop["status"] not in STATUSES:
                raise ValueError("研究材料属性 id 或状态无效")
            props.add(prop["id"])
            if set(prop["source_ids"]) - sources:
                raise ValueError("研究材料属性存在未解析来源")
            if prop["status"] in ("verified", "conditional") and not prop["source_ids"]:
                raise ValueError("可追溯属性必须提供来源")
            if prop["status"] == "unknown" and prop["value"] is not None:
                raise ValueError("未知物性须保留 null，而非零或推测值")
        snapshot = dict(row, schema_version=1, dataset_id=catalog["dataset_id"])
        records[identity] = _canonical(snapshot)
    return records


def research_material_records():
    return [json.loads(value) for value in _catalog_strings().values()]


def validate_research_data(data):
    if not isinstance(data, Mapping):
        raise ValueError("research_data 须为完整研究来源快照")
    try:
        supplied = _canonical(dict(data))
        expected = _catalog_strings().get(data.get("id"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("研究来源快照须为有限 JSON 数据") from exc
    if supplied != expected:
        raise ValueError("研究来源快照与受支持版本不一致；不能编辑或伪造来源物性")
    return deepcopy(dict(data))


def property_coverage(data):
    result = {status: 0 for status in STATUSES}
    for row in (data or {}).get("properties", []):
        result[row["status"]] += 1
    result["total"] = sum(result.values())
    return result
