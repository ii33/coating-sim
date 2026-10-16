"""Export a completed deposition result and its input snapshot to a PDF."""

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime
import math
import re
from pathlib import Path
import tempfile
import unicodedata

import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
from matplotlib.font_manager import FontProperties, fontManager

from .atomic_io import replace_with_retry


_FONT_CANDIDATES = ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                    "Noto Sans CJK SC", "Noto Sans CJK JP", "WenQuanYi Micro Hei", "SimHei")
DEFAULT_SECTIONS = ("heatmap", "thickness", "uniformity")
AVAILABLE_SECTIONS = frozenset((*DEFAULT_SECTIONS, "computation", "settings", "machine", "process", "layers"))


def suggested_pdf_filename(result, process=None):
    """A short portable save suggestion from the displayed immutable result.

    This only summarizes existing values; it never derives new result metrics.
    Keep the full filename below 180 UTF-8 bytes for cross-platform portability.
    """
    result = result if isinstance(result, Mapping) else {}
    process = process if isinstance(process, Mapping) else {}
    profile = process.get("profile", process)
    profile = profile if isinstance(profile, Mapping) else {}
    title = unicodedata.normalize("NFKC", str(profile.get("name") or ""))
    title = "".join(c for c in title if unicodedata.category(c) not in ("Cc", "Cf"))
    title = re.sub(r'[<>:"/\\|?*]', " ", title)
    title = re.sub(r"\s+", " ", title).strip(" .")
    if not title or re.search(r"[0-9a-f]{16,}", title, re.I):
        title = "工艺仿真" if "field_nm" in result or profile else "单源预览"
    if title.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
            prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"}:
        title = "膜厚-" + title
    title = title[:32].rstrip(" .")
    count = len(profile.get("steps") or result.get("step_results") or [])
    parts = [title]
    if count:
        parts.append(f"{count}层")
    target = result.get("total_target_nm")
    if isinstance(target, (int, float, np.number)) and math.isfinite(float(target)) and target > 0:
        parts.append(f"{float(target):.4g}nm")
    parts.append("膜厚报告")
    name = "_".join(parts)
    while len(name.encode("utf-8")) > 176:
        name = name[:-1]
    return name.rstrip(" .") + ".pdf"


def report_sections(sections=None):
    selected = tuple(dict.fromkeys(DEFAULT_SECTIONS if sections is None else sections))
    if not selected or any(item not in AVAILABLE_SECTIONS for item in selected):
        raise ValueError("PDF 至少选择一项：heatmap/thickness/uniformity/computation/settings/machine/process/layers")
    return selected


def _font():
    available = {entry.name for entry in fontManager.ttflist}
    family = next((name for name in _FONT_CANDIDATES if name in available),
                  "DejaVu Sans")
    return FontProperties(family=[family, "DejaVu Sans"])


def _plain(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, np.ndarray):
        return _plain(value.tolist())
    if isinstance(value, np.generic):
        return _plain(value.item())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _display_width(text):
    return sum(2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
               for char in text)


def _wrap_line(line, width=94):
    """Wrap detail lines by approximate printed glyph width, including CJK."""
    if not line:
        return [""]
    indent = len(line) - len(line.lstrip())
    prefix = " " * min(indent + 2, 16)
    chunks = []
    current = ""
    for char in line:
        if current and _display_width(current + char) > width:
            chunks.append(current)
            current = prefix
        current += char
    if current:
        chunks.append(current)
    return chunks


