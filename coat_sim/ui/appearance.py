"""Qt controller for the isolated thin-film appearance render window."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import uuid
from typing import Any
from coat_sim.project_session import OperationBinding

from PySide6.QtCore import QObject, Property, QProcess, QProcessEnvironment, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QImageReader
from PySide6.QtQuick import (
    QQuickItem, QSGFlatColorMaterial, QSGGeometry, QSGGeometryNode, QSGNode,
)
from PySide6.QtQml import qmlRegisterType


_DETACHED_APPEARANCE_OBSERVERS: list[QProcess] = []


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _worker_python(root: Path) -> str:
    """Return the dedicated renderer Python; never fall back to the GUI runtime."""
    module_root = Path(__file__).resolve().parents[2]
    for candidate_root in dict.fromkeys((Path(root), module_root)):
        runtime = candidate_root / "build" / "mitsuba_appearance_env"
        runtime_python = (runtime / "Scripts" / "python.exe" if os.name == "nt" else
                          runtime / "bin" / "python")
        if runtime_python.is_file():
            return str(runtime_python.resolve())
    raise FileNotFoundError(
        "缺少独立外观渲染环境：build/mitsuba_appearance_env")


def _worker_process_environment(root: Path, worker_python: str) -> QProcessEnvironment:
    """Keep the renderer's Python and DLL lookup away from the GUI environment."""
    environment = QProcessEnvironment.systemEnvironment()
    for name in environment.keys():
        upper = name.upper()
        if ((upper.startswith(("CONDA", "MAMBA", "PYTHON"))
             and upper not in {"PYTHONUTF8", "PYTHONIOENCODING", "PYTHONUNBUFFERED"})
                or upper in {"VIRTUAL_ENV", "__PYVENV_LAUNCHER__"}
                or upper in {"QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
                             "QT_QPA_FONTDIR", "QML_IMPORT_PATH", "QML2_IMPORT_PATH"}):
            environment.remove(name)

    project_root = Path(root).resolve()
    project_conda = project_root / ".conda"

    def is_isolated_path_entry(value: str) -> bool:
        candidate = Path(value)
        try:
            candidate.resolve(strict=False).relative_to(project_conda)
            return True
        except ValueError:
            pass
        components = [component.casefold() for component in candidate.parts]
        return any(
            component in {"conda", "condabin", ".conda", "site-packages"}
            or component.startswith(("miniconda", "anaconda", "mambaforge", "miniforge"))
            or "conda" in component or "mamba" in component
            or (component.startswith("python") and component[6:7].isdigit())
            for component in components
        )

    def sanitized_search_path(variable: str, preferred: str | None = None) -> str:
        entries = []
        if preferred:
            entries.append(preferred)
        entries.extend(environment.value(variable, "").split(os.pathsep))
        result = []
        seen = set()
        for entry in entries:
            entry = entry.strip().strip('"')
            if not entry or is_isolated_path_entry(entry):
                continue
            normalized = os.path.normcase(os.path.normpath(entry))
            if normalized not in seen:
                result.append(entry)
                seen.add(normalized)
        return os.pathsep.join(result)

    worker_scripts = str(Path(worker_python).resolve().parent)
    environment.insert("PATH", sanitized_search_path("PATH", worker_scripts))
    for variable in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"):
        if environment.contains(variable):
            environment.insert(variable, sanitized_search_path(variable))
    environment.insert("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    environment.insert("PYTHONNOUSERSITE", "1")
    environment.insert("PYTHONSAFEPATH", "1")
    return environment


def _display_field_label(label: str) -> str:
    """Translate editable UI field labels without changing any stored keys."""
    names = {
        "geometry_thickness_mm": "实体厚度", "solid geometry thickness": "实体厚度",
        "solid substrate geometry thickness": "基片实体厚度", "width": "图像宽度",
        "height": "图像高度", "spp": "每像素采样数", "seed": "随机种子",
        "wavelength_start_nm": "波长起点", "wavelength_end_nm": "波长终点",
        "wavelength_step_nm": "波长步长", "wavelength start": "波长起点",
        "wavelength end": "波长终点", "wavelength step": "波长步长",
        "vertical FOV": "垂直视场角", "fov_y_deg": "垂直视场角",
        "render width": "渲染宽度", "render height": "渲染高度",
        "area width": "面光源宽度", "area height": "面光源高度",
        "area scale": "面光源强度尺度", "environment scale": "环境光强度尺度",
        "area_scale": "面光源强度尺度", "environment_scale": "环境光强度尺度",
        "area_size_x": "面光源宽度", "area_size_y": "面光源高度",
        "camera direction": "相机方向", "camera right": "相机右方向",
        "camera up projection": "相机上方向", "camera up": "相机上方向",
    }
    if label in names:
        return names[label]
    for prefix, title in (("area_target_", "面光源瞄准点"),
                          ("area_up_", "面光源上方向"), ("camera_", "相机位置"),
                          ("target_", "相机目标"), ("up_", "相机上方向"),
                          ("area_", "面光源位置"), ("camera position ", "相机位置"),
                          ("camera target ", "相机目标"), ("camera up ", "相机上方向"),
                          ("area target ", "面光源瞄准点"),
                          ("area up ", "面光源上方向"), ("area ", "面光源位置")):
        if label.startswith(prefix) and label[len(prefix):] in "xyz":
            return title + " " + label[len(prefix):].upper()
    return label


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    label = _display_field_label(label)
    if isinstance(value, bool):
        raise ValueError(f"{label}须为有限数值。")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}须为有限数值。") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{label}须为有限数值" + ("且大于零。" if positive else "。"))
    return result


