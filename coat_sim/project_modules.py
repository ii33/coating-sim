"""Canonical project module identities shared by headless and desktop clients."""

from __future__ import annotations


PROJECT_MODULES = (
    ("film_design", "膜系设计"),
    ("film_simulation", "光谱仿真"),
    ("film_optimization", "膜系优化"),
    ("process", "沉积工艺"),
    ("main", "沉积仿真"),
    ("machine", "机器设计"),
    ("source", "蒸发源"),
    ("mask", "修正板"),
    ("substrate", "基片"),
    ("materials", "工程材料"),
    ("project_resources", "工程资产库"),
    ("ray_trace", "光线追迹"),
    ("film_appearance", "外观仿真"),
    ("compute_queue", "工程计算队列"),
)


def project_module_options() -> list[dict[str, str]]:
    """Return detached navigation data; callers cannot mutate the registry."""
    return [{"key": key, "name": name} for key, name in PROJECT_MODULES]