def _f(value, suffix="", digits=3):
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def _summary(result, elapsed_s, requested_device, selected=DEFAULT_SECTIONS):
    lines = [
        f"请求设备：{requested_device}",
        f"实际设备：{result.get('device', '—')}",
        f"计算用时：{_f(elapsed_s, ' s', 2)}",
    ] if "computation" in selected else []
    if "field_nm" in result and "thickness" in selected:
        lines.extend((
            f"总目标厚度：{_f(result.get('total_target_nm'), ' nm')}",
            f"平均膜厚：{_f(result.get('mean_nm'), ' nm')}",
            f"最小膜厚：{_f(result.get('min_nm'), ' nm')}",
            f"最大膜厚：{_f(result.get('max_nm'), ' nm')}",
        ))
    if "uniformity" in selected:
        lines.extend((
            f"膜厚 CV：{_f(result.get('cv_percent'), '%')}",
            f"膜厚峰谷差：{_f(result.get('pv_nm'), ' nm')}",
            f"峰谷差 / 均值：{_f(result.get('pv_percent'), '%')}",
        ))
    notices = result.get("backend_notices", [])
    if not notices and result.get("backend_notice"):
        notices = [result["backend_notice"]]
    for notice in notices if "computation" in selected else []:
        if notice:
            lines.append(f"设备说明：{notice}")
    return lines


def _figure_with_plot(result, elapsed_s, requested_device, font, generated_at,
                      selected=DEFAULT_SECTIONS, has_details=False):
    process_mode = "field_nm" in result
    field = np.asarray(result["field_nm" if process_mode else "field"], dtype=float)
    x = np.asarray(result["x"], dtype=float)
    y = np.asarray(result.get("y", x), dtype=float)
    if field.ndim != 2 or len(x) != field.shape[1] or len(y) != field.shape[0]:
        raise ValueError("仿真结果的网格坐标与厚度数组形状不一致")
    if len(x) < 2 or len(y) < 2 or not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError("仿真结果的网格坐标无效")

    fig = Figure(figsize=(8.27, 11.69), dpi=120, facecolor="white")
    fig.text(0.07, 0.955, "沉积膜厚仿真结果", fontsize=18, fontproperties=font,
             weight="bold", color="#172554")
    fig.text(0.07, 0.925,
             f"{'工艺仿真' if process_mode else '单源预览'}  ·  {generated_at}",
             fontsize=9.5, fontproperties=font, color="#475569")
    axis = fig.add_axes([0.09, 0.31, 0.72, 0.55])
    image = axis.imshow(np.ma.masked_invalid(field), origin="lower",
                        extent=(x[0], x[-1], y[0], y[-1]),
                        cmap="turbo", interpolation="bilinear", aspect="equal")
    axis.set_xlabel("X / mm")
    axis.set_ylabel("Y / mm")
    axis.set_title("膜厚分布", fontproperties=font, fontsize=12)
    color_axis = fig.add_axes([0.85, 0.37, 0.025, 0.43])
    colorbar = fig.colorbar(image, cax=color_axis)
    colorbar.set_label("膜厚 / nm" if process_mode else "相对膜厚 / 平均值",
                       fontproperties=font)

    summary_lines = []
    for item in _summary(result, elapsed_s, requested_device, selected):
        summary_lines.extend(_wrap_line(item, 95))
    for i, line in enumerate(summary_lines[:11]):
        fig.text(0.08, 0.265 - i * 0.019, line, fontsize=9,
                 fontproperties=font, color="#334155")
    if has_details:
        fig.text(0.07, 0.018, "所选参数及计算信息见后续页面。", fontsize=8,
                 fontproperties=font, color="#64748b")
    fig.text(0.93, 0.018, "1", fontsize=8, ha="right", color="#64748b")
    return fig

