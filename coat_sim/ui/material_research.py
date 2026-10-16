"""Read-only presentation of material research, without inferring physics.

Research records are immutable provenance snapshots. This module formats every
property, source and unresolved gap; no record is converted into a coefficient.
"""
from collections import Counter

from .source_reference import PROPERTY_LABELS as REFERENCE_LABELS, display_value

ROLE_LABELS = {"film": "膜料", "substrate": "基片", "component": "组分",
               "element": "元素", "boat": "舟材"}
ROLE_LABELS["medium"] = "光学介质"
STATUS_LABELS = {
    "supported": "有来源支持", "literature_reference": "文献参考",
    "reference": "文献参考", "reported": "来源报告", "verified": "已核对来源",
    "conditional": "条件性参考", "estimated": "估计值（不可视为实测）",
    "conflict": "来源冲突", "conflicting": "来源冲突", "quarantined": "已隔离",
    "unknown": "未知，不能填零", "missing": "缺失，不能填零",
    "unavailable": "未取得", "not_found": "未找到可靠值",
    "not_applicable": "不适用", "source_only": "仅来源线索",
    "not_extracted": "尚未提取", "unverified": "未核实",
}
PROPERTY_LABELS = {
    **REFERENCE_LABELS,
    "density": "密度", "mass_density": "质量密度", "film_effective_density": "薄膜有效密度",
    "mass_thickness_density": "质量厚度换算密度", "packing_density": "堆积密度",
    "optically_inferred_packing_density_ratio": "光学反推堆积密度比", "optically_inferred_porosity": "光学反推孔隙率",
    "source_theoretical_density": "源料理论密度", "source_thermal_conductivity": "源料热导率",
    "source_linear_expansion": "源料线膨胀系数", "thermal_expansion_coefficient": "热膨胀系数",
    "linear_expansion": "线膨胀系数", "specific_heat": "比热容", "specific_heat_capacity": "比热容",
    "melting_temperature": "熔化温度", "thermo_optic_coefficient": "热光系数",
    "Young_modulus": "杨氏模量", "Poisson_ratio": "泊松比", "Knoop_hardness": "努氏硬度",
    "flexural_strength": "抗弯强度", "rupture_modulus": "断裂模量", "residual_tensile_stress": "残余拉应力",
    "absorption_coefficient": "吸收系数", "absorption_coefficient_alpha": "吸收系数 α",
    "absorption_coefficient_alpha_upper_bound": "吸收系数 α 上界", "internal_transmittance": "内透过率",
    "substrate_temperature_range": "基片温度范围", "substrate_temperature_upper_warning_range": "基片温度上限警戒范围",
    "substrate_C": "基片温度", "substrate_preheat_C": "基片预热温度", "anneal_C": "退火温度", "anneal_time_h": "退火时间",
    "compatibility_candidate": "材料兼容性候选", "electron_beam_candidate": "电子束容器候选",
    "resistance_candidate": "电阻蒸发容器候选", "alloying_embrittlement_risk": "合金化 / 脆化风险",
    "phase_chemistry_boundary": "相态 / 化学反应边界", "risk": "风险", "actual_user_grade": "实际使用牌号",
    "process_evidence_claim": "工艺证据说明", "rate_nm_s": "沉积速率", "thickness_nm": "膜厚",
    "AFM_RMS_nm": "AFM 表面粗糙度 RMS", "grain_nm": "晶粒尺寸", "F_to_Yb_atomic_ratio": "F / Yb 原子比",
    "stoichiometric_La_Ti_O_ratio": "La / Ti / O 化学计量比", "source_SnO2_wt_percent": "源料 SnO₂ 质量分数",
    "Tauc_gap_eV": "Tauc 带隙", "T_percent": "透过率", "FOM_ohm_inverse": "透明导电性能指标",
    "Rs_ohm_sq": "方阻", "Rs_ohm_sq_derived": "方阻（推导）", "rho_ohm_cm": "电阻率",
    "rho_ohm_cm_derived": "电阻率（推导）", "rho_text_ohm_cm": "正文电阻率",
    "mu_cm2_Vs": "载流子迁移率", "n_cm3": "载流子浓度", "O2_sccm": "氧气流量",
    "discharge_A": "放电电流", "discharge_V": "放电电压",
}
FIELD_LABELS = {
    "schema_version": "记录版本", "dataset_id": "数据集", "id": "记录 ID",
    "name": "名称", "formula": "化学式 / 牌号", "aliases": "别名",
    "description": "说明", "topics": "研究主题", "status": "状态",
    "notes": "限制 / 备注", "access_status": "访问状态", "locator": "原文定位",
    "local_path": "本地资料路径", "url": "原始链接", "title": "标题",
    "source_ids": "来源 ID", "conditions": "适用条件", "unit": "单位",
    "value": "数值 / 关系", "roles": "用途角色", "gaps": "缺口", "original_record": "原始记录（完整溯源）",
    "research_path": "研究资料路径", "research_text": "研究全文摘录", "source_coupling": "源与材料耦合条件",
    "apparatus_conditions": "装置条件", "electro_optics_evidence": "电学 / 光学证据", "response_contract": "适用范围约束",
    "reuse_audit_supplement": "复用审核补充",
}
WARNING_STATUSES = {"unknown", "missing", "unavailable", "not_found", "not_extracted",
                    "unverified", "conflict", "conflicting", "quarantined", "estimated"}


