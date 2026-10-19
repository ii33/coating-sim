"""JSON contracts for the public MCP boundary.

The core validators remain the single source of truth for physical and file
constraints.  These Pydantic models expose their normalized shapes to MCP
clients and reject malformed requests before any file write or calculation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, TypeAdapter, model_validator, model_serializer

from .machine_profiles import validate_machine_profile, validate_mask_profile
from .process_profiles import validate_process_profile
from .source_profiles import validate_source_profile
from .substrate_profiles import validate_substrate_profile
from .simulator import Settings


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecipeAlgorithmOptions(_StrictModel):
    """Execution-path controls; algorithm-specific combinations are checked by CoatService."""
    model_config = ConfigDict(extra="forbid", strict=True)
    budget_s: FiniteFloat | None = Field(default=None, gt=0, le=300,
        description="Optional wall-time stop limit in seconds; omitted/null means no time limit for offline computation")
    max_evaluations: int | None = Field(default=None, ge=1, le=100,
        description="Evaluation limit for reference/optimize; retained but ignored by legacy fast calls, which always perform one evaluation")
    tolerance_percent: FiniteFloat | None = Field(default=None, gt=0,
        description="Temporal convergence tolerance for reference; valid legacy values are ignored with a warning on other paths")
    rpm_scale_bounds: list[FiniteFloat] | None = Field(default=None, min_length=2, max_length=2,
        description="RPM multiplier bounds for optimize; valid legacy values are ignored with a warning on other paths")
    sampling_policy: Literal["automatic", "requested"] | None = None
    periodic_integration: bool | None = None

class _DesignModel(_StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str = Field(default="", max_length=20000)
    notes: str = Field(default="", max_length=20000)

    @model_serializer(mode="wrap")
    def preserve_optional_design_name(self, handler):
        value = handler(self)
        # An absent display name is valid legacy input, not a persisted null
        # name. Explicit metadata still goes through the core text validator.
        if "name" not in self.model_fields_set:
            value.pop("name", None)
        return value


class ModelProvenance(_StrictModel):
    status: Literal["unvalidated", "theoretical", "calibrated"] = "unvalidated"
    reference: str = Field(default="", max_length=20000)
    notes: str = Field(default="", max_length=20000)


class MaterialModelDefaults(_StrictModel):
    source_exponent: FiniteFloat = Field(default=1.0, ge=0, le=20)
    reference_rate_nm_s: FiniteFloat = Field(default=1.0, gt=0, le=1e9)
    rate_exponent_per_decade: FiniteFloat = Field(default=0.0, ge=-100, le=100)
    valid_rate_min_nm_s: FiniteFloat | None = Field(default=None, gt=0, le=1e9)
    valid_rate_max_nm_s: FiniteFloat | None = Field(default=None, gt=0, le=1e9)


class ResistanceModelDefaults(MaterialModelDefaults):
    active_fraction: FiniteFloat = Field(default=1.0, gt=0, le=1)
    rate_active_fraction_per_decade: FiniteFloat = Field(default=0.0, ge=-100, le=100)
    center_weight: FiniteFloat = Field(default=0.0, ge=0, le=1)


class ElectronBeamModelDefaults(MaterialModelDefaults):
    spot_sigma_mm: FiniteFloat = Field(default=3.0, gt=0, le=10000)
    rate_spot_sigma_per_decade: FiniteFloat = Field(default=0.0, ge=-100, le=100)


class ResistanceMaterialModel(_StrictModel):
    defaults: ResistanceModelDefaults = Field(default_factory=ResistanceModelDefaults)
    provenance: ModelProvenance = Field(default_factory=ModelProvenance)


class ElectronBeamMaterialModel(_StrictModel):
    defaults: ElectronBeamModelDefaults = Field(default_factory=ElectronBeamModelDefaults)
    provenance: ModelProvenance = Field(default_factory=ModelProvenance)


class OpticalProcessParameter(_StrictModel):
    name: str = Field(min_length=1, max_length=120)
    value: str = Field(default="", max_length=512)
    unit: str = Field(default="", max_length=80)


class IndependentKData(_StrictModel):
    wavelength_unit: Literal["nm", "um"]
    wavelength_min: FiniteFloat = Field(gt=0)
    wavelength_max: FiniteFloat = Field(gt=0)
    wavelengths: list[FiniteFloat] = Field(min_length=2)
    k: list[FiniteFloat] = Field(min_length=2)
    source: str = Field(default="", max_length=20000)
    notes: str = Field(default="", max_length=20000)


class _OpticalSet(_StrictModel):
    @model_validator(mode="before")
    @classmethod
    def optical_contract(cls, value):
        # Validate original JSON values before Pydantic can coerce bools or
        # supply an implicit wavelength unit. Core owns physical domains.
        from .optical_constants import validate_optical_set
        if isinstance(value, BaseModel):
            value = value.model_dump(exclude_unset=True)
        return validate_optical_set(value)

    @model_serializer(mode="wrap")
    def preserve_optional_optical_fields(self, handler):
        value = handler(self)
        explicit = self.model_fields_set
        if "process_parameters" not in explicit:
            value.pop("process_parameters", None)
        if "k_data" not in explicit:
            value.pop("k_data", None)
        return value

    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    notes: str = Field(default="", max_length=20000)
    source: str = Field(default="", max_length=20000)
    wavelength_unit: Literal["nm", "um"]
    wavelength_min: FiniteFloat = Field(gt=0)
    wavelength_max: FiniteFloat = Field(gt=0)
    process_parameters: list[OpticalProcessParameter] | None = Field(default=None, max_length=128)


class TabulatedOpticalSet(_OpticalSet):
    kind: Literal["tabulated"]
    wavelengths: list[FiniteFloat] = Field(min_length=2)
    n: list[FiniteFloat] = Field(min_length=2)
    k: list[FiniteFloat] | None = None


class CauchyParameters(_StrictModel):
    A: FiniteFloat
    B: FiniteFloat
    C: FiniteFloat


class SellmeierParameters(_StrictModel):
    B1: FiniteFloat = Field(ge=0)
    B2: FiniteFloat = Field(ge=0)
    B3: FiniteFloat = Field(ge=0)
    C1: FiniteFloat = Field(ge=0)
    C2: FiniteFloat = Field(ge=0)
    C3: FiniteFloat = Field(ge=0)


class _DispersionOpticalSet(_OpticalSet):
    k_data: IndependentKData | None = None


class CauchyOpticalSet(_DispersionOpticalSet):
    kind: Literal["cauchy"]
    parameters: CauchyParameters


class SellmeierOpticalSet(_DispersionOpticalSet):
    kind: Literal["sellmeier"]
    parameters: SellmeierParameters


class RiiFormulaOpticalSet(_DispersionOpticalSet):
    kind: Literal["rii_formula"]
    formula_number: Literal[1, 2, 3, 4, 5, 6, 7, 8, 9]
    coefficients: list[FiniteFloat] = Field(min_length=1, max_length=17)


OpticalConstantSet: TypeAlias = Annotated[
    TabulatedOpticalSet | CauchyOpticalSet | SellmeierOpticalSet | RiiFormulaOpticalSet,
    Field(discriminator="kind"),
]


class MaterialRecord(_DesignModel):
    optical_only: bool | None = Field(default=None, strict=True, description="Explicit optical-only state; no process-source models or inferred process defaults.")
    category: Literal["film", "substrate", "other"] | None = None
    optical_constants: list[OpticalConstantSet] | None = Field(default=None, max_length=100)
    research_data: dict[str, Any] | None = Field(default=None, description="Immutable per-material research evidence; does not enable a physics solver.")
    roles: list[Literal["film", "substrate", "medium", "component", "element", "boat"]] | None = None
    reference_data: dict[str, Any] | None = Field(default=None, description="Immutable, sourced and versioned material reference record; known dataset validation is authoritative.")
    schema_version: Literal[1] = 1
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=120)
    resistance_model: ResistanceMaterialModel | None = Field(default_factory=ResistanceMaterialModel)
    electron_beam_model: ElectronBeamMaterialModel | None = Field(default_factory=ElectronBeamMaterialModel)

    @model_validator(mode="before")
    @classmethod
    def explicit_optical_only_contract(cls, value):
        if isinstance(value, BaseModel):
            value = value.model_dump(exclude_unset=True)
        if isinstance(value, dict) and "optical_only" in value:
            # Check the supplied state before typed model factories can insert
            # process defaults or coerce the explicit flag.
            from .materials import validate_material_profile
            return validate_material_profile(value)
        return value

    @model_validator(mode="after")
    def material_contract(self):
        from .materials import validate_material_profile
        omitted = {"optical_only"} if self.optical_only is None else set()
        validate_material_profile(self.model_dump(exclude=omitted))
        return self

    @model_serializer(mode="wrap")
    def material_serialization(self, handler):
        result = handler(self)
        if self.optical_only is None:
            result.pop("optical_only", None)
        return result


class EmissionCommon(_StrictModel):
    quadrature_order: int = Field(default=9, ge=3, le=21, description="Odd source quadrature order; increase to check source-sampling convergence.")
    reference_rate_nm_s: FiniteFloat = Field(default=1.0, gt=0, le=1e9)
    rate_exponent_per_decade: FiniteFloat = Field(default=0.0, ge=-100, le=100)
    valid_rate_min_nm_s: FiniteFloat | None = Field(default=None, gt=0, le=1e9)
    valid_rate_max_nm_s: FiniteFloat | None = Field(default=None, gt=0, le=1e9)
    provenance: ModelProvenance = Field(default_factory=ModelProvenance)

    @model_validator(mode="after")
    def emission_contract(self):
        from .source_profiles import validate_emission_model
        validate_emission_model(self.model_dump())
        return self


class UniformEmission(EmissionCommon):
    kind: Literal["uniform"]


class SourceAperture(_StrictModel):
    kind: Literal["rectangle", "disk"] = "rectangle"
    length_mm: FiniteFloat = Field(default=20.0, gt=0, le=10000)
    width_mm: FiniteFloat = Field(default=10.0, gt=0, le=10000)
    radius_mm: FiniteFloat = Field(default=5.0, gt=0, le=10000)
    offset_x_mm: FiniteFloat = Field(default=0.0, ge=-10000, le=10000)
    offset_y_mm: FiniteFloat = Field(default=0.0, ge=-10000, le=10000)
    angle_deg: FiniteFloat = 0.0


class ResistanceEmission(EmissionCommon):
    kind: Literal["resistance_boat"]
    active_fraction: FiniteFloat = Field(default=1.0, gt=0, le=1)
    rate_active_fraction_per_decade: FiniteFloat = Field(default=0.0, ge=-100, le=100)
    center_weight: FiniteFloat = Field(default=0.0, ge=0, le=1)
    apertures: list[SourceAperture] = Field(min_length=1, max_length=100)
    boat_mode: Literal["open", "covered"] = "open"
    cover_height_mm: FiniteFloat = Field(default=10.0, gt=0, le=10000)
    cover_thickness_mm: FiniteFloat = Field(default=0.0, ge=0, le=10000)


class ScanDwellPoint(_StrictModel):
    x_mm: FiniteFloat = Field(ge=-10000, le=10000)
    y_mm: FiniteFloat = Field(ge=-10000, le=10000)
    weight: FiniteFloat = Field(ge=0, le=1e12)


class ElectronBeamEmission(EmissionCommon):
    kind: Literal["electron_beam"]
    spot_sigma_mm: FiniteFloat = Field(default=3.0, gt=0, le=10000,
        description="Effective Gaussian emission sigma (not a validated beam-to-vapor-pool thermal mapping).")
    rate_spot_sigma_per_decade: FiniteFloat = Field(default=0.0, ge=-100, le=100)
    scan_pattern: Literal["point", "line", "raster", "ring", "custom"] = "point"
    scan_width_mm: FiniteFloat = Field(default=0.0, ge=0, le=10000)
    scan_height_mm: FiniteFloat = Field(default=0.0, ge=0, le=10000)
    scan_center_x_mm: FiniteFloat = Field(default=0.0, ge=-10000, le=10000)
    scan_center_y_mm: FiniteFloat = Field(default=0.0, ge=-10000, le=10000)
    scan_angle_deg: FiniteFloat = 0.0
    dwell_center_fraction: FiniteFloat = Field(default=0.0, ge=0, le=1)
    dwell_points: list[ScanDwellPoint] = Field(default_factory=list, max_length=100)


EmissionModel: TypeAlias = Annotated[
    UniformEmission | ResistanceEmission | ElectronBeamEmission,
    Field(discriminator="kind"),
]


class SpeciesOutgoingCoefficients(_StrictModel):
    Zn: FiniteFloat | None = Field(default=None, ge=0, le=1)
    S2: FiniteFloat | None = Field(default=None, ge=0, le=1)


class SourceThermochemistry(_StrictModel):
    model_id: Literal["zns_vendor_pressure_v1", "zns_ri8301_gas_v1"]
    temperature_K: FiniteFloat | None = Field(default=None, gt=0,
        description="Vendor model:1223.15–1343.15K; RI8301 gas model:1180–1210K. Outside range gives no prediction.")
    closure: Literal["pressure_ratio", "sealed_stoichiometric_gas", "congruent_outgoing", "ideal_effusion"] | None = None
    pressure_ratio: FiniteFloat | None = Field(default=None, gt=0)
    alpha: SpeciesOutgoingCoefficients = Field(default_factory=SpeciesOutgoingCoefficients,
        description="Specified effective outgoing coefficients, not measured ZnS surface kinetics; null means unknown.")
    transmission: SpeciesOutgoingCoefficients = Field(default_factory=SpeciesOutgoingCoefficients)
    emitting_area_m2: FiniteFloat | None = Field(default=None, gt=0,
        description="For ideal/congruent effusion this is explicitly the outlet area, not charge surface area.")
    area_basis: Literal["explicit", "source_geometry"] = "explicit"

    @model_validator(mode="after")
    def thermochemistry_contract(self):
        from .material_physics import validate_thermochemistry
        validate_thermochemistry(self.model_dump())
        return self


class _SourceBase(_DesignModel):
    @model_validator(mode="before")
    @classmethod
    def source_defaults_from_material(cls, value):
        # Normalize BEFORE Pydantic fills field defaults, otherwise implicit
        # n=1 / reference-rate=1 / sigma=3 overwrite selected material settings.
        # Normalized values then survive nested workflow model_dump unchanged.
        def raw(item):
            if isinstance(item, BaseModel):
                return item.model_dump(exclude_unset=True)
            if isinstance(item, dict):
                return {key: raw(child) for key, child in item.items()}
            if isinstance(item, list):
                return [raw(child) for child in item]
            return item
        return validate_source_profile(raw(value))

    schema_version: Literal[2] = 2
    sampling_order: int | None = Field(default=None, ge=3, le=41, strict=True, description="Optional odd numerical source quadrature order; omitted preserves legacy sampling.")
    device_type: Literal["custom", "resistance_boat", "crucible", "electron_beam"] = "custom"
    source_exponent: FiniteFloat = Field(default=1.0, ge=0, le=20)
    thermochemistry: SourceThermochemistry | None = Field(default=None, description="Separate bounded literature-reference calculator inputs; never replaces process deposition rates or target normalization.")
    material: MaterialRecord | None = Field(default=None,
        description="Immutable full material-library record captured when the source is designed. Validated by the material/source core contract; never a live library reference.")
    emission_model: EmissionModel | None = Field(default=None,
        description="Configurable source spatial emission model, rate-response calibration, boat aperture or time-averaged electron-beam scan. Core validation is authoritative; not a thermal solver.")


class PointSource(_SourceBase):
    kind: Literal["point"]


class LineSource(_SourceBase):
    kind: Literal["line"]
    length_mm: FiniteFloat = Field(gt=0, le=10000)
    angle_deg: FiniteFloat = 0.0


class DiskSource(_SourceBase):
    kind: Literal["disk"]
    radius_mm: FiniteFloat = Field(gt=0, le=10000)


class AnnulusSource(_SourceBase):
    kind: Literal["annulus"]
    inner_radius_mm: FiniteFloat = Field(ge=0)
    outer_radius_mm: FiniteFloat = Field(gt=0, le=10000)


class RectangleSource(_SourceBase):
    kind: Literal["rectangle"]
    length_mm: FiniteFloat = Field(gt=0, le=10000)
    width_mm: FiniteFloat = Field(gt=0, le=10000)
    angle_deg: FiniteFloat = 0.0


SourceDesign: TypeAlias = Annotated[
    PointSource | LineSource | DiskSource | AnnulusSource | RectangleSource,
    Field(discriminator="kind"),
]


class MaskDesign(_DesignModel):
    z_mm: FiniteFloat
    outer: list[tuple[FiniteFloat, FiniteFloat]] = Field(min_length=3, max_length=500)
    holes: list[list[tuple[FiniteFloat, FiniteFloat]]] = Field(default_factory=list, max_length=30)


class SourcePlacement(_StrictModel):
    file: str = Field(min_length=1)
    x_mm: FiniteFloat
    y_mm: FiniteFloat
    z_mm: FiniteFloat = 0.0


class MaskPlacement(_StrictModel):
    file: str = Field(min_length=1)


class MonitorPlacement(_StrictModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["crystal", "optical"]
    x_mm: FiniteFloat
    y_mm: FiniteFloat
    z_mm: FiniteFloat


class MachineDesign(_DesignModel):
    schema_version: Literal[5]
    carrier_x_mm: FiniteFloat
    carrier_y_mm: FiniteFloat
    planet_offset_mm: FiniteFloat = Field(ge=0, le=10000)
    planet_tilt_deg: FiniteFloat = Field(default=0.0, ge=-80, le=80)
    substrate_z_mm: FiniteFloat = Field(gt=0, le=100000)
    sun_teeth: int = Field(ge=1, le=1000)
    planet_teeth: int = Field(ge=1, le=1000)
    gear_module_mm: FiniteFloat = Field(ge=0)
    sources: list[SourcePlacement] = Field(min_length=1, max_length=64)
    masks: list[MaskPlacement]
    monitors: list[MonitorPlacement] = Field(max_length=64)


class ProcessGasComponent(_StrictModel):
    species: str = Field(min_length=1, max_length=100)
    partial_pressure_pa: FiniteFloat | None = Field(default=None, ge=0)
    effective_cross_section_m2: FiniteFloat | None = Field(default=None, gt=0)
    flow_sccm: FiniteFloat | None = Field(default=None, ge=0, description="Recorded inlet flow; no pumping model to derive pressure.")
    cross_section_source: str | None = Field(default=None, max_length=2000)


class ProcessEnvironment(_StrictModel):
    transport_model: Literal["disabled", "ballistic_loss"] | None = None
    substrate_temperature_k: FiniteFloat | None = Field(default=None, gt=0, description="Recorded; substrate retention/re-evaporation not modeled.")
    source_temperature_k: FiniteFloat | None = Field(default=None, gt=0, description="Recorded; source thermal emission not modeled.")
    gas_temperature_k: FiniteFloat | None = Field(default=None, gt=0)
    operating_pressure_pa: FiniteFloat | None = Field(default=None, ge=0, description="Measured total absolute pressure during deposition; no automatic base-pressure addition.")
    base_pressure_pa: FiniteFloat | None = Field(default=None, ge=0, description="Predeposition base vacuum; recorded, not automatically operating residual pressure.")
    gas_components: list[ProcessGasComponent] | None = Field(default=None, max_length=32)
    effective_cross_section_source: str | None = Field(default=None, max_length=2000)
    effective_cross_section_m2: FiniteFloat | None = Field(default=None, gt=0, description="User supplied effective projectile/background collision cross section; no material default.")


class FilmLayerReference(_StrictModel):
    """Stable reference back to the selected layer in a frozen film design."""
    surface: Literal["front", "back"]
    layer_id: str = Field(min_length=1, max_length=80)


class FrozenFilmDesignSnapshot(_StrictModel):
    """Versioned, content-addressed film design snapshot embedded in a process.

    The film-design core validator owns the full nested design/material shape;
    keeping these top-level members typed here makes the process wire contract
    explicit without duplicating the film-design schema.
    """
    schema_version: Literal[1]
    kind: Literal["coat_sim.frozen_film_design"]
    design: dict[str, Any]
    settings: dict[str, Any]
    materials: list[dict[str, Any]]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FilmDesignImport(_StrictModel):
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    surface: Literal["front", "back"]
    sequence_order: Literal["substrate_outward"]
    layer_ids: list[str] = Field(min_length=1, max_length=10000)
    unexecuted_surfaces: list[Literal["front", "back"]] = Field(min_length=1, max_length=1)
    source_slots_by_material_id: dict[str, Annotated[int, Field(ge=1)]]


class ProcessStep(_StrictModel):
    environment: ProcessEnvironment | None = Field(default=None, description="Per-field overrides: omitted inherits, explicit null clears. Temperature K; pressure Pa.")
    model_config = ConfigDict(extra="forbid", json_schema_extra={
        "allOf": [{
            "if": {"properties": {"target_mode": {"const": "monitor"}}, "required": ["target_mode"]},
            "then": {"properties": {"monitor_slot": {"type": "integer", "minimum": 1}},
                     "required": ["monitor_slot"]},
            "else": {"properties": {"monitor_slot": {"type": "null"}}},
        }],
    })
    source_slot: int = Field(ge=1)
    target_nm: FiniteFloat = Field(gt=0, le=1_000_000_000)
    rate_nm_s: FiniteFloat = Field(gt=0)
    target_mode: Literal["mean", "center", "monitor"]
    monitor_slot: int | None = Field(default=None, ge=1,
        description="Only target_mode=monitor requires a one-based monitor slot. For mean/center omit or use null.")
    mask_slots: list[Annotated[int, Field(ge=0)]] | None = Field(default=None,
        description="Zero-based correction-board slots. Omitted/null inherits installed boards; [] disables all boards.")
    film_layer_ref: FilmLayerReference | None = None
    film_design_thickness_nm: FiniteFloat | None = Field(default=None, gt=0, le=1_000_000_000,
        description="Frozen physical design thickness; target_nm remains the editable process target.")

    @model_validator(mode="after")
    def film_layer_condition(self):
        if (self.film_layer_ref is None) != (self.film_design_thickness_nm is None):
            raise ValueError("film_layer_ref and film_design_thickness_nm must be provided together")
        return self

    @model_validator(mode="after")
    def monitor_condition(self):
        if self.target_mode == "monitor" and self.monitor_slot is None:
            raise ValueError("target_mode='monitor' 时 monitor_slot 必须为从 1 开始的监测器位次，不能省略或填 null")
        if self.target_mode != "monitor" and self.monitor_slot is not None:
            raise ValueError("mean/center 基准不使用 monitor_slot，请省略或填 null")
        return self


class ProcessDesign(_DesignModel):
    environment: ProcessEnvironment | None = Field(default=None, description="Process environment defaults; omitted preserves legacy vacuum-shape behavior.")
    schema_version: Literal[7]
    name: str = Field(min_length=1, max_length=100)
    machine_file: str = Field(min_length=1, max_length=4096)
    substrate_file: str = Field(min_length=1, max_length=4096)
    steps: list[ProcessStep] = Field(min_length=1, max_length=10000)
    film_design_snapshot: FrozenFilmDesignSnapshot | None = None
    film_design_import: FilmDesignImport | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_and_validate_process(cls, value):
        # Pydantic is also used directly by workflow/API callers. Run the same
        # domain validator used by design_data so snapshots, source mappings,
        # and v6 migration cannot diverge across public validation boundaries.
        if isinstance(value, Mapping):
            return validate_process_profile(value)
        return value

    @model_validator(mode="after")
    def film_design_pair(self):
        if (self.film_design_snapshot is None) != (self.film_design_import is None):
            raise ValueError("film_design_snapshot and film_design_import must be provided together")
        if self.film_design_snapshot is None and any(step.film_layer_ref is not None for step in self.steps):
            raise ValueError("film layer references require a frozen film design snapshot")
        return self


class CircleOutline(_StrictModel):
    kind: Literal["circle"]
    radius_mm: FiniteFloat = Field(gt=0, le=10000)


class RectangleOutline(_StrictModel):
    kind: Literal["rectangle"]
    width_mm: FiniteFloat = Field(gt=0)
    height_mm: FiniteFloat = Field(gt=0)


class PolygonOutline(_StrictModel):
    kind: Literal["polygon"]
    points_mm: list[tuple[FiniteFloat, FiniteFloat]] = Field(min_length=3, max_length=500)


class CurveOutline(_StrictModel):
    kind: Literal["curve"]
    control_points_mm: list[tuple[FiniteFloat, FiniteFloat]] = Field(min_length=3, max_length=100)


OutlineDesign: TypeAlias = Annotated[
    CircleOutline | RectangleOutline | PolygonOutline | CurveOutline,
    Field(discriminator="kind"),
]


class FlatSurface(_StrictModel):
    kind: Literal["flat"]


class SphericalSurface(_StrictModel):
    kind: Literal["spherical"]
    radius_mm: FiniteFloat


class AsphereSurface(_StrictModel):
    kind: Literal["asphere"]
    radius_mm: FiniteFloat
    conic_k: FiniteFloat
    a4: FiniteFloat
    a6: FiniteFloat
    a8: FiniteFloat


class ParaboloidSurface(_StrictModel):
    kind: Literal["paraboloid"]
    sag_edge_mm: FiniteFloat


SubstrateSurfaceDesign: TypeAlias = Annotated[
    FlatSurface | SphericalSurface | AsphereSurface,
    Field(discriminator="kind"),
]

SurfaceDesign: TypeAlias = Annotated[
    FlatSurface | SphericalSurface | AsphereSurface | ParaboloidSurface,
    Field(discriminator="kind"),
]


class SubstrateDesign(_DesignModel):
    material: MaterialRecord | None = None
    schema_version: Literal[1]
    name: str = Field(min_length=1, max_length=120)
    outline: OutlineDesign
    surface: SubstrateSurfaceDesign


DesignKind: TypeAlias = Literal["source", "substrate", "mask", "machine", "process"]

_DESIGN_MODELS: dict[str, Any] = {
    "source": SourceDesign,
    "substrate": SubstrateDesign,
    "mask": MaskDesign,
    "machine": MachineDesign,
    "process": ProcessDesign,
}
_DESIGN_VALIDATORS = {
    "source": validate_source_profile,
    "substrate": validate_substrate_profile,
    "mask": validate_mask_profile,
    "machine": validate_machine_profile,
    "process": validate_process_profile,
}


def design_data(kind: DesignKind, data: dict[str, Any] | BaseModel) -> dict[str, Any]:
    """Normalize exactly as the core does, then validate the public shape."""
    if kind not in _DESIGN_MODELS:
        raise ValueError(f"未知方案类型：{kind}")
    raw = data.model_dump(exclude_unset=True) if isinstance(data, BaseModel) and kind == "process" else (data.model_dump(exclude_none=True) if isinstance(data, BaseModel) else data)
    normalized = _DESIGN_VALIDATORS[kind](raw)
    TypeAdapter(_DESIGN_MODELS[kind]).validate_python(normalized)
    return normalized


def design_json_schema(kind: DesignKind) -> dict[str, Any]:
    if kind not in _DESIGN_MODELS:
        raise ValueError(f"未知方案类型：{kind}")
    return TypeAdapter(_DESIGN_MODELS[kind]).json_schema()


class SimulationSettings(_StrictModel):
    beam_attenuation_m_inv: FiniteFloat | None = Field(default=None, ge=0, description="Derived zero-collision inverse beam interaction length; recipes derive this from their environment.")
    """Full settings schema; callers may supply any subset of defaulted fields."""

    carrier_rpm: FiniteFloat = Field(default=15.0, ge=-300, le=300)
    sun_teeth: int = Field(default=120, ge=1, le=1000)
    planet_teeth: int = Field(default=36, ge=1, le=1000)
    duration_s: FiniteFloat = Field(default=1.0, gt=0, le=86400)
    time_steps: int = Field(default=600, ge=1)
    grid_size: int = Field(default=181, ge=21, strict=True)
    substrate_radius_mm: FiniteFloat = Field(default=150.0, gt=0, le=10000)
    planet_offset_mm: FiniteFloat = Field(default=312.0, ge=0, le=10000)
    substrate_z_mm: FiniteFloat = Field(default=1000.0, gt=0, le=100000)
    carrier_x_mm: FiniteFloat = 0.0
    carrier_y_mm: FiniteFloat = 0.0
    source_x_mm: FiniteFloat = 100.0
    source_y_mm: FiniteFloat = 0.0
    source_z_mm: FiniteFloat = 0.0
    source_rate_nm_s: FiniteFloat | None = Field(default=None, gt=0,
        description="Rate used only for explicit calibrated source-shape response; omitted uses the designed source reference rate.")
    source_exponent: FiniteFloat = Field(default=1.0, ge=0, le=20)
    carrier_phase_deg: FiniteFloat = 0.0
    planet_phase_deg: FiniteFloat = 0.0
    planet_tilt_deg: FiniteFloat = Field(default=0.0, ge=-80, le=80)
    gear_module_mm: FiniteFloat | None = Field(default=None, ge=0)
    substrate_surface: dict[str, Any] | None = None
    substrate_profile: dict[str, Any] | None = None


def settings_data(value: SimulationSettings | dict[str, Any]) -> dict[str, Any]:
    raw = value.model_dump() if isinstance(value, SimulationSettings) else value
    validated = SimulationSettings.model_validate(raw)
    # The core checks cross-field and geometric conditions, including finite
    # source-to-substrate height and any supplied physical substrate profile.
    settings = Settings(**validated.model_dump())
    settings.validate()
    return asdict(settings)


class MaskOptimizationBounds(_StrictModel):
    z_mm: FiniteFloat | None = Field(default=None, description="Required height for a newly designed board; installed heights remain fixed.")
    slot: int = Field(ge=0)
    x_min_mm: FiniteFloat
    x_max_mm: FiniteFloat
    y_min_mm: FiniteFloat
    y_max_mm: FiniteFloat


class MaskOptimizationObjective(_StrictModel):
    kind: Literal["uniformity", "target_map"] = "uniformity"
    target_field_nm: list[list[FiniteFloat | None]] | None = None


class MaskOptimizationOptions(_StrictModel):
    """Omitted slots select boards used by the recipe; omitted bounds use existing outlines."""
    mode: Literal["auto_design", "optimize_loaded", "fixed_plus_variable"] = "optimize_loaded"
    variable_mode: Literal["auto_design", "optimize_loaded"] = "auto_design"
    fixed_mask_slots: list[Annotated[int, Field(ge=0)]] = Field(default_factory=list, max_length=1)
    mask_slots: list[Annotated[int, Field(ge=0)]] | None = Field(default=None, min_length=1)
    mask_bounds: list[MaskOptimizationBounds] | None = Field(default=None, min_length=1)
    source_slot: int | None = Field(default=None, ge=1, le=64)
    allow_two_masks: bool = Field(default=False, description="Legacy name: explicitly permit joint optimization of any number of selected boards per source.")
    objective: MaskOptimizationObjective = Field(default_factory=MaskOptimizationObjective)
    backend: Literal["自动", "CPU", "GPU", "DirectML"] = "自动"
    fast_cycle: bool = True
    fast_cycle_residual_percent: FiniteFloat = Field(default=5, gt=0, le=100)
    search_grid_size: int | None = Field(default=None, ge=21, strict=True)
    max_evaluations: int = Field(default=48, ge=4, le=2000)
    seed_grid_points: int = Field(default=2, ge=1, le=5)
    control_points: int = Field(default=8, ge=6, le=24)
    min_radial_extent_mm: FiniteFloat = Field(default=0, ge=0)
    min_edge_length_mm: FiniteFloat = Field(default=0, ge=0)
    max_area_fraction: FiniteFloat = Field(default=1, gt=0, le=1)
    initial_radius_mm: FiniteFloat | None = Field(default=None, gt=0)


class DocumentResponse(_StrictModel):
    kind: DesignKind
    path: str
    data: dict[str, Any]
    valid: bool
    errors: list[str]


class ValidationResponse(_StrictModel):
    kind: DesignKind
    valid: bool
    data: dict[str, Any] | None
    errors: list[str]


class RunResponse(_StrictModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    result_file: str
    field_name: Literal["field", "field_nm"]
    field_unit: Literal["relative", "nm"]
    field_shape: tuple[int, int]
    elapsed_s: FiniteFloat = Field(gt=0)
    work_index: FiniteFloat = Field(ge=0)
    metadata: dict[str, Any]
    history_warning: str | None = None
    files: list[dict[str, Any]] = Field(default_factory=list)
    temporary_asset: dict[str, Any] | None = None
    asset_warning: str | None = None


class ExportResponse(_StrictModel):
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    pdf_path: str


class HistoryResponse(_StrictModel):
    records: list[dict[str, Any]]
    count: int = Field(ge=0)


class DependenciesResponse(_StrictModel):
    kind: DesignKind
    path: str
    data: dict[str, Any]
    runtime: dict[str, Any] | None
    errors: list[str]
    ok: bool


class PreviewResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    kind: DesignKind
    data: dict[str, Any]
    issues: list[str]


class SourceQuadratureResponse(_StrictModel):
    points_xy: list[tuple[FiniteFloat, FiniteFloat]]
    weights: list[FiniteFloat]
    count: int = Field(ge=1)
    unit: Literal["mm"]
    coordinate_system: Literal["source_local"]


class RadialWeightsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    radii_mm: list[FiniteFloat]
    weights_percent: list[FiniteFloat | None]
    return_carrier_revolutions: int = Field(ge=1)
    phase_sample_count: int = Field(ge=1)
    samples_per_revolution: int = Field(ge=1)
    point_count: int = Field(ge=1)
    source_sample_count: int = Field(ge=1)


class FieldWeightsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    ring_radii_mm: list[FiniteFloat]
    azimuths_deg: list[FiniteFloat]
    angular_weights_percent: list[list[FiniteFloat | None]]
    grid_x_mm: list[FiniteFloat]
    grid_y_mm: list[FiniteFloat]
    grid_weights_percent: list[list[FiniteFloat | None]]
    relative_grid_weights_percent: list[list[FiniteFloat | None]]
    return_carrier_revolutions: int = Field(ge=1)
    phase_sample_count: int = Field(ge=1)


class CenterRingResponse(_StrictModel):
    points_xy: list[tuple[FiniteFloat, FiniteFloat]]
    center_xy: tuple[FiniteFloat, FiniteFloat]
    radius_mm: FiniteFloat = Field(ge=0)
    start_xy: tuple[FiniteFloat, FiniteFloat]
    end_xy: tuple[FiniteFloat, FiniteFloat]
    full_revolution: bool
    sweep_degrees: FiniteFloat


class EstimateResponse(_StrictModel):
    estimated_peak_memory_bytes: int | None = Field(default=None, ge=0)
    available_memory_bytes: int | None = Field(default=None, ge=0)
    memory_warning: bool = False
    memory_estimate_label: str = ""
    memory_estimate_basis: str = ""
    work_index: FiniteFloat = Field(ge=0)
    estimated_s: FiniteFloat | None
    formatted: str | None
    actual_device: str | None
    sample_count: int = Field(ge=0)


class InputSchemaResponse(_StrictModel):
    kind: DesignKind | Literal["settings", "mask_optimization", "film_stack"]
    json_schema: dict[str, Any] = Field(alias="schema")


class GearResponse(_StrictModel):
    gear_module_mm: FiniteFloat = Field(ge=0)
    planet_offset_mm: FiniteFloat = Field(ge=0)


class OutlineSamplesResponse(_StrictModel):
    points_xy_mm: list[tuple[FiniteFloat, FiniteFloat]]
    point_count: int = Field(ge=1)


class SurfaceSamplesResponse(_StrictModel):
    profile_xh_mm: list[tuple[FiniteFloat, FiniteFloat]]
    point_count: int = Field(ge=1)


class TimeFieldsResponse(_StrictModel):
    time_steps: int = Field(ge=1)
    time_step_s: FiniteFloat = Field(gt=0)
    total_duration_s: FiniteFloat = Field(gt=0)


def output(model: type[BaseModel], value: dict[str, Any]) -> dict[str, Any]:
    """Validate and serialize an MCP response without inventing absent keys."""
    return model.model_validate(value).model_dump(mode="json", exclude_unset=True,
                                                   by_alias=True)