def _detail_sections(result, settings, machine, process, elapsed_s,
                     requested_device, generated_at, selected=DEFAULT_SECTIONS):
    computation = {
        "exported_at": generated_at,
        "simulation_mode": "process" if "field_nm" in result else "single_source_preview",
        "requested_device": requested_device,
        "actual_device": result.get("device"),
        "elapsed_s": elapsed_s,
        "backend_notice": result.get("backend_notice"),
        "backend_notices": result.get("backend_notices"),
        "time_steps": result.get("time_steps"),
        "time_step_s": result.get("time_step_s"),
        "total_duration_s": result.get("total_duration_s"),
    }
    if "field_nm" in result:
        computation.update({
            "algorithm": result.get("algorithm"),
            "fast_cycle_residual_percent": result.get("fast_cycle_residual_percent"),
            "computed_time_steps": result.get("computed_time_steps"),
            "return_carrier_revolutions": result.get("return_carrier_revolutions"),
            "return_cycle_s": result.get("return_cycle_s"),
        })
    if computation["backend_notices"]:
        computation.pop("backend_notice")
    # Report thickness and execution metrics only. A substrate's shape remains
    # part of its input parameters, but no shape or final-surface result is
    # rendered or summarized in this film-thickness report.
    thickness_keys = (
        "total_target_nm", "mean_nm", "min_nm", "max_nm", "pv_nm",
        "cv_percent", "pv_percent", "time_batch_steps", "batch_elements_limit",
    )
    statistics = {key: result[key] for key in thickness_keys if key in result}
    settings_snapshot = _plain(settings)
    if isinstance(settings_snapshot, dict):
        settings_snapshot.pop("substrate_surface", None)
        settings_snapshot.pop("substrate_profile", None)
        if machine is not None:
            for key in (
                "sun_teeth", "planet_teeth", "planet_offset_mm", "gear_module_mm",
                "carrier_x_mm", "carrier_y_mm", "substrate_radius_mm",
                "substrate_z_mm", "source_x_mm", "source_y_mm",
                "source_exponent",
            ):
                settings_snapshot.pop(key, None)
    sections = []
    if "computation" in selected:
        sections.append(("计算信息", computation))
    if "thickness" in selected:
        sections.append(("膜厚统计", {k: v for k, v in statistics.items()
                                    if k in ("total_target_nm", "mean_nm", "min_nm", "max_nm")}))
    if "uniformity" in selected:
        sections.append(("均匀性", {k: v for k, v in statistics.items()
                                 if k in ("pv_nm", "cv_percent", "pv_percent")}))
    if "settings" in selected:
        sections.append(("计算设置", settings_snapshot))
    if machine is not None and "machine" in selected:
        machine_snapshot = _plain(machine)
        if isinstance(machine_snapshot, dict):
            substrate = machine_snapshot.pop("substrate_profile", None)
            machine_snapshot.pop("substrate_radius_mm", None)
            if substrate is not None:
                sections.append(("基片配置", substrate))
        sections.append(("机器结构", machine_snapshot))
    if process is not None and "process" in selected:
        process_parameters = process.get("profile", process) if isinstance(process, Mapping) else process
        process_parameters = dict(process_parameters) if isinstance(process_parameters, Mapping) else process_parameters
        if isinstance(process_parameters, dict) and "machine" in process_parameters \
                and machine is not None:
            process_parameters = {key: value for key, value in process_parameters.items()
                                  if key != "machine"}
        if isinstance(process_parameters, dict):
            process_parameters = {key: value for key, value in process_parameters.items()
                                  if key not in ("steps", "profile", "name", "schema_version")
                                  and value not in (None, "")}
        if process_parameters:
            sections.append(("工艺方案", process_parameters))
    if result.get("step_results") is not None and "layers" in selected:
        sections.append(("各层结果", [{k: v for k, v in item.items() if k != "effective_settings"}
                                     for item in result["step_results"]]))
    return sections


