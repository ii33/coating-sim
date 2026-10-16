"""Local successful-run history and an empirical estimate for the same device."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from statistics import median


HISTORY_PATH = Path(__file__).resolve().parents[2] / "history" / "computations.jsonl"
MAX_ESTIMATE_SAMPLES = 12
ESTIMATOR_VERSION = 2
ALGORITHMS = ("precise", "fast_cycle", "single_source")


def device_key(actual_device: str) -> str:
    """Group CPU fallback labels while preserving distinct accelerator names."""
    label = str(actual_device).strip()
    if "、" in label:
        return label  # A multi-step run may have used more than one backend.
    if label.startswith("CPU"):
        return "CPU"
    return label


def format_elapsed(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)} 分 {remainder:.0f} 秒"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours} 小时 {minutes} 分"


class CalculationHistory:
    def __init__(self, path: Path = HISTORY_PATH):
        self.path = Path(path)
        self.records = []
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            record = json.loads(line)
                            if self._valid(record):
                                self.records.append(record)
                        except (ValueError, TypeError, KeyError):
                            continue
            except OSError:
                pass

    @staticmethod
    def _valid(record) -> bool:
        legacy = (isinstance(record, dict)
                  and "algorithm" not in record
                  and "estimator_version" not in record)
        current = (isinstance(record, dict)
                   and record.get("algorithm") in ALGORITHMS
                   and type(record.get("estimator_version")) is int
                   and record["estimator_version"] == ESTIMATOR_VERSION)
        return ((legacy or current)
                and isinstance(record.get("requested_device"), str)
                and isinstance(record.get("actual_device"), str)
                and isinstance(record.get("timestamp_utc"), str)
                and isinstance(record.get("work_index"), (int, float))
                and isinstance(record.get("elapsed_s"), (int, float))
                and math.isfinite(record["work_index"])
                and math.isfinite(record["elapsed_s"])
                and record["work_index"] > 0
                and record["elapsed_s"] > 0)

    def append(self, requested_device: str, actual_device: str,
               work_index: float, elapsed_s: float, *, algorithm: str) -> dict:
        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "requested_device": str(requested_device),
            "actual_device": str(actual_device),
            "work_index": float(work_index),
            "elapsed_s": float(elapsed_s),
            "algorithm": algorithm,
            "estimator_version": ESTIMATOR_VERSION,
        }
        if not self._valid(record):
            raise ValueError("计算历史包含无效的运算量或用时")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.records.append(record)
        return record

    def recent(self, count: int = 100) -> list[dict]:
        return list(reversed(self.records[-count:]))

    def estimate(self, work_index: float, requested_device: str, *, algorithm: str):
        """Return (estimated_seconds, actual_device, sample_count), or None."""
        if (not math.isfinite(work_index) or work_index <= 0
                or algorithm not in ALGORITHMS):
            return None
        eligible = [record for record in self.records
                    if record.get("algorithm") == algorithm
                    and record.get("estimator_version") == ESTIMATOR_VERSION]
        if requested_device == "CPU":
            key = "CPU"
        elif requested_device == "GPU":
            key = "GPU (CuPy)"
        elif requested_device == "DirectML":
            latest = next((record for record in reversed(eligible)
                            if "DirectML" in record["actual_device"]), None)
            if latest is None:
                return None
            key = device_key(latest["actual_device"])
        elif requested_device == "自动":
            latest = next((record for record in reversed(eligible)
                            if record["requested_device"] == "自动"), None)
            if latest is None:
                return None
            key = device_key(latest["actual_device"])
        else:
            return None
        matches = [record for record in reversed(eligible)
                   if device_key(record["actual_device"]) == key]
        samples = matches[:MAX_ESTIMATE_SAMPLES]
        if not samples:
            return None
        seconds_per_index = median(record["elapsed_s"] / record["work_index"]
                                   for record in samples)
        predicted_s = work_index * seconds_per_index
        if not math.isfinite(predicted_s):
            return None
        return predicted_s, samples[0]["actual_device"], len(samples)