def role_label(roles):
    return " / ".join(ROLE_LABELS.get(role, role) for role in roles) if roles else "未限定（兼容材料）"


def coverage_label(coverage):
    """Status counts are coverage only, never a claim of calculation readiness."""
    if not coverage:
        return "尚无研究物性记录"
    parts = []
    for status, count in coverage.items():
        if count or status == "total":
            label = STATUS_LABELS.get(status, "合计" if status == "total" else status)
            parts.append(f"{label} {count} 项")
    return "；".join(parts)


def research_coverage(research):
    return dict(Counter(row.get("status", "unknown") for row in (research or {}).get("properties", [])))


def _value(value):
    if value == {} or value == [] or value == "":
        return "未给出"
    return display_value(value)


def _fields(record, exclude=()):
    return [FIELD_LABELS.get(key, key) + "：" + _value(value)
            for key, value in record.items() if key not in exclude]


def research_sections(research):
    if not research:
        return []
    sources = {row["id"]: row for row in research.get("sources", [])}
    summary_keys = {"schema_version", "dataset_id", "id", "name", "formula", "aliases", "description"}
    overview = _fields({key: value for key, value in research.items() if key in summary_keys})
    overview.append("用途角色：" + role_label(research.get("roles", [])))
    overview.append("研究主题：" + _value(research.get("topics", [])))
    overview.append("资料覆盖：" + coverage_label(research_coverage(research)))
    overview.append("研究数据仅供查阅。资料完整度不等于空间分布标定、热场或温度到沉积速率模型就绪；未知不是零。")
    sections = [{"title": "研究目录 · " + research.get("name", research.get("id", "")),
                 "body": "\n".join(overview), "source": "", "url": "", "warning": False}]
    for row in research.get("properties", []):
        status = row.get("status", "unknown")
        unit = str(row.get("unit") or "")
        unit = "（无量纲）" if unit == "1" else unit
        body = ["数值 / 关系：" + _value(row.get("value")) + " " + unit,
                "状态：" + STATUS_LABELS.get(status, status) + "（" + status + "）",
                "条件：" + _value(row.get("conditions")),
                "限制 / 备注：" + _value(row.get("notes"))]
        body.extend(_fields(row, {"id", "name", "value", "unit", "status", "conditions", "notes", "source_ids"}))
        for source_id in row.get("source_ids", []):
            source = sources.get(source_id, {})
            body.append("出处：" + source_id + " · " + source.get("title", "来源信息缺失"))
            if source.get("locator"):
                body.append("定位：" + _value(source["locator"]))
        if not row.get("source_ids"):
            body.append("出处：未取得可靠来源")
        sections.append({"title": "物性 · " + PROPERTY_LABELS.get(row.get("name"), row.get("name", row.get("id", ""))) + " · " + row.get("id", ""),
                         "body": "\n".join(body), "source": "、".join(row.get("source_ids", [])),
                         "url": "", "warning": status in WARNING_STATUSES})
    sections.append({"title": "研究缺口 / 尚不支持的物理（未知不是零）",
                     "body": _value(research.get("gaps", [])), "source": "", "url": "", "warning": True})
    for key, value in research.items():
        if key not in summary_keys | {"properties", "sources", "gaps", "roles", "topics"}:
            sections.append({"title": FIELD_LABELS.get(key, key), "body": _value(value),
                             "source": "", "url": "", "warning": False})
    for row in research.get("sources", []):
        sections.append({"title": "研究来源 " + row["id"] + " · " + row.get("title", ""),
                         "body": "\n".join(_fields(row, {"id", "title", "url"})),
                         "source": row["id"] + " · 打开原始来源", "url": row.get("url") or "",
                         "warning": row.get("access_status") in {"blocked", "unavailable", "not_accessed"}})
    return sections


def safe_source_url(url):
    """Only explicit web references can be opened from evidence snapshots."""
    from urllib.parse import urlsplit
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    return url if parsed.scheme.casefold() in {"http", "https"} and parsed.netloc else ""