_FIELD_NAMES = {
    "schema_version": "文件格式版本", "name": "名称", "kind": "类型",
    "file": "文件", "machine_file": "机器文件", "substrate_file": "基片文件",
    "sources": "蒸发源", "masks": "修正板", "monitors": "监测器",
    "steps": "层", "step_results": "层结果", "profile": "配置",
    "substrate_profile": "基片配置", "surface": "表面", "outline": "轮廓",
    "outer": "外轮廓", "holes": "孔洞", "points_mm": "顶点",
    "control_points_mm": "曲线控制点", "x_mm": "X / mm", "y_mm": "Y / mm",
    "z_mm": "Z / mm", "width_mm": "宽度 / mm", "height_mm": "高度 / mm",
    "radius_mm": "半径 / mm", "conic_k": "圆锥常数 k",
    "source_slot": "蒸发源位次", "monitor_slot": "监测器位次",
    "source_name": "蒸发源名称", "monitor_name": "监测器名称",
    "target_nm": "目标厚度 / nm", "target_mode": "厚度基准",
    "rate_nm_s": "沉积速率 / nm/s", "duration_s": "沉积时长 / s",
    "carrier_x_mm": "载架中心 X / mm", "carrier_y_mm": "载架中心 Y / mm",
    "planet_offset_mm": "行星盘轨道半径 / mm",
    "substrate_z_mm": "基片中心高度 / mm",
    "sun_teeth": "太阳轮齿数", "planet_teeth": "行星轮齿数",
    "gear_module_mm": "齿轮模数 / mm", "carrier_rpm": "大盘转速 / rpm",
    "carrier_phase_deg": "大盘初相 / °", "planet_phase_deg": "行星盘初相 / °",
    "source_x_mm": "源中心 X / mm", "source_y_mm": "源中心 Y / mm",
    "source_exponent": "源角指数", "substrate_radius_mm": "基片计算半径 / mm",
    "grid_size": "网格边长", "time_steps": "时间步数",
    "time_step_s": "时间步长 / s", "total_duration_s": "总沉积时长 / s",
    "computed_time_steps": "实际执行采样数", "algorithm": "沉积算法",
    "fast_cycle_residual_percent": "快速算法残余阈值 / %",
    "return_carrier_revolutions": "材料点回归公转圈数",
    "return_cycle_s": "材料点回归周期 / s",
    "residual_s": "周期残余时长 / s",
    "residual_fraction": "周期残余比例 / 0–1",
    "simulated_duration_s": "实际积分时长 / s",
    "fast_cycle_eligible": "满足快速算法条件",
    "reused_cycle_field": "复用同源周期通量场",
    "sampling_capped": "采样触及上限",
    "exported_at": "导出时间", "simulation_mode": "计算模式",
    "requested_device": "请求设备", "actual_device": "实际设备",
    "elapsed_s": "计算用时 / s", "backend_notice": "设备说明",
    "backend_notices": "设备说明", "total_target_nm": "总目标厚度 / nm",
    "mean_nm": "平均膜厚 / nm", "min_nm": "最小膜厚 / nm",
    "max_nm": "最大膜厚 / nm", "pv_nm": "膜厚峰谷差 / nm",
    "cv_percent": "膜厚 CV / %", "pv_percent": "膜厚峰谷差 / 均值 / %",
}


def _value_text(value):
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.9g}" if math.isfinite(value) else str(value)
    return str(value)


def _key_value_rows(value, prefix=""):
    """Flatten nested snapshots into readable label/value rows without JSON syntax."""
    value = _plain(value)
    if isinstance(value, dict):
        if not value:
            yield prefix or "值", "无"
        for key, child in value.items():
            label = _FIELD_NAMES.get(key, key)
            next_prefix = f"{prefix} / {label}" if prefix else label
            if key == "algorithm":
                child = {"fast_cycle": "快速算法", "precise": "精确算法",
                         "cycle": "完整回归周期", "time": "真实时长"}.get(child, child)
            yield from _key_value_rows(child, next_prefix)
    elif isinstance(value, list):
        if not value:
            yield prefix or "值", "无"
        elif all(not isinstance(item, (dict, list)) for item in value):
            yield prefix or "值", "、".join(_value_text(item) for item in value)
        elif all(isinstance(item, list) and len(item) == 2 and
                 all(isinstance(number, (int, float)) for number in item)
                 for item in value):
            for index, point in enumerate(value, 1):
                yield f"{prefix} / 点 {index}", f"{_value_text(point[0])}, {_value_text(point[1])} mm"
        else:
            for index, child in enumerate(value, 1):
                yield from _key_value_rows(child, f"{prefix} / 第 {index} 项")
    else:
        yield prefix or "值", _value_text(value)


def _table_number(value):
    try:
        number = float(value)
        return f"{number:.6g}" if math.isfinite(number) else "—"
    except (TypeError, ValueError):
        return "—"


