"""Public request schemas for film-stack creation and explicit table mappings.

The numerical and file validation in :mod:`film_stack` remains authoritative.
These models document the request shape without placing expanded layers in an
agent response.  File selection/parsing options belong to the import endpoint.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, TypeAdapter, model_validator

from .schemas import ProcessEnvironment
from .process_environment import model_dump_with_environment
from .film_stack import MAX_FILM_STACK_LAYERS, MAX_FILM_STACK_MATERIALS


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _FilmStackPlacement(_StrictModel):
    environment: ProcessEnvironment | None = None
    source_slot: int = Field(ge=1, le=64, description="One-based source position in the selected machine.")
    rate_nm_s: FiniteFloat | None = Field(default=None, gt=0, description="Physical deposition rate in nm/s; defaults to the selected source emission model reference rate.")
    target_mode: Literal["mean", "center", "monitor"] = "mean"
    monitor_slot: int | None = Field(default=None, ge=1, le=64,
        description="One-based monitor position, required only for target_mode=monitor.")
    mask_slots: list[Annotated[int, Field(ge=0)]] | None = Field(default=None,
        description="Zero-based correction-board positions. Null inherits installed boards; [] disables them.")
    refractive_index: FiniteFloat | None = Field(default=None, gt=0,
        description="User-supplied real refractive index at the reference wavelength; required by quarter_wave.")
    refractive_index_source: str = Field(default="", max_length=1000,
        description="User-supplied provenance of the refractive index, such as a measurement or source URL.")

    @model_validator(mode="after")
    def conditional_positions(self):
        if self.target_mode == "monitor" and self.monitor_slot is None:
            raise ValueError("target_mode=monitor 必须明确指定 monitor_slot")
        if self.target_mode != "monitor" and self.monitor_slot is not None:
            raise ValueError("仅 target_mode=monitor 可指定 monitor_slot")
        if self.mask_slots is not None and len(self.mask_slots) != len(set(self.mask_slots)):
            raise ValueError("mask_slots 位次不能重复")
        return self


class FilmStackSource(_FilmStackPlacement):
    alias: str | None = Field(default=None, min_length=1, max_length=120,
        description="Optional explicit table/sequence alias, never a material definition. If omitted, use the selected source material name, or its filename for a legacy source. Duplicate labels require distinct aliases.")


class FilmStackMaterial(_FilmStackPlacement):
    """Legacy table aliases retained without inventing material properties."""
    material: str = Field(min_length=1, max_length=120,
        description="Legacy explicit table/sequence alias only; physical material belongs to the selected saved source.")
    rate_nm_s: FiniteFloat = Field(gt=0, description="Explicit physical deposition rate in nm/s.")
    target_mode: Literal["mean", "center", "monitor"]


class FilmStackLayer(_StrictModel):
    material: str = Field(min_length=1, max_length=120, description="Selected source material label or explicitly supplied alias; never defines a material.")
    thickness_nm: FiniteFloat = Field(gt=0, le=1_000_000_000,
                                      description="Physical layer thickness in nm.")


class _FilmStackBase(_StrictModel):
    environment: ProcessEnvironment | None = None
    name: str = Field(min_length=1, max_length=100)
    machine_file: str = Field(min_length=1, max_length=4096)
    substrate_file: str = Field(min_length=1, max_length=4096)
    description: str = Field(default="", max_length=20000)
    notes: str = Field(default="", max_length=20000)
    sources: list[FilmStackSource] | None = Field(default=None, min_length=1, max_length=MAX_FILM_STACK_MATERIALS,
        description="Saved source selections in the selected machine; material and default rate derive from each source design.")
    materials: list[FilmStackMaterial] | None = Field(default=None, min_length=1, max_length=MAX_FILM_STACK_MATERIALS,
        description="Compatibility-only explicit aliases for old layer tables. Use sources for new designs.")

    @model_validator(mode="after")
    def one_binding_format(self):
        if (self.sources is None) == (self.materials is None):
            raise ValueError("须且只须指定 sources 已保存源选择或 materials 旧层表别名映射")
        return self


class ExplicitFilmStack(_FilmStackBase):
    mode: Literal["explicit"]
    layers: list[FilmStackLayer] = Field(min_length=1, max_length=MAX_FILM_STACK_LAYERS,
                                        description="Explicit layer table in deposition order, substrate to air.")


class PeriodicFilmStack(_FilmStackBase):
    mode: Literal["periodic"]
    period_layers: list[FilmStackLayer] = Field(min_length=1, max_length=MAX_FILM_STACK_LAYERS)
    repeat_count: int = Field(ge=1, le=MAX_FILM_STACK_LAYERS)
    prefix_layers: list[FilmStackLayer] = Field(default_factory=list, max_length=MAX_FILM_STACK_LAYERS)
    suffix_layers: list[FilmStackLayer] = Field(default_factory=list, max_length=MAX_FILM_STACK_LAYERS)


class EqualFilmStack(_FilmStackBase):
    mode: Literal["equal"]
    sequence: list[str] = Field(min_length=1, max_length=MAX_FILM_STACK_MATERIALS,
                                description="Selected source material labels or explicit aliases cycled in deposition order.")
    layer_count: int = Field(ge=1, le=MAX_FILM_STACK_LAYERS)
    total_thickness_nm: FiniteFloat = Field(gt=0,
                                          description="Total physical thickness; each layer is total/layer_count.")


class QuarterWaveFilmStack(_FilmStackBase):
    mode: Literal["quarter_wave"]
    sequence: list[str] = Field(min_length=1, max_length=MAX_FILM_STACK_MATERIALS,
                                description="Selected source material labels or explicit aliases cycled in deposition order.")
    layer_count: int = Field(ge=1, le=MAX_FILM_STACK_LAYERS)
    reference_wavelength_nm: FiniteFloat = Field(gt=0,
        description="Reference vacuum wavelength in nm. d=lambda/(4n) is a normal-incidence candidate only; no optical performance is certified.")


class FilmStackColumns(_StrictModel):
    material: str | int = Field(description="Exact column name or zero-based column index.")
    thickness: str | int = Field(description="Physical thickness column; optical thickness/QWOT is unsupported.")


class ImportedFilmStack(_FilmStackBase):
    mode: Literal["import"]
    columns: FilmStackColumns
    thickness_unit: Literal["nm", "um", "angstrom"]
    order: Literal["substrate_to_air", "air_to_substrate"] = Field(
        description="Explicit input table order; air_to_substrate is reversed before constructing deposition steps.")
    decimal_separator: Literal[".", ","] = "."


FilmStackSpec: TypeAlias = Annotated[
    ExplicitFilmStack | PeriodicFilmStack | EqualFilmStack | QuarterWaveFilmStack | ImportedFilmStack,
    Field(discriminator="mode"),
]


def film_stack_json_schema() -> dict:
    return TypeAdapter(FilmStackSpec).json_schema()


def film_stack_spec_data(value: dict | BaseModel) -> dict:
    """Normalize defaults, preserving explicit environment clearing overrides."""
    raw = model_dump_with_environment(value) if isinstance(value, BaseModel) else value
    return model_dump_with_environment(TypeAdapter(FilmStackSpec).validate_python(raw))
