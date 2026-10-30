"""Bounded shared archive of actual, fully scored synthesis designs.

Pareto and diversity retention are project search conventions, not feasibility
certificates. The strict full-grid champion is protected independently of the
exploration endpoint. No layer-count target or manufacturing cap is introduced.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping

import numpy as np

from .film_synthesis_surfaces import selected_rows, selected_sides


def physical_design_hash(design: Mapping[str, Any]) -> str:
    """Identity of the optical problem and ordered stack, ignoring editor labels."""
    payload = {key: deepcopy(design.get(key)) for key in (
        "layer_order", "reference_wavelength_nm", "substrate", "incident_medium",
        "exit_medium", "assume_lossless", "first_surface", "use_k")}
    for side in ("front_layers", "back_layers"):
        payload[side] = [{key: deepcopy(value) for key, value in row.items()
                          if key not in {"id", "note", "symbol", "group"}}
                         for row in design.get(side, ())]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _error_field(row, key):
    return row[key] if isinstance(row, Mapping) else getattr(row, key)


def original_window_errors(value: Mapping[str, Any], count: int) -> tuple[float, ...]:
    result = [0.] * count
    seen = set()
    for row in value["score"].get("errors", ()):
        index = int(_error_field(row, "objective_index"))
        if 0 <= index < count:
            seen.add(index)
            result[index] = max(result[index], float(_error_field(row, "normalized_violation")))
    # A missing condition is not a perfect window. Custom evaluators may omit
    # rows entirely; use the complete parent scalar rather than invent zero.
    for index in set(range(count)) - seen:
        result[index] = float(value["score"]["max_error"])
    return tuple(result)


@dataclass
class SynthesisArchiveEntry:
    physical_hash: str
    candidate: dict[str, Any]
    provenance: dict[str, Any]
    parent_hash: str | None
    operator: str
    window_errors: tuple[float, ...]
    response_signature: tuple[float, ...]
    insertion_index: int
    rank_key: tuple
    audit: dict[str, Any] | None = None
    selected_surfaces: tuple[str, ...] = ("front",)

    @property
    def design(self):
        return self.candidate["design"]

    @property
    def costs(self):
        rows = selected_rows(self, self.design)
        return (float(self.candidate["score"]["max_error"]), len(rows),
                math.fsum(float(row["thickness"]["value"]) for row in rows),
                *self.window_errors)


class SynthesisDesignArchive:
    """Retain real candidates, Pareto specialists, families and layer-scale bins.

    Capacity bounds storage only. It never prevents evaluating or growing a
    design. A retained deletion/merge winner includes its complete design and
    parent/operator provenance, not merely its score. Response signatures use
    window means/extrema and are never substituted for full-grid acceptance.
    """
    def __init__(self, objective_count: int, *, capacity: int = 32, rank=None,
                 selected_surfaces=("front",)):
        if type(capacity) is not int or capacity < 2:
            raise ValueError("archive capacity must be an integer >= 2")
        self.selected_surfaces = selected_sides(selected_surfaces)
        self.objective_count = objective_count
        self.capacity = capacity
        self.rank = rank or (lambda value: (float(value["score"]["max_error"]),
                                           float(value["score"]["weighted_rms_error"])))
        self.entries: dict[str, SynthesisArchiveEntry] = {}
        self.total_admitted = 0
        self.total_pruned = 0
        self._rotation = 0
        self.audited_champion_hash: str | None = None
        self._audited_rank: tuple | None = None

    def add(self, value, *, provenance=None, parent_hash=None, operator="evaluated"):
        maximum = float(value["score"]["max_error"])
        if not math.isfinite(maximum):
            raise ValueError("archive requires a complete finite original score")
        rows = selected_rows(self, value["design"])
        if any(float(row["thickness"]["value"]) <= 0 for row in rows):
            return None  # Exact-zero probes are directions, not physical designs.
        identity = physical_design_hash(value["design"])
        if identity in self.entries:
            return self.entries[identity]
        signature: list[float] = []
        for index in range(self.objective_count):
            arrays = [np.asarray(row["actual_values"], float) for row in value.get("responses", ())
                      if row["objective_index"] == index]
            if arrays:
                actual = np.concatenate(arrays)
                signature.extend((float(np.min(actual)), float(np.mean(actual)), float(np.max(actual))))
            else:
                signature.extend((maximum,) * 3)
        # Derivative/response matrices are deliberately not multiplied by the
        # archive capacity. Actual designs and original complete scores remain.
        score = {key: deepcopy(item) for key, item in value["score"].items()
                 if key not in {"design", "errors"}}
        score["original_condition_count"] = len(value["score"].get("errors", ()))
        candidate = {"design": deepcopy(value["design"]), "score": score}
        self.total_admitted += 1
        entry = SynthesisArchiveEntry(identity, candidate, deepcopy(provenance or {}),
            parent_hash, operator, original_window_errors(value, self.objective_count),
            tuple(signature), self.total_admitted, tuple(self.rank(value)),
            selected_surfaces=self.selected_surfaces)
        self.entries[identity] = entry
        self._trim()
        return self.entries.get(identity)

    def mark_audited(self, training_value, audited_value):
        """Attach complete independent-audit status to this exact physical design."""
        identity = physical_design_hash(training_value["design"])
        if physical_design_hash(audited_value["design"]) != identity:
            raise ValueError("archive audit must belong to the exact physical candidate")
        audit_rank = tuple(self.rank(audited_value))
        if self._audited_rank is None or audit_rank < self._audited_rank:
            self.audited_champion_hash, self._audited_rank = identity, audit_rank
        entry = self.add(training_value, provenance=dict(family="audited_candidate"), operator="independent_audit")
        if entry is not None:
            score = audited_value["score"]
            entry.audit = dict(status="complete", max_error=float(score["max_error"]),
                weighted_rms_error=float(score["weighted_rms_error"]),
                condition_count=len(score.get("errors", ())), physical_hash=identity)
        self._trim()

    @staticmethod
    def _dominates(first, second):
        return all(a <= b for a, b in zip(first.costs, second.costs, strict=True)) and any(
            a < b for a, b in zip(first.costs, second.costs, strict=True))

    def pareto_entries(self):
        entries = list(self.entries.values())
        return [entry for entry in entries if not any(self._dominates(other, entry)
                                                     for other in entries if other is not entry)]

    def _ordered_diverse(self):
        entries = sorted(self.entries.values(), key=lambda item: item.rank_key)
        if not entries:
            return []
        result, selected = [entries[0]], {entries[0].physical_hash}
        audited = self.entries.get(self.audited_champion_hash or "")
        if audited is not None and audited.physical_hash not in selected:
            result.append(audited)
            selected.add(audited.physical_hash)
        buckets: dict[tuple[Any, int], SynthesisArchiveEntry] = {}
        for entry in entries:
            family = entry.provenance.get("family", entry.operator)
            # Geometric scale classes do not prescribe any particular layer count.
            scale = len(selected_rows(self, entry.design)).bit_length()
            key = family, scale
            buckets.setdefault(key, entry)
        protected = list(buckets.values()) + self.pareto_entries()
        # Farthest-first response diversity among specialists, with deterministic
        # physical-hash tie breaking and no score-only synthetic records.
        while protected:
            remaining = [entry for entry in protected if entry.physical_hash not in selected]
            if not remaining:
                break
            def distance(entry):
                signature = np.asarray(entry.response_signature)
                return min(float(np.linalg.norm(signature - np.asarray(other.response_signature)))
                           for other in result)
            chosen = max(remaining, key=lambda entry: (distance(entry), -entry.insertion_index))
            result.append(chosen)
            selected.add(chosen.physical_hash)
            protected = remaining
        result.extend(entry for entry in entries if entry.physical_hash not in selected)
        return result

    def _trim(self):
        if len(self.entries) <= self.capacity:
            return
        retained = self._ordered_diverse()[:self.capacity]
        self.total_pruned += len(self.entries) - len(retained)
        self.entries = {entry.physical_hash: entry for entry in retained}

    def select_parents(self, count=3):
        ordered = self._ordered_diverse()
        if not ordered:
            return ()
        count = min(max(1, count), len(ordered))
        # Champion plus rotating specialists prevents one high-layer scalar
        # champion from monopolizing all future local/needle work.
        result = [ordered[0]]
        pool = ordered[1:]
        if pool:
            for offset in range(count - 1):
                result.append(pool[(self._rotation + offset) % len(pool)])
            self._rotation = (self._rotation + count - 1) % len(pool)
        return tuple(result)

    def snapshot(self):
        pareto = {entry.physical_hash for entry in self.pareto_entries()}
        return dict(capacity=self.capacity, total_admitted=self.total_admitted,
            total_pruned=self.total_pruned, retained=len(self.entries),
            audited_champion_hash=self.audited_champion_hash,
            policy="original full-grid strict champion + objective/layer/thickness Pareto + family/scale/response diversity; actual designs retained",
            entries=[dict(physical_hash=entry.physical_hash, design=deepcopy(entry.design),
                max_error=float(entry.candidate["score"]["max_error"]),
                weighted_rms_error=float(entry.candidate["score"]["weighted_rms_error"]),
                window_max_errors=list(entry.window_errors), layer_count=len(selected_rows(self, entry.design)),
                total_thickness_nm=entry.costs[2], parent_hash=entry.parent_hash,
                operator=entry.operator, provenance=deepcopy(entry.provenance), pareto=entry.physical_hash in pareto,
                audit_status="not_audited" if entry.audit is None else "complete", audit=deepcopy(entry.audit))
                for entry in sorted(self.entries.values(), key=lambda item: item.insertion_index)])