def _process_layer_rows(process, machine=None, result=None):
    """Presentation-only layer table, preserving the recipe's persisted order."""
    process = process if isinstance(process, Mapping) else {}
    profile = process.get("profile", process)
    profile = profile if isinstance(profile, Mapping) else {}
    machine = machine if isinstance(machine, Mapping) else process.get("machine", {})
    machine = machine if isinstance(machine, Mapping) else {}
    sources = machine.get("sources", [])
    results = (result or {}).get("step_results") or []
    modes = {"mean": "整片平均", "center": "基片中心", "monitor": "监测器"}
    rows = []
    for index, step in enumerate(profile.get("steps", []), 1):
        completed = results[index - 1] if index <= len(results) else {}
        slot = step.get("source_slot", completed.get("source_slot"))
        source = sources[slot - 1] if isinstance(slot, int) and 0 < slot <= len(sources) else {}
        source_profile = source.get("profile") or {}
        source_name = source_profile.get("name") or completed.get("source_name")
        if not source_name:
            source_name = Path(str(source.get("file") or source.get("path") or "")).stem
        source_text = f"{slot or '-'} · {source_name}" if source_name else f"源 {slot or '-'}"
        material = source_profile.get("material") or {}
        material_text = material.get("name") or material.get("formula") or material.get("id") or "未指定"
        mode = modes.get(step.get("target_mode"), str(step.get("target_mode") or "-"))
        if step.get("target_mode") == "monitor":
            mode += f" {step.get('monitor_slot', '-')}"
        duration = completed.get("duration_s")
        if duration is None:
            try:
                duration = float(step["target_nm"]) / float(step["rate_nm_s"])
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                duration = None
        rows.append([str(index), source_text, str(material_text),
                     _table_number(step.get("target_nm")), _table_number(step.get("rate_nm_s")),
                     mode, _table_number(duration)])
    return rows


def _table_cell(value, width):
    lines = _wrap_line(str(value).replace("\n", " "), width)
    if len(lines) > 4:
        lines = lines[:4]
        lines[-1] = lines[-1].rstrip() + "…"
    return "\n".join(lines)


def _process_pages(pdf, process, machine, result, font, start_page=1):
    """Paginate one physical row per layer with repeated column headers."""
    rows = _process_layer_rows(process, machine, result)
    if not rows:
        return start_page
    headers = ["层", "蒸发源", "材料", "目标 / nm", "速率 / nm/s", "厚度基准", "时长 / s"]
    widths = [0.045, 0.175, 0.175, 0.125, 0.13, 0.20, 0.15]
    # Width budgets include double-width CJK. Fixed four-line maxima keep even
    # long filesystem names to one readable table row instead of unbounded pages.
    budgets = [4, 18, 18, 13, 13, 20, 15]
    prepared = [[_table_cell(cell, budget) for cell, budget in zip(row, budgets)] for row in rows]
    cursor, page_no = 0, start_page
    while cursor < len(prepared):
        fig = Figure(figsize=(8.27, 11.69), dpi=120, facecolor="white")
        fig.text(0.06, 0.955, "工艺层表", fontsize=16, fontproperties=font,
                 weight="bold", color="#172554")
        profile = process.get("profile", process) if isinstance(process, Mapping) else {}
        title = str(profile.get("name") or "工艺方案")
        subtitle = _table_cell(f"{title}  ·  共 {len(rows)} 层", 106).split("\n")[:2]
        fig.text(0.06, 0.925, "\n".join(subtitle), fontsize=8.5,
                 va="top", fontproperties=font, color="#475569")
        left, total_width, y = 0.06, 0.88, 0.872

        def draw_row(cells, height, *, heading=False, alternate=False):
            nonlocal y
            x = left
            for cell, fraction in zip(cells, widths):
                width = total_width * fraction
                fig.add_artist(Rectangle((x, y-height), width, height,
                    transform=fig.transFigure, facecolor="#e2e8f0" if heading else
                    "#f8fafc" if alternate else "white", edgecolor="#cbd5e1", linewidth=.5))
                fig.text(x + .006, y - .007, cell, fontsize=7.6,
                         va="top", linespacing=1.25, fontproperties=font,
                         weight="bold" if heading else "normal", color="#172554" if heading else "#334155")
                x += width
            y -= height

        draw_row(headers, .032, heading=True)
        first = cursor
        while cursor < len(prepared):
            row = prepared[cursor]
            height = .018 + .013 * max(cell.count("\n") + 1 for cell in row)
            if y - height < .065:
                break
            draw_row(row, height, alternate=cursor % 2 == 1)
            cursor += 1
        fig.text(.06, .035, f"第 {first + 1}-{cursor} 层 / 共 {len(rows)} 层；长名称以省略号显示", fontsize=7.5,
                 fontproperties=font, color="#64748b")
        fig.text(.94, .018, str(page_no), fontsize=8, ha="right", color="#64748b")
        pdf.savefig(fig)
        page_no += 1
    return page_no


