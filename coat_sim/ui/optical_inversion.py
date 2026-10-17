"""Page-owned, asynchronous measured-spectrum inversion with explicit append/export.

Workers own plain value snapshots only. Numerical model validation belongs to
core.optical_inversion; this adapter never changes an existing optical dataset.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from threading import BoundedSemaphore, Event, Lock
from typing import Any
from uuid import uuid4
import weakref

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication

from coat_sim.core import optical_constants
from coat_sim.core.project_dependencies import reject_links

MAX_FORM_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_MEASUREMENT_SAMPLES = 4096
_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="optical-inversion")
_PENDING = BoundedSemaphore(4)
_WARNING_TEXT = {
    "non_unique": "模型依赖估计：低残差（包括R+T）不能证明n/k唯一或模型正确。",
    "fixed_thickness": "膜厚和基片是固定输入；它们的误差会影响n/k，本次未传播厚度不确定度。",
    "substrate_lossless_assumption": "基片k=0来自您明确选择的无损假设，不是实测吸收数据。",
    "semi_infinite_substrate": "已选半无限基片：忽略背面与基片传播；T只表示进入基片的通量。",
    "coherent_substrate_resolution": "全相干基片按原波长保留条纹；未模拟仪器分辨率、平均或部分相干。",
    "reflectance_only": "强警告：仅R谱通常不能充分约束n/k；建议增加独立T、角度/偏振及膜厚核验。",
    "transmittance_only": "强警告：仅T谱通常不能唯一识别n/k；建议增加独立R及角度/偏振测量。",
    "normal_incidence_ambiguity": "只有正入射数据，即使R+T也可能存在多解；斜入射能增加约束但不保证唯一。",
    "ill_conditioned": "强警告：局部参数灵敏度退化或病态，结果对误差敏感，不能当作独立实测常数。",
    "multiple_similar_solutions": "多个起点得到相近残差但不同参数，存在已发现的近似等价解；并非完整置信区间。",
    "poor_fit": "强警告：残差超过阈值。请检查模型、膜厚、基片/背面、测量校准与光谱分辨率。",
    "parameter_at_bound": "参数触及边界，结果可能受所设参数域支配；未触界也不能证明唯一。",
    "transparent_model_assumption": "Cauchy透明模型将k固定为0；这是假设，不是无吸收证明，也不保证一般吸收/因果一致性。",
    "limited_lorentz_model": "单Lorentz振子+ε∞仅是受限模型，不包含多跃迁、带隙、粗糙、渐变或各向异性。",
    "model_assumption": "结果依赖所选色散模型和固定结构，不能仅凭拟合优度验证模型。",
    "solver_not_converged": "强警告：最佳起点达到数值预算仍未收敛，返回值只是未完成估计。",
    "multistart_incomplete": "部分起点失败或耗尽预算，多起点结果不完整，不保证全局最优。",
    "spectral_gaps": "测量波长存在较大空缺；其间的模型插值没有独立验证。",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        raise ValueError(label + "必须填写有限数值")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(label + "必须填写有限数值") from exc
    if not math.isfinite(result):
        raise ValueError(label + "必须填写有限数值")
    return result


def _table(text: Any) -> tuple[list[float], list[float]]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_FORM_BYTES:
        raise ValueError("测量表超过大小限制")
    waves: list[float] = []
    values: list[float] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cells = next(csv.reader([line])) if "," in line else line.split()
        if not waves and [value.strip().casefold() for value in cells] in (
                ["wavelength", "r"], ["wavelength", "t"], ["wavelength", "value"]):
            continue
        if len(cells) != 2:
            raise ValueError(f"测量表第{number}行须为波长、数值两列")
        waves.append(_number(cells[0], f"第{number}行波长"))
        values.append(_number(cells[1], f"第{number}行测量值"))
        if len(waves) > MAX_MEASUREMENT_SAMPLES:
            raise ValueError("单条测量谱最多4096点")
    if len(waves) < 3:
        raise ValueError("每条测量谱至少需要三个采样点")
    return waves, values


def request_from_form(form: dict[str, Any]) -> dict[str, Any]:
    """Turn explicitly labelled UI values into the core request; no unit guessing."""
    if not isinstance(form, dict) or form.get("absolute_specular") is not True:
        raise ValueError("请确认输入为绝对镜面反射/透射谱，非相对量、漫反射或任意强度")
    spectra = form.get("spectra")
    if not isinstance(spectra, list) or not 1 <= len(spectra) <= 8:
        raise ValueError("请提供1至8条测量谱")
    rows = []
    for index, row in enumerate(spectra):
        if not isinstance(row, dict):
            raise ValueError("测量谱格式无效")
        waves, values = _table(row.get("text"))
        source = row.get("source", "")
        if not isinstance(source, str) or not source.strip() or len(source) > 1000:
            raise ValueError("每条测量谱都须填写测量来源/文件名")
        rows.append({"id": "measurement_" + str(index + 1), "quantity": row.get("quantity"),
            "wavelengths": waves, "values": values, "wavelength_unit": row.get("wavelength_unit"),
            "value_unit": row.get("value_unit"), "angle_deg": _number(row.get("angle_deg"), "入射角"),
            "polarization": row.get("polarization"), "measurement": "absolute_specular",
            "transmission_definition": ("into_substrate" if form.get("backside") == "semi_infinite"
                                        else "external"), "source": source})
    model = form.get("model")
    if model == "cauchy" and form.get("transparent_region") is not True:
        raise ValueError("Cauchy模式必须显式确认本波段透明并固定k=0，这是模型假设")
    backside = form.get("backside")
    if backside not in {"incoherent", "coherent", "semi_infinite"}:
        raise ValueError("请明确选择基片背面/相干模型")
    if backside == "semi_infinite" and any(row["quantity"] == "T" for row in rows):
        if form.get("into_substrate_confirmed") is not True:
            raise ValueError("半无限基片T仅表示进入基片的通量，不能使用全玻璃片外部透射率")
    known_lossless = form.get("substrate_lossless") is True
    k_value = form.get("substrate_k")
    substrate = {"n": _number(form.get("substrate_n"), "基片n"),
                 "k": None if k_value in (None, "") else _number(k_value, "基片k"),
                 "assume_lossless": known_lossless}
    if known_lossless and substrate["k"] not in (None, 0):
        raise ValueError("基片非零k与显式无损假设冲突")
    if substrate["k"] is None and not known_lossless:
        raise ValueError("基片k未知；请提供已知k或显式声明已知无损假设，不能自动填零")
    if backside != "semi_infinite":
        substrate["thickness_nm"] = _number(form.get("substrate_thickness_nm"), "基片厚度（nm）")
    request = {"model": model, "transparent_region": form.get("transparent_region") is True,
        "film_thickness_nm": _number(form.get("film_thickness_nm"), "已知膜厚（nm）"),
        "substrate": substrate, "geometry": {"backside": backside, "incident_n": 1.0, "exit_n": 1.0},
        "spectra": rows, "optimization": {"multistarts": 4, "max_nfev": 250, "seed": 0,
                                         "acceptance_rmse": 0.01}}
    return request


def fit_optical_constants(request, *, cancelled=None, progress=None):
    """Lazy seam keeps numerical imports and compute off material-library startup."""
    from coat_sim.core.optical_inversion import fit_optical_constants as fit
    return fit(request, cancelled=cancelled, progress=progress)


def _run(request, cancelled: Event, progress: dict, lock: Lock):
    def update(value):
        if isinstance(value, dict):
            with lock:
                progress.clear()
                progress.update(deepcopy(value))
    try:
        return fit_optical_constants(request, cancelled=cancelled.is_set, progress=update)
    finally:
        _PENDING.release()


class OpticalInversionController(QObject):
    """One material editor owns the session token, pending worker and preview."""

    changed = Signal()
    applied = Signal()

    def __init__(self, library):
        super().__init__(library)
        self._library = weakref.proxy(library)
        self._future: Future | None = None
        self._event = Event()
        self._progress: dict = {}
        self._progress_lock = Lock()
        self._context: tuple | None = None
        self._session = False
        self._result: dict = {}
        self._receipt: dict = {}
        self._status = "填写已知结构与实测谱；拟合只生成预览。"
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._poll)
        library.changed.connect(self._check_context)
        library.draftChanged.connect(self._check_context)
        event = self._event
        self.destroyed.connect(lambda *_: event.set())

    def _identity(self):
        return (self._library._draft.get("id"), self._library.opticalSetDataRevision,
                self._library._load_token)

    def _editable(self):
        return not (self._library.loading or self._library.readOnly
                    or getattr(self._library, "_editing_frozen", False) or self._library.builtin)

    @Property(bool, notify=changed)
    def busy(self):
        return self._future is not None and not self._future.done()

    @Property(bool, notify=changed)
    def canApply(self):
        return bool(self._result and self._session and not self.busy and self._editable()
                    and self._context == self._identity())

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property("QVariantMap", notify=changed)  # type: ignore[arg-type]  # Qt accepts named metatypes.
    def preview(self) -> dict[str, Any]:
        # Full JSON stays available for explicit export; QML only gets plotted fields.
        return deepcopy({key: value for key, value in self._result.items()
                         if key in {"model", "parameters", "nk", "spectra", "rmse", "warnings", "diagnostics"}})

    @Property(str, notify=changed)
    def qualityText(self):
        if not self._result:
            return ""
        result = self._result
        warnings = result.get("warnings", [])
        diagnostics = result.get("diagnostics", {})
        starts = diagnostics.get("multistarts", [])
        return ("RMSE（比例值）=" + str(result.get("rmse")) + "\n参数：" + _json(result.get("parameters", {}))
                + "\n触界参数：" + (", ".join(diagnostics.get("bound_hits", [])) or "无")
                + "；局部秩=" + str(diagnostics.get("jacobian_rank", "未返回"))
                + "；条件数=" + str(diagnostics.get("condition_number", "未返回/退化"))
                + "\n多起点：" + _json(starts)
                + "\n参数差异（按边界归一化）：" + _json(diagnostics.get("normalized_parameter_spread", {}))
                + "\n" + "\n".join(_WARNING_TEXT.get(str(row.get("code", "")), str(row.get("message", row)))
                                     if isinstance(row, dict) else str(row) for row in warnings)
                + "\n低残差不代表唯一解；结果是给定模型和固定结构下的估计，不是独立实测n/k。")

    @Property(str, notify=changed)
    def reportReference(self):
        return self._receipt.get("uri", "")

    @Slot()
    def beginSession(self):
        self.invalidate()
        self._session = True
        self._context = self._identity()
        self._status = "填写已知结构与实测谱；拟合只生成预览。"
        self.changed.emit()

    @Slot()
    def invalidate(self):
        self._event.set()
        self._session = False
        self._context = None
        self._result = {}
        self._receipt = {}
        self._status = "已取消；已有材料及数据集未改变。"
        self.changed.emit()

    @Slot()
    def inputsChanged(self):
        if self.busy:
            self._event.set()
            self._result, self._receipt = {}, {}
            self._status = "输入已改变，正在取消旧拟合；结束后请重新拟合。"
            self.changed.emit()
        elif self._result:
            self._result, self._receipt = {}, {}
            self._status = "输入已修改，请重新拟合后预览。"
            self.changed.emit()

    def _check_context(self):
        if self._session and (self._context != self._identity() or not self._editable()):
            self.invalidate()
            self._status = "材料、数据集或编辑状态已改变，旧拟合已失效。"
            self.changed.emit()

    @Slot(str, result=bool)
    def startFit(self, form_json):
        if self.busy or not self._session or not self._editable() or self._context != self._identity():
            self._status = "当前材料不可编辑或旧任务尚在取消，请重新打开后重试。"
            self.changed.emit()
            return False
        self._result, self._receipt = {}, {}
        try:
            if len(form_json.encode("utf-8")) > MAX_FORM_BYTES:
                raise ValueError("输入超过2 MiB限制")
            request = request_from_form(json.loads(form_json))
        except (ValueError, TypeError, UnicodeError, csv.Error) as exc:
            self._status = "输入无效：" + str(exc)
            self.changed.emit()
            return False
        if not _PENDING.acquire(blocking=False):
            self._status = "拟合任务已达并发上限，请稍后重试。"
            self.changed.emit()
            return False
        self._result, self._receipt = {}, {}
        self._event = Event()
        event = self._event
        self.destroyed.connect(lambda *_: event.set())
        self._progress = {}
        self._status = "后台拟合中；原始波长逐点前向计算，多起点有界搜索。"
        try:
            self._future = _EXECUTOR.submit(_run, deepcopy(request), event, self._progress, self._progress_lock)
        except Exception:
            _PENDING.release()
            raise
        self._timer.start()
        self.changed.emit()
        return True

    def _poll(self):
        future = self._future
        if future is None:
            self._timer.stop()
            return
        if not future.done():
            return
        self._future = None
        self._timer.stop()
        try:
            result = future.result()
            if not self._session or self._event.is_set() or self._context != self._identity() or not self._editable():
                self.changed.emit()
                return
            if not isinstance(result, dict) or not result.get("ok"):
                self._status = "拟合未完成：" + str(result.get("message") or result.get("status") or result.get("error"))
            else:
                raw = _json(result).encode("utf-8")
                if len(raw) > MAX_REPORT_BYTES:
                    raise ValueError("完整拟合报告超过8 MiB，无法无损保留来源")
                self._result = deepcopy(result)
                self._status = "预览已就绪。请检查回代、残差、n/k及质量警告，再另存新数据集。"
        except Exception as exc:
            self._status = "拟合失败：" + str(exc)
            self._result = {}
        self.changed.emit()

    @Slot(str, result=str)
    def readMeasurementFile(self, url):
        try:
            parsed = QUrl(url)
            if not parsed.isLocalFile():
                raise ValueError("请选择本地UTF-8文本文件")
            path = Path(parsed.toLocalFile())
            reject_links(path)
            if not path.is_file() or path.stat().st_size > MAX_FORM_BYTES:
                raise ValueError("测量文件必须为不超过2 MiB的普通文件")
            text = path.read_text(encoding="utf-8-sig")
            _table(text)
            return _json({"ok": True, "text": text, "source": path.name})
        except (ValueError, OSError, UnicodeError, csv.Error) as exc:
            return _json({"ok": False, "message": str(exc)})

    @Slot(result=bool)
    def copyReport(self):
        if not self.canApply:
            return False
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return False
        clipboard.setText(_json(self._result))
        self._status = "完整拟合JSON已复制；这不会保存或修改材料。"
        self.changed.emit()
        return True

    @Slot(str, result=bool)
    def exportReport(self, url):
        if not self.canApply:
            return False
        temporary = None
        try:
            parsed = QUrl(url)
            if not parsed.isLocalFile():
                raise ValueError("请选择本地JSON报告路径")
            path = Path(parsed.toLocalFile()).absolute()
            reject_links(path)
            if path.suffix.casefold() != ".json" or not path.parent.is_dir():
                raise ValueError("请选择已有目录内的新.json文件名")
            if path.exists():
                raise ValueError("报告文件已存在；请另选新文件名，不覆盖已有文件")
            raw = (_json(self._result) + "\n").encode("utf-8")
            fd, name = tempfile.mkstemp(prefix=".optical-inversion-", suffix=".tmp", dir=path.parent)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            reject_links(path)
            os.link(temporary, path, follow_symlinks=False)
            temporary.unlink()
            temporary = None
            self._receipt = {"uri": QUrl.fromLocalFile(str(path)).toString(),
                             "sha256": hashlib.sha256(raw).hexdigest()}
            self._status = "完整报告已导出。外部报告需随材料手动保留，不会自动上传或复制到其它工程。"
            self.changed.emit()
            return True
        except (ValueError, OSError) as exc:
            self._status = "报告未导出：" + str(exc)
            self.changed.emit()
            return False
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @Slot(str, str, result=bool)
    def applyAsNew(self, name, user_notes=""):
        if not self.canApply:
            self._status = "拟合预览已失效；没有添加数据集。"
            self.changed.emit()
            return False
        try:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("请为新的反演数据集命名")
            result = deepcopy(self._result)
            source = {"kind": "measured_spectrum_inversion", "evidence_type": "model_estimate",
                "unique_solution": False, "request": result["request"],
                "formula": result.get("formula"), "bounds": result.get("bounds"),
                "parameter_units": result.get("parameter_units"), "structure": result.get("structure")}
            notes = {"user_notes": user_notes, "parameters": result.get("parameters"),
                "model_note": (result.get("optical_set") or {}).get("notes"),
                "converged": result.get("converged"), "weighted_rmse": result.get("weighted_rmse"),
                "rmse_unit": result.get("rmse_unit"),
                "rmse": result.get("rmse"), "warnings": result.get("warnings"),
                "diagnostics": result.get("diagnostics"), "spectra": result.get("spectra")}
            source_text, notes_text = _json(source), _json(notes)
            if len(source_text) > 20000 or len(notes_text) > 20000:
                if not self._receipt:
                    raise ValueError("完整来源或备注超过20000字符。请先显式导出完整JSON报告，再添加引用；不会截断原谱。")
                report_path = Path(QUrl(self._receipt["uri"]).toLocalFile())
                reject_links(report_path)
                if (not report_path.is_file() or report_path.stat().st_size > MAX_REPORT_BYTES + 1
                        or hashlib.sha256(report_path.read_bytes()).hexdigest() != self._receipt["sha256"]):
                    raise ValueError("已导出报告缺失或已改变，请重新导出完整报告")
                request = result["request"]
                source_text = _json({"kind": "measured_spectrum_inversion", "evidence_type": "model_estimate",
                    "unique_solution": False, "report": self._receipt,
                    "model": request.get("model"), "formula": result.get("formula"),
                    "bounds": result.get("bounds"), "parameter_units": result.get("parameter_units"),
                    "film_thickness_nm": request.get("film_thickness_nm"),
                    "substrate": request.get("substrate"), "geometry": request.get("geometry"),
                    "spectra": [{key: value for key, value in row.items() if key not in {"wavelengths", "values", "sigma"}}
                                for row in request.get("spectra", [])]})
                notes_text = _json({key: value for key, value in notes.items() if key != "spectra"})
            if len(source_text) > 20000 or len(notes_text) > 20000:
                raise ValueError("来源/质量摘要仍超过20000字符，不能无损添加；完整导出报告仍保留")
            record = deepcopy(result.get("optical_set") or {})
            if not record:
                nk = result["nk"]
                record = {"kind": "tabulated", "wavelength_unit": "nm", "wavelengths": nk["wavelength_nm"],
                          "n": nk["n"], "k": nk["k"], "wavelength_min": min(nk["wavelength_nm"]),
                          "wavelength_max": max(nk["wavelength_nm"])}
            record.update(id=uuid4().hex, name=name.strip(), source=source_text, notes=notes_text)
            record = optical_constants.validate_optical_set(record)
            form = {key: value for key, value in record.items() if key not in {"wavelengths", "n", "k"}}
            form["table_rows"] = [{"wavelength": str(wave), "n": str(n), "k": str(k)}
                                  for wave, n, k in zip(record["wavelengths"], record["n"], record["k"], strict=True)]
            if not self._library.addOpticalSet(_json(form)):
                raise ValueError(self._library.status)
            self._result = {}
            self._session = False
            self._status = "已作为新数据集加入材料草稿；保存材料后才正式写盘。"
            self.changed.emit()
            self.applied.emit()
            return True
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self._status = "没有添加数据集：" + str(exc)
            if self._receipt:
                self._status += " 已导出报告保留于" + self._receipt["uri"]
            self.changed.emit()
            return False
