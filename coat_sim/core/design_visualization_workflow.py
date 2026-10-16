"""Artifact adapter for prepared design-workflow bundles.

The workflow runner can call this after preparation or completion. It returns
only artifact references and compact counts; full layer data stays in the HTML.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from .data_files import MAX_FILE_BYTES, data_path
from .design_visualization import (render_design_visualization,
                                   render_process_detail_html)


def render_design_workflow_artifacts(service, manifest: Mapping[str, Any] | str) -> dict[str, Any]:
    """Render saved workflow dependencies and return compact file metadata.

    ``manifest`` is the result of ``prepare_design_bundle``. Every referenced
    input is re-authorized under data before ``CoatService.preview`` reads it.
    No process steps or profile JSON are copied into the result.
    """
    if isinstance(manifest, str):
        manifest_file = data_path(manifest, root_allowed=False)
        if not manifest_file.is_file() or manifest_file.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("工作流清单不存在或超过文件大小限制")
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取工作流清单：{exc}") from exc
    paths = manifest.get("paths") if isinstance(manifest, Mapping) else None
    if not isinstance(paths, Mapping):
        raise ValueError("工作流清单缺少 paths")

    artifacts = []
    summaries = []

    substrate_path = paths.get("substrate")
    substrate_profile = None
    if substrate_path:
        if not isinstance(substrate_path, str):
            raise ValueError("工作流 substrate 文件路径无效")
        substrate_preview = service.preview(
            "substrate", str(data_path(substrate_path, root_allowed=False)))
        if substrate_preview.get("issues"):
            raise ValueError("工作流基片依赖无效：" + "；".join(map(str, substrate_preview["issues"])))
        substrate_profile = substrate_preview["data"]

    def draw(kind: str, path: Any) -> None:
        if not isinstance(path, str):
            raise ValueError(f"工作流 {kind} 文件路径无效")
        result = render_design_visualization(service, kind,
                                             str(data_path(path, root_allowed=False)),
                                             substrate_profile=substrate_profile
                                             if kind == "machine" else None)
        artifacts.extend(result["files"])
        summaries.append(result["summary"])

    for kind in ("sources", "masks"):
        values = paths.get(kind, [])
        if not isinstance(values, list):
            raise ValueError(f"工作流 {kind} 路径须为数组")
        singular = "source" if kind == "sources" else "mask"
        for path in values:
            draw(singular, path)
    for kind in ("machine", "substrate"):
        path = paths.get(kind)
        if path:
            draw(kind, path)
    process_path = paths.get("process")
    if process_path:
        if not isinstance(process_path, str):
            raise ValueError("工作流 process 文件路径无效")
        authorized = str(data_path(process_path, root_allowed=False))
        result = render_process_detail_html(service, authorized)
        artifacts.extend(result["files"])
        summaries.append(result["summary"])
    return {"files": artifacts,
            "summary": {"kind": "design-workflow", "artifact_count": len(artifacts),
                        "layer_count": next((item["layer_count"] for item in summaries
                                              if item.get("kind") == "process-detail"), 0),
                        "designs": summaries}}