def _detail_pages(pdf, sections, font, start_page=2):
    rows = []
    for title, value in sections:
        rows.append(("heading", title))
        for key, item in _key_value_rows(value):
            rows.extend(("data", part) for part in _wrap_line(f"{key}：{item}"))
        rows.append(("data", ""))

    page_no = start_page
    cursor = 0
    while cursor < len(rows):
        fig = Figure(figsize=(8.27, 11.69), dpi=120, facecolor="white")
        fig.text(0.07, 0.955, "仿真参数与计算记录", fontsize=16,
                 fontproperties=font, weight="bold", color="#172554")
        y = 0.915
        line_count = 0
        while cursor < len(rows) and line_count < 55:
            kind, value = rows[cursor]
            if kind == "heading" and line_count >= 53:
                break
            if kind == "heading":
                y -= 0.007
                fig.text(0.07, y, value, fontsize=10, fontproperties=font,
                         weight="bold", color="#1d4ed8")
            else:
                fig.text(0.075, y, value, fontsize=8.1, fontproperties=font,
                         color="#334155")
            y -= 0.0152
            line_count += 1
            cursor += 1
        fig.text(0.93, 0.018, str(page_no), fontsize=8, ha="right",
                 color="#64748b")
        pdf.savefig(fig)
        page_no += 1


def export_result_pdf(path, *, result, settings, machine, process, elapsed_s,
                      requested_device, sections=None):
    """Write one film-thickness plot and the input/computation snapshot.

    ``result`` is the dictionary returned by ``simulate`` or
    ``simulate_process``. ``settings`` may be a Settings dataclass or mapping;
    machine/process may be mappings or ``None``. The file is replaced only
    after every PDF page has been written successfully. Returns the saved path.
    """
    destination = Path(path).expanduser().resolve()
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    font = _font()
    selected = report_sections(sections)
    # Scalar statistics are printed below the image by default; optional audit
    # sections add pages only when requested. A report without a heatmap is valid.
    audit = tuple(item for item in selected if item not in ("thickness", "uniformity"))
    details = _detail_sections(result, settings, machine, process, elapsed_s,
                               requested_device, generated_at,
                               audit if "heatmap" in selected else selected)
    first_page = (_figure_with_plot(result, elapsed_s, requested_device,
                                   font, generated_at, selected, bool(details) or "process" in selected)
                  if "heatmap" in selected else None)
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".pdf", prefix=".deposition-",
                                     dir=destination.parent, delete=False) as temp:
        temporary_path = Path(temp.name)
    try:
        with PdfPages(temporary_path, metadata={
                "Title": "沉积仿真结果", "Author": "deposition_sim",
                "Subject": "仿真图像、参数与计算信息"}) as pdf:
            if first_page is not None:
                pdf.savefig(first_page)
            page_no = 2 if first_page is not None else 1
            if "process" in selected and process is not None:
                page_no = _process_pages(pdf, process, machine, result, font, start_page=page_no)
            _detail_pages(pdf, details, font, start_page=page_no)
        replace_with_retry(temporary_path, destination)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return str(destination)