def _dot3(a: list[float], b: list[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross3(a: list[float], b: list[float]) -> list[float]:
    return [a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0]]


def _unit3(value: list[float], label: str) -> list[float]:
    length = math.sqrt(_dot3(value, value))
    if not math.isfinite(length) or length < 1e-12:
        raise ValueError(f"{_display_field_label(label)}须为非零有限向量。")
    return [component / length for component in value]


def _rotate3(value: list[float], axis: list[float], angle: float) -> list[float]:
    """Rotate a vector by Rodrigues' formula; all inputs are scene-local values."""
    cosine, sine = math.cos(angle), math.sin(angle)
    cross = _cross3(axis, value)
    projection = _dot3(axis, value) * (1.0 - cosine)
    return [value[i] * cosine + cross[i] * sine + axis[i] * projection
            for i in range(3)]


def _camera_quaternion(position: list[float], target: list[float],
                       up: list[float]) -> list[float]:
    """Return a Qt quaternion mapping camera-local axes onto the mm scene pose."""
    forward = _unit3([target[i] - position[i] for i in range(3)], "camera direction")
    up_projection = [up[i] - forward[i] * _dot3(up, forward) for i in range(3)]
    camera_up = _unit3(up_projection, "camera up projection")
    right = _unit3(_cross3(forward, camera_up), "camera right")
    camera_up = _unit3(_cross3(right, forward), "camera up")
    backward = [-component for component in forward]

    # The matrix columns are camera right, up, and backward in scene coordinates.
    m00, m01, m02 = right[0], camera_up[0], backward[0]
    m10, m11, m12 = right[1], camera_up[1], backward[1]
    m20, m21, m22 = right[2], camera_up[2], backward[2]
    trace = m00 + m11 + m22
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        quaternion = [0.25 * scale, (m21 - m12) / scale,
                      (m02 - m20) / scale, (m10 - m01) / scale]
    elif m00 > m11 and m00 > m22:
        scale = math.sqrt(max(0.0, 1.0 + m00 - m11 - m22)) * 2.0
        quaternion = [(m21 - m12) / scale, 0.25 * scale,
                      (m01 + m10) / scale, (m02 + m20) / scale]
    elif m11 > m22:
        scale = math.sqrt(max(0.0, 1.0 + m11 - m00 - m22)) * 2.0
        quaternion = [(m02 - m20) / scale, (m01 + m10) / scale,
                      0.25 * scale, (m12 + m21) / scale]
    else:
        scale = math.sqrt(max(0.0, 1.0 + m22 - m00 - m11)) * 2.0
        quaternion = [(m10 - m01) / scale, (m02 + m20) / scale,
                      (m12 + m21) / scale, 0.25 * scale]
    magnitude = math.sqrt(sum(component * component for component in quaternion))
    if not math.isfinite(magnitude) or magnitude < 1e-12:
        raise ValueError("camera pose does not define a finite orientation")
    return [component / magnitude for component in quaternion]


def _camera_axes(position: list[float], target: list[float],
                 up: list[float]) -> tuple[list[float], list[float], list[float]]:
    forward = _unit3([target[i] - position[i] for i in range(3)], "camera direction")
    up_projection = [up[i] - forward[i] * _dot3(up, forward) for i in range(3)]
    camera_up = _unit3(up_projection, "camera up projection")
    right = _unit3(_cross3(forward, camera_up), "camera right")
    return forward, right, _unit3(_cross3(right, forward), "camera up")


def _project_preview_view_point(view: tuple[float, float, float], width: float,
                                height: float, fov_y_deg: float,
                                near: float) -> tuple[float, float] | None:
    """Project Mitsuba-compatible camera-space coordinates into viewport pixels."""
    x, y, depth = view
    if depth < near or width <= 0.0 or height <= 0.0:
        return None
    tangent = math.tan(math.radians(fov_y_deg) * 0.5)
    if not math.isfinite(tangent) or tangent <= 0.0:
        return None
    pixel = (width * 0.5 + x * (height * 0.5) / (depth * tangent),
             height * 0.5 - y * (height * 0.5) / (depth * tangent))
    return pixel if all(math.isfinite(value) for value in pixel) else None


_PREVIEW_NODE_COLORS = (
    "slab_sides", "surface", "grid", "outline", "axis_x", "axis_y",
    "axis_z", "area", "area_direction",
)


class AppearanceSceneCanvas(QQuickItem):
    """Geometry-only perspective view drawn with reusable Qt Quick scenegraph nodes."""

    sceneDataChanged = Signal()
    surfaceColorChanged = Signal()
    gridColorChanged = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.setFlag(QQuickItem.Flag.ItemHasContents, True)
        self._scene_data: dict[str, Any] = {}
        self._surface_color = QColor("#87929d")
        self._grid_color = QColor("#71808d")
        self._scene_node_refs: list[Any] = []
        # Keep the PySide AttributeSet wrapper alive for all scene-graph geometries.
        self._scene_attributes = QSGGeometry.defaultAttributes_Point2D()

    @Property("QVariantMap", notify=sceneDataChanged)
    def sceneData(self) -> dict[str, Any]:
        return self._scene_data

    @sceneData.setter
    def sceneData(self, value: Any) -> None:
        self._scene_data = copy.deepcopy(value) if isinstance(value, dict) else {}
        self.sceneDataChanged.emit()
        self.update()

    @Property(QColor, notify=surfaceColorChanged)
    def surfaceColor(self) -> QColor:
        return QColor(self._surface_color)

    @surfaceColor.setter
    def surfaceColor(self, value: QColor) -> None:
        color = QColor(value)
        if color.isValid() and color != self._surface_color:
            self._surface_color = color
            self.surfaceColorChanged.emit()
            self.update()

    @Property(QColor, notify=gridColorChanged)
    def gridColor(self) -> QColor:
        return QColor(self._grid_color)

    @gridColor.setter
    def gridColor(self, value: QColor) -> None:
        color = QColor(value)
        if color.isValid() and color != self._grid_color:
            self._grid_color = color
            self.gridColorChanged.emit()
            self.update()

    @staticmethod
    def _finite_vector(value: Any, size: int = 3) -> list[float] | None:
        try:
            result = [float(value[index]) for index in range(size)]
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        return result if all(math.isfinite(component) for component in result) else None

    @staticmethod
    def _clip_near(points: list[tuple[float, float, float]],
                   near: float) -> list[tuple[float, float, float]]:
        if not points:
            return []
        output: list[tuple[float, float, float]] = []
        previous = points[-1]
        previous_inside = previous[2] >= near
        for current in points:
            current_inside = current[2] >= near
            if current_inside != previous_inside:
                denominator = current[2] - previous[2]
                if denominator != 0.0:
                    t = (near - previous[2]) / denominator
                    output.append(tuple(previous[i] + t * (current[i] - previous[i])
                                        for i in range(3)))
            if current_inside:
                output.append(current)
            previous, previous_inside = current, current_inside
        return output

    @staticmethod
    def _view_plane_values(point: tuple[float, float, float], near: float,
                           tan_y: float, aspect: float) -> tuple[float, ...]:
        x, y, depth = point
        tan_x = tan_y * aspect
        return (depth - near, x + depth * tan_x, -x + depth * tan_x,
                y + depth * tan_y, -y + depth * tan_y)

    @classmethod
    def _clip_view_segment(cls, a: tuple[float, float, float],
                           b: tuple[float, float, float], near: float,
                           tan_y: float, aspect: float
                           ) -> tuple[tuple[float, float, float],
                                      tuple[float, float, float]] | None:
        first, second = list(a), list(b)
        for plane_index in range(5):
            first_value = cls._view_plane_values(tuple(first), near, tan_y,
                                                 aspect)[plane_index]
            second_value = cls._view_plane_values(tuple(second), near, tan_y,
                                                  aspect)[plane_index]
            if first_value < 0.0 and second_value < 0.0:
                return None
            if first_value < 0.0 or second_value < 0.0:
                denominator = first_value - second_value
                if denominator == 0.0:
                    return None
                t = first_value / denominator
                intersection = [first[i] + t * (second[i] - first[i])
                                for i in range(3)]
                if first_value < 0.0:
                    first = intersection
                else:
                    second = intersection
        return tuple(first), tuple(second)

    @classmethod
    def _clip_view_polygon(cls, points: list[tuple[float, float, float]],
                           near: float, tan_y: float,
                           aspect: float) -> list[tuple[float, float, float]]:
        for plane_index in range(5):
            if not points:
                return []
            output: list[tuple[float, float, float]] = []
            previous = points[-1]
            previous_value = cls._view_plane_values(previous, near, tan_y,
                                                    aspect)[plane_index]
            for current in points:
                current_value = cls._view_plane_values(current, near, tan_y,
                                                       aspect)[plane_index]
                if (previous_value < 0.0) != (current_value < 0.0):
                    denominator = previous_value - current_value
                    if denominator != 0.0:
                        t = previous_value / denominator
                        output.append(tuple(
                            previous[i] + t * (current[i] - previous[i])
                            for i in range(3)))
                if current_value >= 0.0:
                    output.append(current)
                previous, previous_value = current, current_value
            points = output
        return points

    @staticmethod
    def _clip_screen_polygon(points: list[tuple[float, float]], width: float,
                             height: float) -> list[tuple[float, float]]:
        for axis, limit, keep_greater in ((0, 0.0, True), (0, width, False),
                                          (1, 0.0, True), (1, height, False)):
            if not points:
                return []
            output: list[tuple[float, float]] = []
            previous = points[-1]
            previous_inside = (previous[axis] >= limit if keep_greater
                               else previous[axis] <= limit)
            for current in points:
                current_inside = (current[axis] >= limit if keep_greater
                                  else current[axis] <= limit)
                if current_inside != previous_inside:
                    denominator = current[axis] - previous[axis]
                    if denominator != 0.0:
                        t = (limit - previous[axis]) / denominator
                        other = 1 - axis
                        intersection = [0.0, 0.0]
                        intersection[axis] = limit
                        intersection[other] = previous[other] + t * (
                            current[other] - previous[other])
                        output.append((intersection[0], intersection[1]))
                if current_inside:
                    output.append(current)
                previous, previous_inside = current, current_inside
            points = output
        return points

    @staticmethod
    def _append_triangle(vertices: list[tuple[float, float]],
                         a: tuple[float, float], b: tuple[float, float],
                         c: tuple[float, float]) -> None:
        vertices.extend((a, b, c))

    @classmethod
    def _append_polygon(cls, vertices: list[tuple[float, float]],
                        points: list[tuple[float, float]]) -> None:
        if len(points) < 3:
            return
        for index in range(1, len(points) - 1):
            cls._append_triangle(vertices, points[0], points[index], points[index + 1])

    @staticmethod
    def _clip_screen_line(a: tuple[float, float], b: tuple[float, float],
                          width: float, height: float) -> tuple[tuple[float, float],
                                                               tuple[float, float]] | None:
        dx, dy = b[0] - a[0], b[1] - a[1]
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, a[0]), (dx, width - a[0]),
                     (-dy, a[1]), (dy, height - a[1])):
            if p == 0.0:
                if q < 0.0:
                    return None
                continue
            ratio = q / p
            if p < 0.0:
                t0 = max(t0, ratio)
            else:
                t1 = min(t1, ratio)
            if t0 > t1:
                return None
        return ((a[0] + t0 * dx, a[1] + t0 * dy),
                (a[0] + t1 * dx, a[1] + t1 * dy))

    @staticmethod
    def _append_line(vertices: list[tuple[float, float]],
                     a: tuple[float, float], b: tuple[float, float],
                     width: float, height: float, thickness: float = 1.35) -> None:
        clipped = AppearanceSceneCanvas._clip_screen_line(a, b, width, height)
        if clipped is None:
            return
        a, b = clipped
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if not math.isfinite(length) or length < 1e-6:
            return
        ox, oy = -dy * thickness * 0.5 / length, dx * thickness * 0.5 / length
        a0, a1 = (a[0] + ox, a[1] + oy), (a[0] - ox, a[1] - oy)
        b0, b1 = (b[0] + ox, b[1] + oy), (b[0] - ox, b[1] - oy)
        vertices.extend((a0, a1, b0, b0, a1, b1))

    def _scene_vertices(self) -> dict[str, list[tuple[float, float]]]:
        vertices = {name: [] for name in _PREVIEW_NODE_COLORS}
        data = self._scene_data
        camera = data.get("camera") if isinstance(data, dict) else None
        substrate = data.get("substrate") if isinstance(data, dict) else None
        if (not isinstance(camera, dict) or not camera.get("valid")
                or not isinstance(substrate, dict) or not data.get("supported")):
            return vertices

        try:
            width, height = float(self.width()), float(self.height())
            position = self._finite_vector(camera.get("position_m"))
            target = self._finite_vector(camera.get("target_m"))
            up = self._finite_vector(camera.get("up"))
            center = self._finite_vector(substrate.get("center_m"))
            substrate_width = float(substrate.get("width_m"))
            substrate_height = float(substrate.get("height_m"))
            extent = float(substrate.get("extent_m"))
            thickness = float(substrate.get("solid_thickness_m", 0.0))
            outline_kind = substrate.get("outline_kind", "rectangle")
            radius = float(substrate.get("radius_m", 0.0)) if outline_kind == "circle" else 0.0
            fov = float(camera.get("fov_y_deg"))
            if (width <= 0.0 or height <= 0.0 or position is None or target is None
                    or up is None or center is None or substrate_width <= 0.0
                    or substrate_height <= 0.0 or extent <= 0.0 or thickness < 0.0
                    or not all(math.isfinite(value) for value in
                               (width, height, substrate_width, substrate_height,
                                extent, thickness, fov))
                    or not 0.0 < fov < 179.0
                    or (outline_kind == "circle" and (not math.isfinite(radius) or radius <= 0.0))):
                return vertices
            forward, right, camera_up = _camera_axes(position, target, up)
            distance = math.sqrt(sum((position[i] - target[i]) ** 2 for i in range(3)))
            near = max(1e-9, min(extent, distance) * 1e-5)
            tan_y = math.tan(math.radians(fov) * 0.5)
            aspect = width / height
            if (not math.isfinite(near) or near <= 0.0 or not math.isfinite(tan_y)
                    or tan_y <= 0.0 or not math.isfinite(aspect) or aspect <= 0.0):
                return vertices

            def view_point(point: list[float] | tuple[float, float, float]
                           ) -> tuple[float, float, float]:
                delta = [point[i] - position[i] for i in range(3)]
                return (_dot3(delta, right), _dot3(delta, camera_up),
                        _dot3(delta, forward))

            def project(view: tuple[float, float, float]
                        ) -> tuple[float, float] | None:
                return _project_preview_view_point(view, width, height, fov, near)

            def add_line(kind: str, a: list[float] | tuple[float, float, float],
                         b: list[float] | tuple[float, float, float],
                         thickness_px: float = 1.35) -> None:
                view_a, view_b = view_point(a), view_point(b)
                clipped = self._clip_view_segment(view_a, view_b, near,
                                                  tan_y, aspect)
                if clipped is None:
                    return
                view_a, view_b = clipped
                pa, pb = project(view_a), project(view_b)
                if pa is not None and pb is not None:
                    self._append_line(vertices[kind], pa, pb, width, height,
                                      thickness_px)

            def add_polygon(kind: str, points: list[list[float]]) -> None:
                clipped = self._clip_view_polygon(
                    [view_point(point) for point in points], near, tan_y, aspect)
                projected = [project(point) for point in clipped]
                if any(point is None for point in projected):
                    return
                screen = self._clip_screen_polygon(
                    [point for point in projected if point is not None], width, height)
                self._append_polygon(vertices[kind], screen)

            half_w, half_h = substrate_width * 0.5, substrate_height * 0.5
            x0, x1 = center[0] - half_w, center[0] + half_w
            y0, y1 = center[1] - half_h, center[1] + half_h
            base_z = 0.0
            back_z = thickness
            front = [[x0, y0, base_z], [x1, y0, base_z],
                     [x1, y1, base_z], [x0, y1, base_z]]
            back = [[x0, y0, back_z], [x1, y0, back_z],
                    [x1, y1, back_z], [x0, y1, back_z]]
            camera_side_z = position[2]

            # Draw only the camera-facing slab face and side panels; color is neutral geometry.
            if outline_kind == "circle":
                segments = 64  # View-only chord approximation; physical Mitsuba disk remains analytic.
                front = [[center[0] + radius * math.cos(2 * math.pi * i / segments),
                          center[1] + radius * math.sin(2 * math.pi * i / segments), 0.0]
                         for i in range(segments)]
                back = [[point[0], point[1], thickness] for point in front]
                if thickness > 0.0:
                    for i in range(segments):
                        j = (i + 1) % segments
                        midpoint = [(front[i][axis] + front[j][axis]) * 0.5 for axis in range(2)]
                        outward = [midpoint[0] - center[0], midpoint[1] - center[1], 0.0]
                        side_center = [midpoint[0], midpoint[1], thickness * 0.5]
                        if _dot3(outward, [position[k] - side_center[k] for k in range(3)]) > 0.0:
                            add_polygon("slab_sides", [front[i], front[j], back[j], back[i]])
            elif thickness > 0.0:
                camera_from_front = camera_side_z <= thickness * 0.5
                for index, normal in enumerate(((-1.0, 0.0, 0.0),
                                                (1.0, 0.0, 0.0),
                                                (0.0, -1.0, 0.0),
                                                (0.0, 1.0, 0.0))):
                    side_center = [center[0], center[1], thickness * 0.5]
                    if index == 0:
                        side_center[0] = x0
                        quad = [[x0, y0, 0.0], [x0, y1, 0.0],
                                [x0, y1, thickness], [x0, y0, thickness]]
                    elif index == 1:
                        side_center[0] = x1
                        quad = [[x1, y0, 0.0], [x1, y1, 0.0],
                                [x1, y1, thickness], [x1, y0, thickness]]
                    elif index == 2:
                        side_center[1] = y0
                        quad = [[x0, y0, 0.0], [x1, y0, 0.0],
                                [x1, y0, thickness], [x0, y0, thickness]]
                    else:
                        side_center[1] = y1
                        quad = [[x0, y1, 0.0], [x1, y1, 0.0],
                                [x1, y1, thickness], [x0, y1, thickness]]
                    if _dot3(normal, [position[i] - side_center[i] for i in range(3)]) > 0.0:
                        add_polygon("slab_sides", quad)
            face_z = 0.0 if camera_side_z <= thickness * 0.5 else thickness
            face = front if face_z == 0.0 else back
            if (camera_side_z < 0.0 or camera_side_z > thickness
                    or thickness == 0.0):
                add_polygon("surface", face)

            grid_offset = max(1e-9, extent * 1e-5)
            grid_z = face_z + (grid_offset if camera_side_z > face_z else -grid_offset)
            for index in range(1, 8):
                x = x0 + substrate_width * index / 8.0
                y = y0 + substrate_height * index / 8.0
                if outline_kind == "circle":
                    dx, dy = x - center[0], y - center[1]
                    if abs(dx) < radius:
                        span = math.sqrt(max(0.0, radius * radius - dx * dx))
                        add_line("grid", [x, center[1] - span, grid_z],
                                 [x, center[1] + span, grid_z], 0.9)
                    if abs(dy) < radius:
                        span = math.sqrt(max(0.0, radius * radius - dy * dy))
                        add_line("grid", [center[0] - span, y, grid_z],
                                 [center[0] + span, y, grid_z], 0.9)
                else:
                    add_line("grid", [x, y0, grid_z], [x, y1, grid_z], 0.9)
                    add_line("grid", [x0, y, grid_z], [x1, y, grid_z], 0.9)
            for corners in (front, back) if thickness > 0.0 else (front,):
                for index in range(len(corners)):
                    add_line("outline", corners[index], corners[(index + 1) % len(corners)], 1.5)
            if thickness > 0.0:
                for index in range(0, len(front), max(1, len(front) // 8)):
                    add_line("outline", front[index], back[index], 1.5)

            axis_length = extent * 0.32
            axis_z = -grid_offset * 2.0
            origin = [center[0], center[1], axis_z]
            add_line("axis_x", origin, [center[0] + axis_length, center[1], axis_z], 2.1)
            add_line("axis_y", origin, [center[0], center[1] + axis_length, axis_z], 2.1)
            add_line("axis_z", origin, [center[0], center[1], axis_z - axis_length], 2.1)

            area = data.get("area_source")
            if isinstance(area, dict) and area.get("valid"):
                area_position = self._finite_vector(area.get("position_m"))
                area_right = self._finite_vector(area.get("right"))
                area_up = self._finite_vector(area.get("up"))
                area_size = self._finite_vector(area.get("size_m"), size=2)
                if (area_position is not None and area_right is not None
                        and area_up is not None and area_size is not None
                        and area_size[0] > 0.0 and area_size[1] > 0.0):
                    half_x, half_y = area_size[0] * 0.5, area_size[1] * 0.5
                    source_corners = []
                    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                        source_corners.append([
                            area_position[i] + sx * half_x * area_right[i]
                            + sy * half_y * area_up[i] for i in range(3)])
                    for index in range(4):
                        add_line("area", source_corners[index],
                                 source_corners[(index + 1) % 4], 2.0)
                    aim = [center[0], center[1], face_z]
                    add_line("area_direction", area_position, aim, 1.25)
        except (ArithmeticError, KeyError, TypeError, ValueError, IndexError,
                OverflowError):
            return {name: [] for name in _PREVIEW_NODE_COLORS}
        return vertices

    def _ensure_scene_nodes(self, root: QSGNode | None) -> QSGNode:
        def make_geometry_node() -> tuple[QSGGeometryNode, QSGGeometry,
                                          QSGFlatColorMaterial]:
            node = QSGGeometryNode()
            geometry = QSGGeometry(self._scene_attributes, 0)
            geometry.setDrawingMode(QSGGeometry.DrawingMode.DrawTriangles)
            geometry.setVertexDataPattern(QSGGeometry.DataPattern.DynamicPattern)
            material = QSGFlatColorMaterial()
            node.setGeometry(geometry)
            node.setMaterial(material)
            node.setFlag(QSGGeometryNode.Flag.OwnsGeometry, True)
            node.setFlag(QSGGeometryNode.Flag.OwnsMaterial, True)
            return node, geometry, material

        if root is None:
            root = QSGNode()
            references: list[Any] = [root]
            for _ in _PREVIEW_NODE_COLORS:
                child, geometry, material = make_geometry_node()
                child.setFlag(QSGNode.Flag.OwnedByParent, True)
                root.appendChildNode(child)
                references.extend((child, geometry, material))
            # QSGNode is not a QObject; keep its Shiboken wrappers alive while
            # Qt owns the underlying scenegraph nodes.
            self._scene_node_refs = references
        elif not self._scene_node_refs:
            references: list[Any] = [root]
            for index in range(root.childCount()):
                child = root.childAtIndex(index)
                references.extend((child, child.geometry(), child.material()))
            self._scene_node_refs = references
        return root

    def updatePaintNode(self, old_node: QSGNode | None,
                        update_paint_node_data: Any) -> QSGNode:
        root = self._ensure_scene_nodes(old_node)
        color_map = {
            "slab_sides": self._surface_color.darker(145),
            "surface": self._surface_color,
            "grid": self._grid_color,
            "outline": self._grid_color.darker(115),
            "axis_x": QColor("#d65b5b"),
            "axis_y": QColor("#58a66b"),
            "axis_z": QColor("#4d8fc4"),
            "area": QColor("#cf9b52"),
            "area_direction": QColor("#cf9b52"),
        }
        scene_vertices = self._scene_vertices()
        for index, name in enumerate(_PREVIEW_NODE_COLORS):
            child = root.childAtIndex(index)
            geometry = child.geometry()
            points = scene_vertices[name]
            if geometry.vertexCount() != len(points):
                geometry.allocate(len(points))
            vertex_points = geometry.vertexDataAsPoint2D()
            for vertex, point in zip(vertex_points, points):
                vertex.set(point[0], point[1])
            if vertex_points:
                geometry.setVertexDataAsPoint2D(vertex_points)
            geometry.markVertexDataDirty()
            material = child.material()
            if isinstance(material, QSGFlatColorMaterial):
                material.setColor(color_map[name])
            child.markDirty(QSGNode.DirtyStateBit.DirtyGeometry |
                            QSGNode.DirtyStateBit.DirtyMaterial)
        return root


_APPEARANCE_SCENE_TYPE_REGISTERED = False


def register_appearance_scene_type() -> None:
    global _APPEARANCE_SCENE_TYPE_REGISTERED
    if not _APPEARANCE_SCENE_TYPE_REGISTERED:
        qmlRegisterType(AppearanceSceneCanvas, "CoatUi", 1, 0,
                        "AppearanceSceneCanvas")
        _APPEARANCE_SCENE_TYPE_REGISTERED = True


register_appearance_scene_type()


def _profile_geometry(context: dict[str, Any]) -> tuple[list[float], float]:
    profile = context.get("substrate_profile")
    outline = profile.get("outline") if isinstance(profile, dict) else None
    points = outline.get("points") if isinstance(outline, dict) else None
    if isinstance(outline, dict) and outline.get("kind") == "circle":
        if not isinstance(profile.get("surface"), dict) or profile["surface"].get("kind") != "flat":
            raise ValueError("冻结工艺圆形基片必须具有明确的平面表面。")
        radius = _number(outline.get("radius_mm"), "circular substrate radius", positive=True)
        return [-radius, radius, -radius, radius], 2.0 * radius
    if points is None and isinstance(outline, dict) and outline.get("kind") == "rectangle":
        try:
            width, height = float(outline["width_mm"]), float(outline["height_mm"])
            points = [[-width / 2, -height / 2], [width / 2, -height / 2],
                      [width / 2, height / 2], [-width / 2, height / 2]]
        except (KeyError, TypeError, ValueError, OverflowError):
            points = None
    try:
        xs = [float(row[0]) for row in points]
        ys = [float(row[1]) for row in points]
    except (TypeError, ValueError, IndexError):
        raise ValueError("冻结工艺中的矩形基片不可用。")
    if len(xs) < 4 or len(ys) < 4 or not all(map(math.isfinite, xs + ys)):
        raise ValueError("冻结工艺中的矩形基片无效。")
    bounds = [min(xs), max(xs), min(ys), max(ys)]
    if bounds[0] >= bounds[1] or bounds[2] >= bounds[3]:
        raise ValueError("冻结工艺中的矩形基片面积为零。")
    return bounds, max(bounds[1] - bounds[0], bounds[3] - bounds[2], 1.0)


class AppearanceWindowController(QObject):
    """Owns one immutable launch context, editable draft, local QProcess and preview."""

    changed = Signal()
    previewChanged = Signal()
    closeConfirmationRequested = Signal()
    closeReady = Signal()

    _FIELD_KEYS = frozenset({
        "geometry_thickness_mm", "camera_x", "camera_y", "camera_z",
        "target_x", "target_y", "target_z", "up_x", "up_y", "up_z",
        "fov_y_deg", "area_x", "area_y", "area_z", "area_target_x",
        "area_target_y", "area_target_z", "area_up_x", "area_up_y",
        "area_up_z", "area_size_x", "area_size_y", "area_scale",
        "environment_scale", "width", "height", "spp", "seed",
        "wavelength_start_nm", "wavelength_end_nm", "wavelength_step_nm",
    })

    def __init__(self, launch_context: dict[str, Any], *, root: str | Path,
                 parent: QObject | None = None,
                 worker_command: list[str] | None = None,
                 project_context=None):
        super().__init__(parent)
        self.project_context = project_context
        self._project_compute = None
        self._project_operation: OperationBinding | None = None
        self._pending_project_finish: tuple[int, QProcess.ExitStatus] | None = None
        if project_context is not None:
            from coat_sim.core.project_compute import ProjectComputeWorkspace
            self._project_compute = ProjectComputeWorkspace.for_project(
                project_context.host, project_context.binding)
            if Path(root).resolve() != self._project_compute.root:
                raise ValueError("Appearance root differs from its bound project")
        self.root = Path(root).resolve()
        self._context = self._project_launch_context(launch_context)
        self._context_hash = _sha256(_canonical(self._context))
        self._draft: dict[str, Any] = self._default_project_input_draft()
        self._geometry_thickness_overridden = False
        self._camera_reset_pose = self._camera_pose_from_draft()
        self._preview_camera = copy.deepcopy(self._camera_reset_pose)
        self._camera_gesture_active = False
        self._worker_command = list(worker_command) if worker_command else None
        self._backend = "cpu"
        self._precision = "native"
        self._active_backend = "cpu"
        self._active_precision = "native"
        self._active_remote = False
        self._remote_admitted = False
        self._active_host_id = "local"
        self._gpu_capability_state = "unchecked"
        self._gpu_capability_message = "尚未检测 CUDA；选择 GPU 时会异步检测。"
        self._probe_process: QProcess | None = None
        self._progress_fraction = -1.0
        self._eta_seconds = -1.0
        self._gpu_forced_stop = False
        self._process: QProcess | None = None
        self._job_id = ""
        self._job_dir: Path | None = None
        self._cancel_path: Path | None = None
        self._frozen_input_hash = ""
        self._stdout_buffer = bytearray()
        self._stderr_buffer = bytearray()
        self._worker_stdout_capture = bytearray()
        self._worker_exit_code: int | None = None
        self._worker_exit_status = ""
        self._phase = "ready"
        self._status = "导入实测或明确标为相对量的光谱表，再启用光源。"
        self._error = ""
        self._stage = ""
        self._close_after_finish = False
        self._last_result: dict[str, Any] | None = None
        from coat_sim.ui.compute_target import ComputeTarget
        self._compute_target = ComputeTarget(self.root, "appearance", self)
        self._compute_target.changed.connect(self._on_compute_target_changed)
        self._compute_target.appearanceCudaProbeRequested.connect(self._retry_gpu_probe)
        if self._compute_target.deviceId == "cuda:default" and self._compute_target.hostId == "local":
            self.setBackend("gpu")
        if self._project_compute is not None:
            self._project_compute.register_thaw_callback(self._on_project_unfrozen)

    def _default_project_input_draft(self) -> dict[str, Any]:
        bounds, extent = _profile_geometry(self._context)
        cx, cy = (bounds[0] + bounds[1]) * 0.5, (bounds[2] + bounds[3]) * 0.5
        self._inherited_geometry_thickness_mm = self._snapshot_geometry_thickness_mm(
            self._context)
        inherited_thickness = self._inherited_geometry_thickness_mm
        return {
            "geometry_thickness_mm": (f"{inherited_thickness:.12g}"
                                       if inherited_thickness is not None else ""),
            "camera_x": f"{cx:.6g}", "camera_y": f"{cy:.6g}",
            "camera_z": f"{-3.0 * extent:.6g}",
            "target_x": f"{cx:.6g}", "target_y": f"{cy:.6g}", "target_z": "0",
            "up_x": "0", "up_y": "1", "up_z": "0", "fov_y_deg": "35",
            "area_enabled": False,
            "area_x": f"{cx:.6g}", "area_y": f"{cy:.6g}",
            "area_z": f"{-1.2 * extent:.6g}",
            "area_target_x": f"{cx:.6g}", "area_target_y": f"{cy:.6g}",
            "area_target_z": "0", "area_up_x": "0", "area_up_y": "1",
            "area_up_z": "0", "area_size_x": f"{extent:.6g}",
            "area_size_y": f"{extent:.6g}", "area_scale": "1",
            "area_kind": "relative", "area_absolute_calibration": False,
            "area_spectrum": None,
            "environment_enabled": False, "environment_scale": "1",
            "environment_kind": "relative", "environment_absolute_calibration": False,
            "environment_spectrum": None,
            "width": "64", "height": "48", "spp": "8", "seed": "0",
            "wavelength_start_nm": "380", "wavelength_end_nm": "780",
            "wavelength_step_nm": "10",
        }

    def projectInputConfiguration(self) -> dict[str, Any]:
        """Return only persistent user inputs, never results or task state."""
        return {"schema": "coat_sim.appearance_configuration", "version": 1,
                "context": copy.deepcopy(self._context), "draft": copy.deepcopy(self._draft),
                "geometry_thickness_mode": "overridden" if self._geometry_thickness_overridden else "inherited",
                "backend": self._backend, "precision": self._precision}

    def adoptProjectInputConfiguration(self, payload: dict[str, Any], *, allow_invalid: bool = False) -> None:
        """Adopt an already-owned, validated input on the GUI thread."""
        if self.running:
            raise ValueError("请先完成当前外观渲染。")
        from coat_sim.core.project_simulation_resources import normalize_simulation_document
        normalized = copy.deepcopy(payload) if allow_invalid else normalize_simulation_document("appearance_configuration", {
            "kind": "coat_sim.project_appearance_configuration", "version": 1,
            "name": "外观配置", "payload": payload}, managed=False)["payload"]
        if normalized["context"] != self._context:
            raise ValueError("外观配置与当前受管来源不一致。")
        self._draft = copy.deepcopy(normalized["draft"])
        self._geometry_thickness_overridden = normalized["geometry_thickness_mode"] == "overridden"
        self._backend, self._precision = normalized["backend"], normalized["precision"]
        self._compute_target.set_appearance_backend(self._backend)
        try:
            self._preview_camera = self._camera_pose_from_draft()
            self._camera_reset_pose = copy.deepcopy(self._preview_camera)
        except (TypeError, ValueError, OverflowError):
            pass  # Invalid editable inputs stay visible and cannot start a run.
        self._camera_gesture_active = False
        self._last_result = None
        self._phase = "ready"
        self._status = "已打开工程外观输入；请检查来源与光源后渲染。"
        self._error = ""
        self.previewChanged.emit()
        self.changed.emit()

    def rebindProjectInputAssets(self, payload: dict[str, Any]) -> None:
        """Follow captured owned paths after Save without clearing a result."""
        self._context = copy.deepcopy(payload["context"])
        self._context_hash = _sha256(_canonical(self._context))
        for target in ("area", "environment"):
            self._draft[target + "_spectrum"] = copy.deepcopy(payload["draft"][target + "_spectrum"])
        self.changed.emit()

    def resetProjectInputConfiguration(self) -> None:
        """Reset editable settings while retaining the explicit upstream asset."""
        if self.running:
            raise ValueError("请先完成当前外观渲染。")
        self._draft = self._default_project_input_draft()
        self._geometry_thickness_overridden = False
        self._backend, self._precision = "cpu", "native"
        self._compute_target.set_appearance_backend("cpu")
        self._preview_camera = self._camera_pose_from_draft()
        self._camera_reset_pose = copy.deepcopy(self._preview_camera)
        self._camera_gesture_active = False
        self._last_result = None
        self._error = ""
        self._phase = "ready"
        self._status = "已新建外观输入，保留当前明确选择的逐层资产。"
        self.previewChanged.emit()
        self.changed.emit()

    def _on_project_unfrozen(self) -> None:
        pending, self._pending_project_finish = self._pending_project_finish, None
        if pending is not None:
            self._process_finished(*pending)

    def _project_launch_context(self, raw: dict[str, Any]) -> dict[str, Any]:
        context = self._validate_context(raw)
        if self._project_compute is not None:
            asset = self._project_compute.import_asset(context["asset_path"], category="deposition")
            context["asset_path"] = str(asset)
            context["asset_root"] = str(self.root)
        return context

    def _project_current(self) -> bool:
        return (self._project_compute is None or
                (self._project_operation is not None
                 and self._project_compute.accepts(self._project_operation)))

    @Property(bool, notify=changed)
    def localWorkRunning(self) -> bool:
        return bool(self.running and (not self._active_remote or not self._remote_admitted))

    @Slot()
    def cancelLocalWork(self) -> None:
        if self.localWorkRunning:
            self.cancelRender()

    @Property(QObject, constant=True)
    def computeTarget(self):
        return self._compute_target

    def _on_compute_target_changed(self):
        if self._compute_target.hostId != "local":
            # The shared selector is authoritative for a remote route.  The
            # older local backend picker must not silently override it.
            if self._compute_target.deviceId == "cpu:default":
                self._backend = "cpu"
                self._precision = "native"
            elif self._compute_target.deviceId == "cuda:default":
                self._backend = "gpu_cuda"
                if self._precision not in ("float32", "mixed"):
                    self._precision = "mixed"
            else:
                # Host discovery clears the selection first.  Keep the draft
                # non-executable and avoid displaying stale local CUDA state.
                self._backend = "cpu"
                self._precision = "native"
            self.changed.emit()
            return
        device_id = self._compute_target.deviceId
        if device_id == "cpu:default" and self._backend != "cpu":
            self.setBackend("cpu")
        elif device_id == "cuda:default" and self._backend != "gpu_cuda":
            self.setBackend("gpu")
        else:
            self.changed.emit()

    def _retry_gpu_probe(self):
        if self.running or self._compute_target.hostId != "local":
            return
        if self._gpu_capability_state == "checking":
            return
        self._gpu_capability_state = "unchecked"
        self._compute_target.publish_appearance_cuda("unchecked", "正在准备重新检查 NVIDIA CUDA。")
        self._probe_gpu_async()

    @staticmethod
    def _validate_context(raw: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict) or raw.get("schema") != "coat_sim.appearance_launch_context" or raw.get("version") != 1:
            raise ValueError("The RayTrace page did not provide a supported frozen appearance context")
        result = copy.deepcopy(raw)
        for key in ("asset_path", "asset_root", "asset_sha256", "process_profile_sha256"):
            if not isinstance(result.get(key), str) or not result[key]:
                raise ValueError(f"Frozen appearance context is missing {key}")
        if len(result["asset_sha256"]) != 64 or len(result["process_profile_sha256"]) != 64:
            raise ValueError("Frozen appearance context contains an invalid SHA-256 digest")
        if not isinstance(result.get("process_profile"), dict) or not isinstance(result.get("substrate_profile"), dict):
            raise ValueError("Frozen process profile or substrate profile is missing")
        computed = _sha256(_canonical(result["process_profile"]))
        if computed != result["process_profile_sha256"]:
            raise ValueError("Frozen process profile digest does not match its content")
        return result

    @staticmethod
    def _snapshot_geometry_thickness_mm(context: dict[str, Any]) -> float | None:
        """Convert the physical FilmDesign substrate thickness to process-local mm."""
        try:
            profile = context.get("process_profile")
            snapshot = profile.get("film_design_snapshot") if isinstance(profile, dict) else None
            design = snapshot.get("design") if isinstance(snapshot, dict) else None
            substrate = design.get("substrate") if isinstance(design, dict) else None
            thickness = substrate.get("thickness") if isinstance(substrate, dict) else None
            if not isinstance(thickness, dict):
                return None
            value = _number(thickness.get("value"), "FilmDesign substrate thickness",
                            positive=True)
            factor = {"mm": 1.0, "um": 1e-3, "nm": 1e-6}.get(thickness.get("unit"))
            if factor is None:
                return None
            result = value * factor
            return result if math.isfinite(result) and result > 0.0 else None
        except (TypeError, ValueError, OverflowError):
            return None

    @Slot("QVariantMap")
    def updateLaunchContext(self, launch_context: dict[str, Any]) -> None:
        """Replace the frozen source context when idle, retaining a user override."""
        if self.running:
            self._error = "渲染进行时不能更换冻结的来源资产。"
            self.changed.emit()
            return
        try:
            context = self._project_launch_context(launch_context)
            bounds, extent = _profile_geometry(context)
            center_x = (bounds[0] + bounds[1]) * 0.5
            center_y = (bounds[2] + bounds[3]) * 0.5
            reset_pose = {
                "position_mm": [center_x, center_y, -3.0 * extent],
                "target_mm": [center_x, center_y, 0.0],
                "up": [0.0, 1.0, 0.0],
            }
            _camera_quaternion(reset_pose["position_mm"], reset_pose["target_mm"],
                               reset_pose["up"])
        except (TypeError, ValueError, OverflowError) as exc:
            self._error = f"新的冻结来源未通过校验：{exc}"
            self.changed.emit()
            return

        context_hash = _sha256(_canonical(context))
        if context_hash == self._context_hash:
            return
        old_reset = self._camera_reset_pose
        camera_still_reset = True
        for prefix, values in (("camera_", old_reset["position_mm"]),
                               ("target_", old_reset["target_mm"]),
                               ("up_", old_reset["up"])):
            for axis, expected in zip("xyz", values):
                try:
                    if _number(self._draft[f"{prefix}{axis}"], f"{prefix}{axis}") != expected:
                        camera_still_reset = False
                except (TypeError, ValueError, OverflowError):
                    camera_still_reset = False

        self._context = context
        self._context_hash = context_hash
        self._inherited_geometry_thickness_mm = self._snapshot_geometry_thickness_mm(context)
        if not self._geometry_thickness_overridden:
            inherited = self._inherited_geometry_thickness_mm
            self._draft["geometry_thickness_mm"] = (
                f"{inherited:.12g}" if inherited is not None else "")
        self._camera_reset_pose = reset_pose
        if camera_still_reset:
            self._preview_camera = copy.deepcopy(reset_pose)
            for axis, value in zip("xyz", reset_pose["position_mm"]):
                self._draft[f"camera_{axis}"] = f"{value:.12g}"
            for axis, value in zip("xyz", reset_pose["target_mm"]):
                self._draft[f"target_{axis}"] = f"{value:.12g}"
            for axis, value in zip("xyz", reset_pose["up"]):
                self._draft[f"up_{axis}"] = f"{value:.12g}"
        self._error = ""
        self.previewChanged.emit()
        self.changed.emit()

    def _draft_digest(self) -> str:
        return _sha256(_canonical({
            "context_sha256": self._context_hash,
            "draft": self._draft,
            "geometry_thickness_mode": self.geometryThicknessMode,
            "backend": self._backend, "precision": self._precision,
        }))

    def _spectral_grid(self) -> list[float]:
        start = _number(self._draft["wavelength_start_nm"], "wavelength start", positive=True)
        end = _number(self._draft["wavelength_end_nm"], "wavelength end", positive=True)
        step = _number(self._draft["wavelength_step_nm"], "wavelength step", positive=True)
        if end <= start:
            raise ValueError("波长终点必须大于起点。")
        count = (end - start) / step
        if not math.isclose(count, round(count), abs_tol=1e-8):
            raise ValueError("波长范围必须是步长的整数倍。")
        if count + 1 > 241:
            raise ValueError("最多支持 241 个波长采样。")
        waves = [start + i * step for i in range(round(count) + 1)]
        if waves[0] < 360 or waves[-1] > 830:
            raise ValueError("XYZ 输出要求波长位于 360–830 nm。")
        return waves

    def _spectrum(self, target: str, waves: list[float]) -> dict[str, Any] | None:
        prefix = "area" if target == "area" else "environment"
        table = self._draft[f"{prefix}_spectrum"]
        if not self._draft[f"{prefix}_enabled"]:
            return {"wavelength_nm": [waves[0], waves[-1]], "value": [0.0, 0.0],
                    "kind": "relative", "scale": 1.0}
        if not isinstance(table, dict):
            raise ValueError(f"请为{'面光源' if target == 'area' else '环境光'}导入波长、数值两列的标量光谱 CSV。")
        sample_waves = table.get("wavelength_nm", [])
        values = table.get("value", [])
        if (len(sample_waves) < 2 or len(sample_waves) != len(values)
                or not all(math.isfinite(float(x)) for x in sample_waves + values)
                or any(float(b) <= float(a) for a, b in zip(sample_waves, sample_waves[1:]))
                or any(float(value) < 0 for value in values)):
            raise ValueError(f"{'面光源' if target == 'area' else '环境光'}光谱须至少含两个递增、非负采样。")
        if waves[0] < float(sample_waves[0]) or waves[-1] > float(sample_waves[-1]):
            raise ValueError(f"{'面光源' if target == 'area' else '环境光'}光谱未覆盖完整的计算波长范围。")
        result = {"wavelength_nm": list(map(float, sample_waves)),
                  "value": list(map(float, values)),
                  "kind": str(self._draft[f"{prefix}_kind"]),
                  "scale": _number(self._draft[f"{prefix}_scale"], f"{target} scale", positive=True)}
        if result["kind"] == "absolute":
            if not self._draft[f"{prefix}_absolute_calibration"]:
                raise ValueError(f"{'面光源' if target == 'area' else '环境光'}只有经过辐射量校准才能标为绝对光谱。")
            result["calibration"] = "absolute_radiometric"
        return result

    def _validate_draft(self) -> str:
        try:
            waves = self._spectral_grid()
            self._effective_geometry_thickness_mm()
            self._validated_camera_from_draft()
            if not self._draft["area_enabled"] and not self._draft["environment_enabled"]:
                raise ValueError("请至少启用一个已导入标量光谱的光源。")
            self._spectrum("area", waves)
            self._spectrum("environment", waves)
            for key in self._FIELD_KEYS:
                if key in ("geometry_thickness_mm",):
                    continue
                value = _number(self._draft[key], key)
                if key in ("width", "height", "spp", "seed") and value != int(value):
                    raise ValueError(f"{_display_field_label(key)}须为整数。")
                if key in ("width", "height", "spp") and value < 1:
                    raise ValueError(f"{_display_field_label(key)}须大于零。")
                if key == "seed" and (value < 0 or value > 0xFFFFFFFF):
                    raise ValueError("随机种子必须在 uint32 范围内。")
            pixels = int(self._draft["width"]) * int(self._draft["height"])
            if pixels > 1_048_576 or int(self._draft["spp"]) > 4096 or (self._backend == "cpu" and pixels * int(self._draft["spp"]) * len(waves) > 20_000_000):
                raise ValueError("图像与波长的总采样量超过本地渲染上限。")
            if self._backend == "gpu_cuda":
                if int(self._draft["seed"]) + len(waves) - 1 > 0xFFFFFFFF:
                    raise ValueError("CUDA 波长随机种子超过 uint32 范围。")
                if pixels * int(self._draft["spp"]) * 12 >= 2**32:
                    raise ValueError("CUDA 单波长诊断计数可能超过 uint32 范围。")
            return ""
        except (TypeError, ValueError, OverflowError) as exc:
            return str(exc)

    def _render_inputs(self) -> dict[str, Any]:
        waves = self._spectral_grid()
        d = self._draft
        camera_pose, camera_fov = self._validated_camera_from_draft()
        area_spectrum = self._spectrum("area", waves)
        environment_spectrum = self._spectrum("environment", waves)
        position = [_number(d[f"area_{axis}"], f"area {axis}") for axis in "xyz"]
        target = [_number(d[f"area_target_{axis}"], f"area target {axis}") for axis in "xyz"]
        result = {
            "wavelength_nm": waves,
            "coordinate_unit": "mm",
            "substrate_profile": copy.deepcopy(self._context["substrate_profile"]),
            "camera": {
                "position_mm": camera_pose["position_mm"],
                "target_mm": camera_pose["target_mm"],
                "up": camera_pose["up"],
                "fov_y_deg": camera_fov,
            },
            "area_source": {
                "shape": "rectangle", "position_mm": position,
                "normal": [target[i] - position[i] for i in range(3)],
                "up": [_number(d[f"area_up_{axis}"], f"area up {axis}") for axis in "xyz"],
                "size_mm": [_number(d["area_size_x"], "area width", positive=True),
                            _number(d["area_size_y"], "area height", positive=True)],
                "spectrum": area_spectrum,
            },
            "environment_spectrum": environment_spectrum,
            "resolution": {"width": int(d["width"]), "height": int(d["height"]),
                           "spp": int(d["spp"]), "seed": int(d["seed"])},
        }
        if self._context["substrate_profile"].get("outline", {}).get("kind") == "circle":
            from coat_sim.appearance.circle import BOUNDARY_POLICY
            result["circle_boundary_sampling"] = BOUNDARY_POLICY
        return result

    def _camera_pose_from_draft(self) -> dict[str, list[float]]:
        draft = self._draft
        position = [_number(draft[f"camera_{axis}"], f"camera {axis}") for axis in "xyz"]
        target = [_number(draft[f"target_{axis}"], f"camera target {axis}") for axis in "xyz"]
        up = [_number(draft[f"up_{axis}"], f"camera up {axis}") for axis in "xyz"]
        _camera_quaternion(position, target, up)
        return {"position_mm": position, "target_mm": target, "up": up}

    def _validated_camera_from_draft(self) -> tuple[dict[str, list[float]], float]:
        pose = self._camera_pose_from_draft()
        fov = _number(self._draft["fov_y_deg"], "vertical FOV", positive=True)
        if fov < 0.001 or fov >= 179.0:
            raise ValueError("垂直视场角必须不小于 0.001° 且小于 179°。")
        return pose, fov

    def _effective_geometry_thickness_mm(self) -> float:
        if (not self._geometry_thickness_overridden
                and self._inherited_geometry_thickness_mm is not None):
            return self._inherited_geometry_thickness_mm
        return _number(self._draft["geometry_thickness_mm"],
                       "solid substrate geometry thickness", positive=True)

    @staticmethod
    def _rectangle_outline(profile: dict[str, Any]) -> bool:
        outline = profile.get("outline") if isinstance(profile, dict) else None
        if not isinstance(outline, dict):
            return False
        if outline.get("kind") == "circle":
            try:
                return (profile.get("surface", {}).get("kind") == "flat"
                        and _number(outline.get("radius_mm"), "circle radius", positive=True) > 0)
            except (TypeError, ValueError, OverflowError):
                return False
        if outline.get("kind") == "rectangle":
            return True
        points = outline.get("points")
        try:
            rows = [(float(row[0]), float(row[1])) for row in points]
        except (TypeError, ValueError, IndexError, OverflowError):
            return False
        if len(rows) != 4 or not all(math.isfinite(value) for row in rows for value in row):
            return False
        xs = {row[0] for row in rows}
        ys = {row[1] for row in rows}
        if len(xs) != 2 or len(ys) != 2:
            return False
        return set(rows) == {(x, y) for x in xs for y in ys}

    @Property("QVariantMap", notify=previewChanged)
    def previewScene(self) -> dict[str, Any]:
        """Lightweight geometry-only scene data in the renderer's process-local mm frame."""
        bounds, extent = _profile_geometry(self._context)
        center_x = (bounds[0] + bounds[1]) * 0.5
        center_y = (bounds[2] + bounds[3]) * 0.5
        width_mm, height_mm = bounds[1] - bounds[0], bounds[3] - bounds[2]
        camera_valid = True
        camera_validation_error = ""
        try:
            _, fov = self._validated_camera_from_draft()
            camera = copy.deepcopy(self._preview_camera if self._camera_gesture_active
                                   else self._camera_pose_from_draft())
            orientation = _camera_quaternion(
                camera["position_mm"], camera["target_mm"], camera["up"])
        except (TypeError, ValueError, OverflowError) as exc:
            camera_valid = False
            camera_validation_error = str(exc)
            camera = {"position_mm": [], "target_mm": [], "up": []}
            orientation = []
            fov = None
        aspect = 4.0 / 3.0
        try:
            width_px = _number(self._draft["width"], "render width", positive=True)
            height_px = _number(self._draft["height"], "render height", positive=True)
            if width_px == int(width_px) and height_px == int(height_px):
                aspect = width_px / height_px
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            pass

        area_valid = False
        area_position: list[float] = []
        area_size: list[float] = []
        area_orientation: list[float] = []
        area_right: list[float] = []
        area_plane_up: list[float] = []
        try:
            area_position = [_number(self._draft[f"area_{axis}"], f"area {axis}") for axis in "xyz"]
            area_target = [_number(self._draft[f"area_target_{axis}"], f"area target {axis}") for axis in "xyz"]
            area_up = [_number(self._draft[f"area_up_{axis}"], f"area up {axis}") for axis in "xyz"]
            area_size = [_number(self._draft["area_size_x"], "area width", positive=True),
                         _number(self._draft["area_size_y"], "area height", positive=True)]
            # The rectangle primitive's local +Z is its front; point that normal at the substrate.
            area_orientation = _camera_quaternion(area_target, area_position, area_up)
            _, area_right, area_plane_up = _camera_axes(area_target, area_position, area_up)
            area_valid = True
        except (TypeError, ValueError, OverflowError):
            pass

        try:
            solid_thickness_m = self._effective_geometry_thickness_mm() * 0.001
        except (TypeError, ValueError, OverflowError):
            solid_thickness_m = 0.0

        return {
            "supported": self._rectangle_outline(self._context["substrate_profile"]),
            "substrate": {
                "center_m": [center_x * 0.001, center_y * 0.001, 0.0],
                "outline_kind": self._context["substrate_profile"].get("outline", {}).get("kind", "rectangle"),
                "radius_m": (float(self._context["substrate_profile"].get("outline", {}).get("radius_mm", 0.0)) * 0.001),
                "width_m": width_mm * 0.001,
                "height_m": height_mm * 0.001,
                "extent_m": extent * 0.001,
                "solid_thickness_m": solid_thickness_m,
            },
            "camera": {
                "valid": camera_valid,
                "validation_error": camera_validation_error,
                "position_m": [value * 0.001 for value in camera["position_mm"]],
                "target_m": [value * 0.001 for value in camera["target_mm"]],
                "up": camera["up"],
                "rotation": orientation,
                "fov_y_deg": fov,
            },
            "area_source": {
                "valid": area_valid,
                "enabled": bool(self._draft["area_enabled"]),
                "position_m": [value * 0.001 for value in area_position],
                "size_m": [value * 0.001 for value in area_size],
                "rotation": area_orientation,
                "right": area_right,
                "up": area_plane_up,
            },
            "environment_enabled": bool(self._draft["environment_enabled"]),
            "aspect_ratio": aspect,
        }

    def _set_preview_camera(self, position: list[float], target: list[float],
                            up: list[float]) -> None:
        _camera_quaternion(position, target, up)
        _, _, camera_up = _camera_axes(position, target, up)
        self._preview_camera = {
            "position_mm": [float(value) for value in position],
            "target_mm": [float(value) for value in target],
            "up": [float(value) for value in camera_up],
        }
        self.previewChanged.emit()

    @Slot()
    def beginCameraGesture(self) -> None:
        self._camera_gesture_active = True

    @Slot(float, float)
    def orbitCamera(self, delta_x: float, delta_y: float) -> None:
        try:
            dx = max(-100000.0, min(100000.0, _number(delta_x, "orbit x")))
            dy = max(-100000.0, min(100000.0, _number(delta_y, "orbit y")))
            position = self._preview_camera["position_mm"]
            target = self._preview_camera["target_mm"]
            up = self._preview_camera["up"]
            offset = [position[i] - target[i] for i in range(3)]
            radius = math.sqrt(_dot3(offset, offset))
            yaw = dx * 0.005
            offset = _rotate3(offset, [0.0, 1.0, 0.0], yaw)
            up = _rotate3(up, [0.0, 1.0, 0.0], yaw)
            elevation = math.asin(max(-1.0, min(1.0, offset[1] / radius)))
            target_elevation = max(math.radians(-89.5), min(math.radians(89.5),
                                     elevation - dy * 0.005))
            pitch = elevation - target_elevation
            forward = _unit3([-value for value in offset], "camera direction")
            camera_right = _unit3(_cross3(forward, up), "camera right")
            offset = _rotate3(offset, camera_right, pitch)
            up = _rotate3(up, camera_right, pitch)
            self._set_preview_camera([target[i] + offset[i] for i in range(3)], target, up)
        except (TypeError, ValueError, OverflowError):
            return

    @Slot(float, float, float)
    def panCamera(self, delta_x: float, delta_y: float, viewport_height: float) -> None:
        try:
            dx = max(-100000.0, min(100000.0, _number(delta_x, "pan x")))
            dy = max(-100000.0, min(100000.0, _number(delta_y, "pan y")))
            height = _number(viewport_height, "viewport height", positive=True)
            position = self._preview_camera["position_mm"]
            target = self._preview_camera["target_mm"]
            up = self._preview_camera["up"]
            _, right, camera_up = _camera_axes(position, target, up)
            distance = math.sqrt(sum((position[i] - target[i]) ** 2 for i in range(3)))
            _, fov = self._validated_camera_from_draft()
            millimetres_per_pixel = 2.0 * distance * math.tan(math.radians(fov) * 0.5) / height
            translation = [(-dx * right[i] + dy * camera_up[i]) * millimetres_per_pixel
                           for i in range(3)]
            moved_target = [target[i] + translation[i] for i in range(3)]
            moved_position = [position[i] + translation[i] for i in range(3)]
            self._set_preview_camera(moved_position, moved_target, camera_up)
        except (TypeError, ValueError, OverflowError):
            return

    @Slot(float)
    def zoomCamera(self, distance_factor: float) -> None:
        try:
            factor = _number(distance_factor, "zoom", positive=True)
            position = self._preview_camera["position_mm"]
            target = self._preview_camera["target_mm"]
            up = self._preview_camera["up"]
            offset = [position[i] - target[i] for i in range(3)]
            radius = math.sqrt(_dot3(offset, offset))
            _, extent = _profile_geometry(self._context)
            minimum, maximum = max(0.01, extent * 0.02), max(100.0, extent * 1000.0)
            next_radius = max(minimum, min(maximum, radius * max(0.05, min(20.0, factor))))
            ratio = next_radius / radius
            moved_position = [target[i] + offset[i] * ratio for i in range(3)]
            self._set_preview_camera(moved_position, target, up)
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            return

    @Slot()
    def commitCameraGesture(self) -> None:
        if not self._camera_gesture_active:
            return
        self._camera_gesture_active = False
        camera = self._preview_camera
        updates = {}
        for axis, value in zip("xyz", camera["position_mm"]):
            updates[f"camera_{axis}"] = f"{value:.12g}"
        for axis, value in zip("xyz", camera["target_mm"]):
            updates[f"target_{axis}"] = f"{value:.12g}"
        for axis, value in zip("xyz", camera["up"]):
            updates[f"up_{axis}"] = f"{value:.12g}"
        changed = False
        for key, value in updates.items():
            try:
                unchanged = _number(self._draft[key], key) == _number(value, key)
            except (TypeError, ValueError, OverflowError):
                unchanged = self._draft[key] == value
            if not unchanged:
                self._draft[key] = value
                changed = True
        if changed:
            self._error = ""
            self.changed.emit()

    @Slot()
    def resetCamera(self) -> None:
        self._preview_camera = copy.deepcopy(self._camera_reset_pose)
        self._camera_gesture_active = True
        self.previewChanged.emit()
        self.commitCameraGesture()

    @Property(str, notify=changed)
    def phase(self) -> str:
        return self._phase

    @Property(str, notify=changed)
    def backend(self) -> str:
        return "gpu" if self._backend == "gpu_cuda" else "cpu"

    @Property(str, notify=changed)
    def precision(self) -> str:
        return self._precision

    @Property(bool, notify=changed)
    def gpuAvailable(self) -> bool:
        return self._gpu_capability_state == "available"

    @Property(str, notify=changed)
    def gpuCapabilityMessage(self) -> str:
        return self._gpu_capability_message

    @Property(float, notify=changed)
    def progressFraction(self) -> float:
        return self._progress_fraction

    @Property(float, notify=changed)
    def etaSeconds(self) -> float:
        return self._eta_seconds

    @Property(str, notify=changed)
    def previewBackend(self) -> str:
        if not self._last_result:
            return ""
        return "gpu" if self._last_result.get("backend") == "gpu_cuda" else "cpu"

    @Slot(str)
    def setBackend(self, value: str) -> None:
        if self._compute_target.hostId != "local":
            return
        selected = {"cpu": "cpu", "gpu": "gpu_cuda"}.get(value)
        if selected is None or selected == self._backend:
            return
        self._backend = selected
        self._compute_target.set_appearance_backend(selected)
        self._precision = "native" if selected == "cpu" else "mixed"
        self._error = ""
        self.changed.emit()
        if selected == "gpu_cuda" and self._gpu_capability_state == "unchecked":
            self._probe_gpu_async()

    @Slot(str)
    def setPrecision(self, value: str) -> None:
        if self._backend != "gpu_cuda" or value not in ("float32", "mixed") or value == self._precision:
            return
        self._precision = value
        self._error = ""
        self.changed.emit()

    def _probe_gpu_async(self) -> None:
        """Probe isolated CUDA runtime off the Qt/UI thread on demand."""
        if self._gpu_capability_state != "unchecked":
            return
        try:
            program = _worker_python(self.root)
        except (OSError, ValueError) as exc:
            self._gpu_capability_state = "unavailable"
            self._gpu_capability_message = f"CUDA 不可用：{exc}"
            self._compute_target.publish_appearance_cuda("unavailable", self._gpu_capability_message)
            self.changed.emit()
            return
        process = QProcess(self)
        process.setWorkingDirectory(str(self.root))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.setProcessEnvironment(_worker_process_environment(
            Path(__file__).resolve().parents[2], program))
        process.finished.connect(self._gpu_probe_finished)
        process.errorOccurred.connect(self._gpu_probe_error)
        self._probe_process = process
        self._gpu_capability_state = "checking"
        self._gpu_capability_message = "正在独立进程中检测 NVIDIA CUDA 设备。"
        self._compute_target.publish_appearance_cuda("checking", self._gpu_capability_message)
        self.changed.emit()
        process.start(program, ["-s", "-P", "-m", "coat_sim.appearance.gpu", "--probe"])

    @Slot(int, QProcess.ExitStatus)
    def _gpu_probe_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        process = self._probe_process
        self._probe_process = None
        if process is None:
            return
        output = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        process.deleteLater()
        try:
            payload = json.loads(output.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError, TypeError):
            payload = {}
        available = (exit_code == 0 and exit_status == QProcess.ExitStatus.NormalExit
                     and payload.get("available") is True
                     and payload.get("variant") == "cuda_ad_rgb")
        self._gpu_capability_state = "available" if available else "unavailable"
        reason = str(payload.get("reason", "CUDA 设备或独立渲染运行时未通过执行检测。"))[:500]
        self._gpu_capability_message = ("CUDA backend cuda_ad_rgb 已通过设备执行检测；未识别具体显卡型号，GPU 结果仍未获精度资格认证。"
                                        if available else f"CUDA 不可用：{reason}")
        self._compute_target.publish_appearance_cuda(self._gpu_capability_state, self._gpu_capability_message)
        self.changed.emit()

    @Slot(QProcess.ProcessError)
    def _gpu_probe_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart and self._probe_process is not None:
            process = self._probe_process
            self._probe_process = None
            process.deleteLater()
            self._gpu_capability_state = "unavailable"
            self._gpu_capability_message = "CUDA 不可用：无法启动独立渲染环境。"
            self._compute_target.publish_appearance_cuda("unavailable", self._gpu_capability_message)
            self.changed.emit()

    @Property(str, notify=changed)
    def status(self) -> str:
        return self._status

    @Property(str, notify=changed)
    def errorText(self) -> str:
        return self._error

    @Property(str, notify=changed)
    def stage(self) -> str:
        return self._stage

    @Property(bool, notify=changed)
    def running(self) -> bool:
        return self._phase in ("starting", "running", "cancelling")

    @Property(bool, notify=changed)
    def canStart(self) -> bool:
        if self._compute_target.hostId != "local":
            return (not self.running and not self._validate_draft()
                    and self._compute_target.canSubmit
                    and ((self._compute_target.deviceId == "cpu:default"
                          and (self._backend, self._precision) == ("cpu", "native"))
                         or (self._compute_target.deviceId == "cuda:default"
                             and self._backend == "gpu_cuda"
                             and self._precision in ("mixed", "float32"))))
        return (not self.running and not self._validate_draft()
                and self._compute_target.canSubmit
                and (self._backend != "gpu_cuda" or self.gpuAvailable))

    @Property(bool, notify=changed)
    def canCancel(self) -> bool:
        return self.running and self._phase != "cancelling"

    @Property(str, notify=changed)
    def validationMessage(self) -> str:
        if self._compute_target.hostId != "local":
            return self._error or self._validate_draft() or (
                "远端参数已就绪。提交后由服务端重新冻结资产并验证结果。"
                if self._compute_target.canSubmit else self._compute_target.message)
        if self._backend == "gpu_cuda" and not self.gpuAvailable:
            return self._error or self._gpu_capability_message
        return self._error or self._validate_draft() or "参数已就绪，可以开始本地渲染。"

    @Property("QVariantMap", notify=changed)
    def draft(self) -> dict[str, Any]:
        return copy.deepcopy(self._draft)

    @Property(str, notify=changed)
    def contextSummary(self) -> str:
        profile = self._context["process_profile"]
        imported = profile.get("film_design_import") or {}
        snapshot = profile.get("film_design_snapshot") or {}
        design = snapshot.get("design") or {}
        layers = design.get(f"{imported.get('surface')}_layers", [])
        ids = ", ".join(str(row.get("id", "?")) for row in layers if isinstance(row, dict)) or "未镀膜表面"
        side = {"front": "正面", "back": "背面"}.get(imported.get("surface"), "未知")
        return (f"资产：{Path(self._context['asset_path']).name}  ·  SHA-256 {self._context['asset_sha256'][:12]}…\n"
                f"v7 表面：{side}  ·  膜层：{ids}\n"
                f"捕获厚度场：{len(self._context.get('captured_step_ids', []))} 步  ·  基片轮廓由资产继承")

    @Property(str, notify=changed)
    def opticalThicknessReference(self) -> str:
        snapshot = self._context["process_profile"].get("film_design_snapshot") or {}
        design = snapshot.get("design") or {}
        thickness = (design.get("substrate") or {}).get("thickness")
        if not isinstance(thickness, dict):
            return "膜系基片实体厚度：不可用"
        return (f"膜系基片实体厚度：{thickness.get('value')} "
                f"{thickness.get('unit')} （平面且平行背面的几何近似）")

    @Property(str, notify=changed)
    def geometryThicknessMode(self) -> str:
        if self._geometry_thickness_overridden:
            return "explicit_override"
        if self._inherited_geometry_thickness_mm is not None:
            return "film_design_snapshot"
        return "missing"

    @Property(bool, notify=changed)
    def canResetGeometryThicknessToInherited(self) -> bool:
        return self._inherited_geometry_thickness_mm is not None

    @Property(str, notify=changed)
    def geometryThicknessSourceText(self) -> str:
        inherited = self._inherited_geometry_thickness_mm
        if self._geometry_thickness_overridden:
            try:
                value = _number(self._draft["geometry_thickness_mm"],
                                "solid geometry thickness", positive=True)
                return (f"外观显式厚度：{value:g} mm。背面"
                        "采用平面且平行的近似。")
            except (TypeError, ValueError, OverflowError):
                return "显式几何厚度无效，请输入大于零的毫米数。"
        if inherited is None:
            return ("膜系没有可用的基片厚度。请输入"
                    "大于零的几何厚度（mm）；背面采用平面且平行的近似。")
        return (f"继承冻结膜系的基片厚度：{inherited:g} mm。"
                "平行背面属于几何近似。")

    @Slot()
    def resetGeometryThicknessToInherited(self) -> None:
        if self._inherited_geometry_thickness_mm is None:
            return
        value = f"{self._inherited_geometry_thickness_mm:.12g}"
        mode_changed = self._geometry_thickness_overridden
        value_changed = self._draft["geometry_thickness_mm"] != value
        self._geometry_thickness_overridden = False
        self._draft["geometry_thickness_mm"] = value
        if mode_changed or value_changed:
            self._error = ""
            self.previewChanged.emit()
            self.changed.emit()

    @Property(str, notify=changed)
    def appearanceThicknessLabel(self) -> str:
        return "沉积仿真逐层厚度场"

    @Property(str, notify=changed)
    def areaSpectrumSummary(self) -> str:
        return self._spectrum_summary("area")

    @Property(str, notify=changed)
    def environmentSpectrumSummary(self) -> str:
        return self._spectrum_summary("environment")

    def _spectrum_summary(self, prefix: str) -> str:
        table = self._draft.get(f"{prefix}_spectrum")
        if not isinstance(table, dict):
            return "尚未导入光谱"
        name = Path(str(table.get("path", "spectrum.csv"))).name
        return f"{name}: {len(table['wavelength_nm'])} 个采样，{table['wavelength_nm'][0]:g}–{table['wavelength_nm'][-1]:g} nm"

    @Property(str, notify=changed)
    def previewUrl(self) -> str:
        if not self._last_result:
            return ""
        return QUrl.fromLocalFile(str(self._last_result["image_path"])).toString()

    @Property(bool, notify=changed)
    def hasPreview(self) -> bool:
        return self._last_result is not None

    @Property(bool, notify=changed)
    def previewStale(self) -> bool:
        return bool(self._last_result and self._last_result.get("input_sha256") != self._draft_digest())

    @Property(str, notify=changed)
    def previewSummary(self) -> str:
        if not self._last_result:
            return "尚无已完成的本地渲染"
        return (f"{self._last_result['width']} × {self._last_result['height']} · "
                f"{self._last_result['wavelength_count']} 个波长 · "
                f"输入摘要 {self._last_result['input_sha256'][:12]}"
                + (" · CUDA GPU 结果未获图像精度资格认证"
                   if self._last_result.get("backend") == "gpu_cuda" else ""))

    @Slot(str, str)
    def setField(self, key: str, value: str) -> None:
        if key not in self._FIELD_KEYS:
            return
        value = str(value).strip()
        current = self._draft.get(key)
        if current == value:
            return
        try:
            numerically_unchanged = _number(current, key) == _number(value, key)
        except (TypeError, ValueError, OverflowError):
            numerically_unchanged = False
        if numerically_unchanged:
            if key == "geometry_thickness_mm" and not self._geometry_thickness_overridden:
                self._geometry_thickness_overridden = True
                self._error = ""
                self.previewChanged.emit()
                self.changed.emit()
            return
        self._draft[key] = value
        if key == "geometry_thickness_mm":
            self._geometry_thickness_overridden = True
        if key in {"camera_x", "camera_y", "camera_z", "target_x", "target_y",
                   "target_z", "up_x", "up_y", "up_z"}:
            try:
                self._preview_camera = self._camera_pose_from_draft()
            except (TypeError, ValueError, OverflowError):
                pass
        if key in {"camera_x", "camera_y", "camera_z", "target_x", "target_y",
                   "target_z", "up_x", "up_y", "up_z", "fov_y_deg",
                   "area_x", "area_y", "area_z", "area_target_x", "area_target_y",
                   "area_target_z", "area_up_x", "area_up_y", "area_up_z",
                   "area_size_x", "area_size_y", "geometry_thickness_mm",
                   "width", "height"}:
            self.previewChanged.emit()
        self._error = ""
        self.changed.emit()

    @Slot(str, bool)
    def setFlag(self, key: str, value: bool) -> None:
        if key not in ("area_enabled", "environment_enabled", "area_absolute_calibration",
                       "environment_absolute_calibration"):
            return
        self._draft[key] = bool(value)
        if key in ("area_enabled", "environment_enabled"):
            self.previewChanged.emit()
        self._error = ""
        self.changed.emit()

    @Slot(str, str)
    def setSpectrumKind(self, target: str, kind: str) -> None:
        if target not in ("area", "environment") or kind not in ("relative", "absolute"):
            return
        self._draft[f"{target}_kind"] = kind
        self.changed.emit()

    @Slot(str, str)
    def loadSpectrum(self, file_url: str, target: str) -> None:
        if target not in ("area", "environment"):
            return
        owner = self.parent()
        resources = getattr(owner, "_project_input_resources", None) if owner is not None else None
        if resources is not None and self._project_compute is not None:
            url = QUrl(str(file_url))
            selected_path = url.toLocalFile() if url.isValid() and url.isLocalFile() else str(file_url)
            resources.captureSpectrum(str(Path(selected_path).expanduser()), target, self)
            return
        try:
            url = QUrl(str(file_url))
            path = Path(url.toLocalFile() if url.isValid() and url.isLocalFile() else str(file_url)).expanduser()
            path = (self._project_compute.import_asset(path, category="spectra")
                    if self._project_compute is not None else path.resolve())
            if not path.is_file() or path.stat().st_size > 2_000_000:
                raise ValueError("请选择小于 2 MB 的本地 CSV 文件。")
            waves, values = [], []
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                for line_number, raw in enumerate(stream, 1):
                    stripped = raw.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    row = next(csv.reader([raw]))
                    if len(row) < 2:
                        raise ValueError(f"CSV 第 {line_number} 行需要波长、数值两列。")
                    try:
                        wavelength, value = float(row[0]), float(row[1])
                    except (TypeError, ValueError):
                        if not waves and any(token in stripped.lower() for token in ("wavelength", "nm", "value", "spd")):
                            continue
                        raise ValueError(f"CSV 第 {line_number} 行不是两个数值。")
                    if not math.isfinite(wavelength) or not math.isfinite(value) or value < 0:
                        raise ValueError(f"CSV 第 {line_number} 行含无效波长，或负数、非有限光谱值。")
                    if waves and wavelength <= waves[-1]:
                        raise ValueError("CSV 波长必须严格递增。")
                    waves.append(wavelength)
                    values.append(value)
            if len(waves) < 2:
                raise ValueError("CSV 必须至少包含两个光谱采样。")
            if self._project_compute is not None:
                path = self._project_compute.import_asset(path, category="spectra")
            self._draft[f"{target}_spectrum"] = {"path": str(path), "wavelength_nm": waves, "value": values}
            self._error = ""
            self.changed.emit()
        except (OSError, UnicodeError, ValueError, csv.Error) as exc:
            self._error = str(exc)
            self.changed.emit()

    @Slot()
    def startRender(self) -> None:
        if self.running:
            return
        remote = self._compute_target.hostId != "local"
        expected_device = "cpu:default" if self._backend == "cpu" else "cuda:default"
        if (not self._compute_target.canSubmit
                or self._compute_target.deviceId != expected_device
                or (remote and self._backend == "gpu_cuda"
                    and self._precision not in ("mixed", "float32"))):
            self._error = "所选主机或设备尚不可执行外观渲染；未创建任务。"
            self.changed.emit()
            return
        if not remote and self._backend == "gpu_cuda" and not self.gpuAvailable:
            if self._gpu_capability_state == "unchecked":
                self._probe_gpu_async()
            self._error = self._gpu_capability_message
            self.changed.emit()
            return
        message = self._validate_draft()
        if message:
            self._error = message
            self._phase = "ready"
            self.changed.emit()
            return
        try:
            render_inputs = self._render_inputs()
            self._job_id = uuid.uuid4().hex
            if self._project_compute is not None:
                self._project_operation = self._project_compute.capture(self._job_id)
            self._frozen_input_hash = self._draft_digest()
            self._active_backend = self._backend
            self._active_precision = self._precision
            self._active_remote = remote
            self._remote_admitted = False
            self._active_host_id = self._compute_target.hostId
            self._job_dir = self.root / "data" / "tmp" / "appearance" / self._job_id
            if self._project_compute is not None:
                self._job_dir = self._project_compute.owned_path(self._job_dir, allow_missing=True)
            self._job_dir.mkdir(parents=True, exist_ok=False)
            self._cancel_path = self._job_dir / "cancel.request"
            request = {
                "schema": ("coat_sim.appearance_remote_worker_input" if remote
                           else "coat_sim.appearance_worker_input"), "version": 1,
                "job_id": self._job_id, "input_sha256": self._frozen_input_hash,
                "context_sha256": self._context_hash,
                "context": copy.deepcopy(self._context), "render_inputs": render_inputs,
                "backend": self._active_backend, "precision": self._active_precision,
                "geometry_thickness_mode": self.geometryThicknessMode,
                "geometry_thickness_mm": (
                    None if self.geometryThicknessMode == "film_design_snapshot"
                    else self._effective_geometry_thickness_mm()),
            }
            if self._project_compute is not None:
                if self._project_operation is None:
                    raise ValueError("Project render is missing its frozen operation")
                metadata = self._project_compute.metadata(self._project_operation)
                if remote:
                    request["origin"] = metadata["origin"]
                self._atomic_json(self._job_dir / "project_binding.json", metadata)
                self._project_compute.register_job(
                    self._job_id, module_key="film_appearance", service_owned=False,
                    state="starting", metadata=metadata)
            if remote:
                # A host ID is a one-way address label.  URL and credential
                # stay only in settings/keyring and never enter job JSON/argv.
                request.update(host_id=self._active_host_id,
                               device_id=expected_device)
            self._atomic_json(self._job_dir / "worker_input.json", request)
            self._stdout_buffer.clear()
            self._stderr_buffer.clear()
            self._worker_stdout_capture.clear()
            self._worker_exit_code = None
            self._worker_exit_status = ""
            self._progress_fraction = -1.0
            self._eta_seconds = -1.0
            self._gpu_forced_stop = False
            process = QProcess(self)
            process.setWorkingDirectory(str(self.root))
            process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
            process.readyReadStandardOutput.connect(self._read_stdout)
            process.readyReadStandardError.connect(self._read_stderr)
            process.started.connect(self._process_started)
            process.finished.connect(self._process_finished)
            process.errorOccurred.connect(self._process_error)
            self._process = process
            self._phase = "starting"
            self._status = ("正在启动远端提交子进程。" if remote else
                            "正在启动独立的 CUDA 渲染进程；首帧可能需要编译。"
                            if self._active_backend == "gpu_cuda" else
                            "正在启动独立的本地渲染进程。")
            self._stage = "preflight"
            self._error = ""
            self.changed.emit()
            if self._worker_command:
                program, *args = self._worker_command
            elif remote:
                from coat_sim.process_entry import module_command
                program, *args = module_command("coat_sim.ui.appearance_remote_worker")
            else:
                program, args = _worker_python(self.root), ["-s", "-P", "-m", "coat_sim.ui.appearance_worker"]
            args += ["--input", str(self._job_dir / "worker_input.json"),
                     "--job-dir", str(self._job_dir), "--cancel-marker", str(self._cancel_path)]
            source_root = Path(__file__).resolve().parents[2]
            worker_python = program if Path(program).is_absolute() else (
                __import__("sys").executable if remote else _worker_python(self.root))
            process.setProcessEnvironment(_worker_process_environment(source_root, worker_python))
            process.start(program, args)
        except Exception as exc:
            self._phase = "failed"
            self._error = str(exc)
            self._status = "无法启动远端提交。" if remote else "无法启动本地渲染。"
            self.changed.emit()

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                        allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    @Slot()
    def cancelRender(self) -> None:
        if not self.running or self._phase == "cancelling":
            return
        if self._cancel_path is not None:
            self._cancel_path.write_text("cancel\n", encoding="ascii")
        self._phase = "cancelling"
        self._stage = "cancellation boundary"
        self._status = ("已请求远端取消；等待服务端确认终态。" if self._active_remote else
                        "已请求取消 CUDA 渲染；等待当前波长帧结束，若编译卡住将只停止本任务进程。"
                        if self._active_backend == "gpu_cuda" else
                        "已请求取消，正在等待当前波长帧结束。")
        self.changed.emit()
        if self._active_backend == "gpu_cuda" and not self._active_remote:
            process, job_id = self._process, self._job_id
            def stop_stuck_gpu() -> None:
                try:
                    if (self._process is process and self._job_id == job_id
                            and process is not None and self._phase == "cancelling"
                            and process.state() != QProcess.ProcessState.NotRunning):
                        self._gpu_forced_stop = True
                        process.kill()
                except RuntimeError:
                    # Qt already destroyed this window's completed child.
                    pass
            QTimer.singleShot(10_000, stop_stuck_gpu)

    @Slot()
    def requestClose(self) -> None:
        if self.running:
            self.closeConfirmationRequested.emit()
        else:
            self.closeReady.emit()

    @Slot(bool)
    def resolveClose(self, cancel_and_close: bool) -> None:
        if not cancel_and_close:
            return
        if not self.running:
            self.closeReady.emit()
            return
        self._close_after_finish = True
        self.cancelRender()

    @Slot()
    def detach_observers(self) -> None:
        """Detach an admitted remote observation without cancelling its job."""
        if self._project_compute is not None:
            self._project_compute.unregister_thaw_callback(self._on_project_unfrozen)
        self._pending_project_finish = None
        if self.project_context is not None and self._active_remote and self._remote_admitted:
            process, self._process = self._process, None
            if process is not None:
                for signal, callback in (
                        (process.started, self._process_started),
                        (process.finished, self._process_finished),
                        (process.errorOccurred, self._process_error),
                        (process.readyReadStandardOutput, self._read_stdout),
                        (process.readyReadStandardError, self._read_stderr)):
                    try:
                        signal.disconnect(callback)
                    except (RuntimeError, TypeError):
                        pass
                process.setParent(None)
                _DETACHED_APPEARANCE_OBSERVERS.append(process)
                compute, job_id = self._project_compute, self._job_id
                def release(exit_code, _status):
                    if compute is not None:
                        compute.update_job(job_id, "succeeded" if exit_code == 0 else "observer_failed")
                    if process in _DETACHED_APPEARANCE_OBSERVERS:
                        _DETACHED_APPEARANCE_OBSERVERS.remove(process)
                    process.deleteLater()
                process.finished.connect(release)
            return
        self.shutdown()

    @Slot()
    def shutdown(self) -> None:
        if self.project_context is not None and self._active_remote and self._remote_admitted:
            self.detach_observers()
            return
        if self._project_compute is not None:
            self._project_compute.unregister_thaw_callback(self._on_project_unfrozen)
        self._pending_project_finish = None
        # The QML close path waits for this page's child to finish first.
        if self.running:
            self.cancelRender()
        if self._probe_process is not None:
            self._probe_process.kill()

    @Slot()
    def _process_started(self) -> None:
        if self._phase == "starting":
            self._phase = "running"
            self._status = ("远端提交子进程已启动，正在核对主机和资产。"
                            if self._active_remote else
                            "CUDA 子进程已启动，正在预检及准备首帧。"
                            if self._active_backend == "gpu_cuda" else
                            "正在本地子进程中逐波长渲染。")
            self.changed.emit()

    @Slot()
    def _read_stdout(self) -> None:
        if self._process is None:
            return
        chunk = bytes(self._process.readAllStandardOutput())
        self._worker_stdout_capture.extend(chunk)
        if len(self._worker_stdout_capture) > 64_000:
            self._worker_stdout_capture = self._worker_stdout_capture[-32_000:]
        self._stdout_buffer.extend(chunk)
        if len(self._stdout_buffer) > 64_000:
            self._stdout_buffer = self._stdout_buffer[-32_000:]
        while b"\n" in self._stdout_buffer:
            line, _, rest = self._stdout_buffer.partition(b"\n")
            self._stdout_buffer = bytearray(rest)
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict) or len(line) > 8192:
                continue
            self._stage = str(message.get("stage", self._stage))[:96]
            if (self._active_remote and message.get("type") == "status"
                    and message.get("stage") == "queued"):
                self._remote_admitted = True
                if self._project_compute is not None:
                    self._project_compute.update_job(
                        self._job_id, "queued", service_owned=True)
            fraction = message.get("fraction")
            if isinstance(fraction, (int, float)) and not isinstance(fraction, bool) and math.isfinite(fraction) and 0 <= fraction <= 1:
                self._progress_fraction = float(fraction)
            eta = message.get("estimated_remaining_s")
            self._eta_seconds = (float(eta) if isinstance(eta, (int, float))
                                 and not isinstance(eta, bool) and math.isfinite(eta)
                                 and eta >= 0 else -1.0)
            if message.get("type") == "error":
                self._error = str(message.get("message", "Render child failed"))[:2000]
            elif message.get("type") == "status":
                status = str(message.get("message", self._status))[:1000]
                translations = {
                    "Reading the captured per-step field arrays.": "正在读取捕获的逐步厚度场。",
                    "Captured thickness arrays validated; reading the frozen process asset metadata.": "厚度数组已校验，正在读取冻结工艺资产信息。",
                    "Joining captured fields to frozen FilmDesign layer IDs.": "正在按冻结膜层 ID 关联捕获厚度场。",
                    "The frozen film stack passed input validation.": "冻结膜系已通过输入校验。",
                    "Rechecking process asset hash and frozen v7 profile.": "正在复核工艺资产摘要与冻结 v7 配置。",
                    "Joining captured process fields to frozen FilmDesign layer IDs.": "正在按冻结膜层 ID 关联工艺厚度场。",
                    "Renderer module loaded; adapting verified v7 fields.": "渲染模块已载入，正在转换已验证的 v7 厚度场。",
                    "Frozen layers and captured thickness fields passed validation.": "冻结膜层与捕获厚度场已通过校验。",
                    "Rendering independent scalar wavelength frames.": "正在逐波长渲染标量图像。",
                    "Render files and completion manifest are ready.": "渲染文件与完成清单已就绪。",
                    "Cancellation arrived before preflight completed.": "预检完成前收到取消请求。",
                    "Cancellation arrived during asset preflight.": "资产预检时收到取消请求。",
                    "Cancellation arrived during field validation.": "厚度场校验时收到取消请求。",
                }
                self._status = translations.get(status, status)
            self.changed.emit()

    @Slot()
    def _read_stderr(self) -> None:
        if self._process is None:
            return
        self._stderr_buffer.extend(bytes(self._process.readAllStandardError()))
        if len(self._stderr_buffer) > 64_000:
            self._stderr_buffer = self._stderr_buffer[-32_000:]

    @Slot(QProcess.ProcessError)
    def _process_error(self, error: QProcess.ProcessError) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._phase = "failed"
            self._error = ("应用 Python 无法启动远端提交进程。" if self._active_remote
                           else "本地 Python 无法启动外观渲染进程。")
            self._status = ("远端提交进程未启动。" if self._active_remote
                            else "本地渲染进程未启动。")
            self.changed.emit()

    @Slot(int, QProcess.ExitStatus)
    def _process_finished(self, exit_code: int, exit_status: QProcess.ExitStatus) -> None:
        if self._project_compute is not None:
            self._project_compute.update_job(
                self._job_id, "succeeded" if exit_code == 0 else "failed")
        self._worker_exit_code = int(exit_code)
        self._worker_exit_status = exit_status.name
        self._read_stdout()
        self._read_stderr()
        process = self._process
        self._process = None
        if process is not None:
            process.deleteLater()
        if (self._project_compute is not None and self._project_compute.editing_frozen
                and exit_code == 0 and exit_status == QProcess.ExitStatus.NormalExit):
            self._pending_project_finish = (exit_code, exit_status)
            self._phase = "ready"
            self._status = "渲染已完成；退出检查期间暂存结果。"
            self.changed.emit()
            return
        if self._phase == "failed" and exit_status == QProcess.ExitStatus.CrashExit:
            pass
        elif self._cancel_path is not None and self._cancel_path.exists():
            self._phase = "failed" if self._error else "ready"
            self._status = ("远端取消未获确认；请使用保留的任务 ID 核对服务端状态。"
                            if self._active_remote and self._error else
                            "远端已确认取消；未接收未完成结果。" if self._active_remote else
                            "已停止本任务的 CUDA 子进程，未接收未完成结果。"
                            if self._gpu_forced_stop else
                            "已在波长帧边界取消渲染，未接收未完成结果。")
            if not self._error:
                self._error = ""
        elif exit_code == 0 and exit_status == QProcess.ExitStatus.NormalExit:
            try:
                self._accept_result()
                self._phase = "complete"
                self._progress_fraction = 1.0
                self._eta_seconds = 0.0
                self._status = ("远端 CUDA 图像及结果已校验；仍未获图像精度资格认证。"
                                if self._active_remote and self._active_backend == "gpu_cuda" else
                                "CUDA 外观图像已生成；仍未获图像精度资格认证。"
                                if self._active_backend == "gpu_cuda" else
                                "远端 CPU 渲染及结果校验已完成。" if self._active_remote else
                                "本地渲染已完成。")
                self._error = ""
            except Exception as exc:
                self._phase = "failed"
                self._error = f"渲染输出未通过校验：{exc}"
                self._status = "保留上次已验证的结果预览。"
        else:
            self._phase = "failed"
            stderr = self._stderr_buffer.decode("utf-8", errors="replace").strip()
            if not self._error:
                self._error = stderr[-2000:] or f"外观渲染进程已退出，退出码为 {exit_code}。"
            self._status = "远端外观渲染或结果校验失败。" if self._active_remote else "本地外观渲染失败。"
        self.changed.emit()
        if self._close_after_finish:
            self._close_after_finish = False
            self.closeReady.emit()

    def _accept_result(self) -> None:
        if not self._project_current():
            raise ValueError("来源项目已关闭、重新打开或正在退出；结果保留在原项目任务目录")
        if self._job_dir is None:
            raise ValueError("job directory is missing")
        path = self._job_dir / "appearance_result.json"
        if not path.is_file() or path.stat().st_size > 1_000_000:
            raise ValueError("completion manifest is missing or too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(payload, dict) or payload.get("job_id") != self._job_id
                or payload.get("input_sha256") != self._frozen_input_hash
                or payload.get("schema") != "coat_sim.appearance_result"):
            raise ValueError("job ID or frozen input hash does not match")
        if (payload.get("backend", "cpu") != self._active_backend
                or payload.get("precision", "native") != self._active_precision):
            raise ValueError("result backend or precision differs from frozen request")
        if payload.get("image") != "render/display.png" or payload.get("render_manifest") != "render/manifest.json":
            raise ValueError("result contains an unsupported output path")
        render_dir = self._job_dir / "render"
        image_path = render_dir / "display.png"
        manifest_path = render_dir / "manifest.json"
        if not image_path.is_file() or not manifest_path.is_file():
            raise ValueError("display image or render manifest is missing")
        image_bytes = image_path.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
        if len(image_bytes) > 64_000_000 or _sha256(image_bytes) != payload.get("image_sha256"):
            raise ValueError("display image digest is invalid")
        decoded_image = QImageReader(str(image_path)).read()
        if decoded_image.isNull():
            raise ValueError("display image is not a decodable PNG")
        output_width, output_height = int(payload["width"]), int(payload["height"])
        if (output_width < 1 or output_height < 1 or
                (decoded_image.width(), decoded_image.height()) != (output_width, output_height)):
            raise ValueError("display image dimensions do not match its completion manifest")
        if _sha256(manifest_bytes) != payload.get("render_manifest_sha256"):
            raise ValueError("render manifest digest is invalid")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if manifest.get("input_sha256") != payload.get("render_input_sha256"):
            raise ValueError("render input digest is invalid")
        if self._active_backend == "gpu_cuda" and (
                manifest.get("schema") != "coat_sim.gpu_appearance_preview"
                or manifest.get("status") != "rendered_unqualified"
                or manifest.get("precision") != self._active_precision
                or manifest.get("execution", {}).get("variant") != "cuda_ad_rgb"
                or any(row.get("diagnostics", {}).get("pass") is not True
                       for row in manifest.get("per_wavelength", []))):
            raise ValueError("CUDA output was rejected or lacks valid diagnostics")
        self._last_result = {
            "image_path": image_path, "input_sha256": self._frozen_input_hash,
            "backend": self._active_backend, "precision": self._active_precision,
            "width": int(payload["width"]), "height": int(payload["height"]),
            "wavelength_count": int(payload["wavelength_count"]),
            "render_input_sha256": payload["render_input_sha256"],
        }