def markdown_to_safe_html(markdown):
    """Small offline Markdown renderer; raw HTML and images are never executed.

    Supports headings, paragraphs, lists, code, links and pipe tables. Relative
    local-file links remain text, so embedded notes cannot launch a local file.
    """
    import html
    import re

    def inline(text):
        escaped = html.escape(text.replace("\x00", ""), quote=True)
        tokens = []
        def token(value):
            tokens.append(value)
            return f"\x00{len(tokens) - 1}\x00"
        escaped = re.sub(r"`([^`]+)`", lambda m: token("<code>" + m[1] + "</code>"), escaped)
        def link(match):
            title, url = match[1], html.unescape(match[2])
            return token('<a href="' + html.escape(url, quote=True) + '">' + title + '</a>') if safe_source_url(url) else title + " (" + html.escape(url) + ")"
        escaped = re.sub(r"!?\[([^\]]+)\]\(([^\s)]+)\)", link, escaped)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", escaped)
        escaped = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", escaped)
        return re.sub("\x00(\\d+)\x00", lambda m: tokens[int(m[1])], escaped)

    lines, out, paragraph = str(markdown).splitlines(), [], []
    i, in_code, code, list_kind = 0, False, [], None
    def flush():
        if paragraph:
            out.append("<p>" + "<br/>".join(inline(line) for line in paragraph) + "</p>")
            paragraph.clear()
    def close_list():
        nonlocal list_kind
        if list_kind:
            out.append("</" + list_kind + ">")
            list_kind = None
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush(); close_list()
            if in_code:
                out.append("<pre>" + html.escape("\n".join(code)) + "</pre>"); code = []
            in_code = not in_code
        elif in_code:
            code.append(line)
        elif i + 1 < len(lines) and "|" in line and re.fullmatch(r"[\s|:\-]+", lines[i + 1]) and "-" in lines[i + 1]:
            flush(); close_list()
            out.append('<table border="1" cellspacing="0" cellpadding="4">')
            def cells(value):
                return value.strip().strip("|").split("|")
            out.append("<tr>" + "".join("<th>" + inline(x.strip()) + "</th>" for x in cells(line)) + "</tr>")
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                out.append("<tr>" + "".join("<td>" + inline(x.strip()) + "</td>" for x in cells(lines[i])) + "</tr>")
                i += 1
            out.append("</table>")
            continue
        elif re.match(r"^#{1,6}\s", stripped):
            flush(); close_list()
            level = min(len(stripped) - len(stripped.lstrip("#")), 4)
            out.append(f"<h{level}>" + inline(stripped.lstrip("# ")) + f"</h{level}>")
        elif re.match(r"^(?:[-*+] |\d+[.)] )", stripped):
            flush()
            kind = "ol" if stripped[0].isdigit() else "ul"
            if list_kind != kind:
                close_list(); out.append("<" + kind + ">"); list_kind = kind
            out.append("<li>" + inline(re.sub(r"^(?:[-*+] |\d+[.)] )", "", stripped)) + "</li>")
        elif not stripped:
            flush(); close_list()
        else:
            close_list(); paragraph.append(line)
        i += 1
    flush(); close_list()
    if code:
        out.append("<pre>" + html.escape("\n".join(code)) + "</pre>")
    return "".join(out)


def prepare_reference_sections(reference_data=None, research_data=None):
    """Prepare bounded render units without Qt; call in a background worker.

    The underlying snapshots remain untouched. Long text is split into chunks,
    and the QML ListView creates only nearby sections rather than all widgets.
    """
    from .source_reference import reference_sections
    rows = reference_sections(reference_data) + research_sections(research_data)
    # Lead the details view with the readable local research document; retain
    # every structured property and source below it for complete traceability.
    rows.sort(key=lambda row: row.get("title") != FIELD_LABELS["research_text"])
    result = []
    for row in rows:
        body = str(row.get("body", ""))
        markdown = row.get("title") == FIELD_LABELS["research_text"]
        # Split at paragraph boundaries when possible, with a hard cap for long
        # original-record blocks. This bounds each delegate, not stored evidence.
        chunks = []
        while len(body) > 12000:
            split = body.rfind("\n\n", 0, 12000)
            if split < 1000:
                split = 12000
            chunks.append(body[:split]); body = body[split:].lstrip("\n")
        chunks.append(body)
        for index, chunk in enumerate(chunks):
            result.append(dict(row, title=row["title"] + (f" ({index + 1}/{len(chunks)})" if len(chunks) > 1 else ""),
                body=chunk, html=markdown_to_safe_html(chunk) if markdown else "",
                url=safe_source_url(row.get("url", ""))))
    return result
