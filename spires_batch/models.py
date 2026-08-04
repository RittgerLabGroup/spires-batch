"""Versioned request, plan, execution, and reservation models."""

from __future__ import annotations

import re
from datetime import date as Date
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = 1
STAGE_ORDER = {
    "build_r0": 0,
    "invert": 1,
    "albedo": 2,
    "interpolate": 3,
}
PRODUCT_BY_SENSOR_PLATFORM = {
    ("viirs", "snpp"): "vnp09ga",
    ("viirs", "noaa20"): "vj109ga",
    ("viirs", "noaa21"): "vj209ga",
    ("modis", "terra"): "mod09ga",
    ("modis", "aqua"): "myd09ga",
}
PLATFORM_ALIASES = {
    "npp": "snpp",
    "suomi-npp": "snpp",
    "suominpp": "snpp",
    "noaa-20": "noaa20",
    "jpss-1": "noaa20",
    "jpss1": "noaa20",
    "noaa-21": "noaa21",
    "jpss-2": "noaa21",
    "jpss2": "noaa21",
}
PRODUCT_TO_SENSOR_PLATFORM = {
    product: sensor_platform
    for sensor_platform, product in PRODUCT_BY_SENSOR_PLATFORM.items()
}


class FrozenModel(BaseModel):
    """Strict immutable base for every serialized batch model."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        use_enum_values=False,
        validate_default=True,
    )


class Stage(str, Enum):
    BUILD_R0 = "build_r0"
    INVERT = "invert"
    ALBEDO = "albedo"
    INTERPOLATE = "interpolate"


class MetadataCheck(str, Enum):
    NONE = "none"
    SAMPLE = "sample"
    ALL = "all"


class ExistingFileHandling(str, Enum):
    WRITE_NEW_FILE = "write_new_file"
    UPDATE_ATOMICALLY = "update_atomically"


class ExistingOutputPolicy(str, Enum):
    ERROR = "error"
    REUSE_VALID = "reuse_valid"


class ProductContents(str, Enum):
    FULL = "full"
    RESULTS_SUBSET = "results_subset"


class R0Mode(str, Enum):
    EXISTING = "existing"
    BUILD = "build"


class R0Recipe(str, Enum):
    VIIRS_SUMMER_COMPOSITE = "viirs_summer_composite"
    MODIS_SUMMER_COMPOSITE = "modis_summer_composite"


class InputRole(str, Enum):
    REFLECTANCE = "reflectance"
    R0 = "r0"
    R0_SOURCE = "r0_source"
    RAW = "raw"
    INTERPOLATED = "interpolated"
    ANCILLARY = "ancillary"
    LUT = "lut"
    MASK = "mask"


class StagingVerification(str, Enum):
    STAT = "stat"
    SHA256 = "sha256"


class TaskStatus(str, Enum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    LOADED_EXISTING = "loaded_existing"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    MISSING = "missing"


class FailureClass(str, Enum):
    DETERMINISTIC = "deterministic"
    TRANSIENT = "transient"
    CANCELLED = "cancelled"


class CheckLayer(str, Enum):
    SCHEMA = "schema"
    SEMANTIC = "semantic"
    INVENTORY = "inventory"
    METADATA = "metadata"


class CheckSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ReservationState(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


class SubmissionStatus(str, Enum):
    PREPARED = "prepared"


def _normalize_token(value: str) -> str:
    return value.strip().lower().replace("_", "").replace(" ", "")


class RunConfig(FrozenModel):
    name: str = Field(min_length=1, max_length=128)
    sensor: str
    platform: str
    product: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_identity(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        sensor = _normalize_token(str(normalized.get("sensor", "")))
        platform = str(normalized.get("platform", "")).strip().lower()
        platform = PLATFORM_ALIASES.get(platform, platform.replace("_", "").replace("-", ""))
        product = normalized.get("product")
        if product is not None:
            product = _normalize_token(str(product))
            inferred = PRODUCT_TO_SENSOR_PLATFORM.get(product)
            if inferred is not None:
                inferred_sensor, inferred_platform = inferred
                if sensor and sensor != inferred_sensor:
                    raise ValueError(
                        f"product {product!r} belongs to sensor {inferred_sensor!r}, "
                        f"not {sensor!r}"
                    )
                if platform and platform != inferred_platform:
                    raise ValueError(
                        f"product {product!r} belongs to platform {inferred_platform!r}, "
                        f"not {platform!r}"
                    )
                sensor = sensor or inferred_sensor
                platform = platform or inferred_platform
        expected = PRODUCT_BY_SENSOR_PLATFORM.get((sensor, platform))
        if expected is None:
            supported = ", ".join(
                f"{item_sensor}/{item_platform}"
                for item_sensor, item_platform in PRODUCT_BY_SENSOR_PLATFORM
            )
            raise ValueError(
                f"unsupported sensor/platform combination {sensor!r}/{platform!r}; "
                f"supported combinations are {supported}"
            )
        if product is not None and product != expected:
            raise ValueError(
                f"product {product!r} does not match {sensor}/{platform}; expected {expected!r}"
            )
        normalized["sensor"] = sensor
        normalized["platform"] = platform
        normalized["product"] = expected
        return normalized

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", stripped):
            raise ValueError(
                "run name must begin with an alphanumeric character and contain only "
                "letters, numbers, '.', '_', or '-'"
            )
        return stripped


class SelectionConfig(FrozenModel):
    tiles: tuple[str, ...] = ()
    water_years: tuple[int, ...] = ()
    dates: tuple[Date, ...] = ()

    @field_validator("tiles", mode="before")
    @classmethod
    def normalize_tiles(cls, value: Any) -> Any:
        if value is None:
            return ()
        normalized = tuple(str(tile).strip().lower() for tile in value)
        invalid = [tile for tile in normalized if not re.fullmatch(r"h\d{2}v\d{2}", tile)]
        if invalid:
            raise ValueError(f"invalid MODIS-grid tile(s): {invalid}")
        if len(set(normalized)) != len(normalized):
            raise ValueError("selection.tiles contains duplicates")
        return tuple(sorted(normalized))

    @field_validator("water_years", mode="before")
    @classmethod
    def normalize_water_years(cls, value: Any) -> Any:
        if value is None:
            return ()
        normalized = tuple(int(item) for item in value)
        if any(item < 1900 or item > 2200 for item in normalized):
            raise ValueError("selection.water_years must be between 1900 and 2200")
        if len(set(normalized)) != len(normalized):
            raise ValueError("selection.water_years contains duplicates")
        return tuple(sorted(normalized))

    @field_validator("dates", mode="before")
    @classmethod
    def normalize_dates(cls, value: Any) -> Any:
        if value is None:
            return ()
        normalized = tuple(value)
        if len(set(str(item) for item in normalized)) != len(normalized):
            raise ValueError("selection.dates contains duplicates")
        return tuple(sorted(normalized, key=str))


class InputFileConfig(FrozenModel):
    role: InputRole
    path: Path
    name: str | None = None
    tile: str | None = None
    date: Date | None = None
    water_year: int | None = None
    product: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tile")
    @classmethod
    def validate_tile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not re.fullmatch(r"h\d{2}v\d{2}", value):
            raise ValueError(f"invalid MODIS-grid tile {value!r}")
        return value

    @field_validator("product")
    @classmethod
    def normalize_product(cls, value: str | None) -> str | None:
        return None if value is None else _normalize_token(value)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", normalized):
            raise ValueError(
                "input name must begin with a letter and contain only lowercase "
                "letters, numbers, and underscores"
            )
        return normalized

    @model_validator(mode="after")
    def require_named_context(self) -> "InputFileConfig":
        if self.role in {InputRole.ANCILLARY, InputRole.LUT, InputRole.MASK}:
            if self.name is None:
                raise ValueError(f"{self.role.value} inputs require an explicit name")
        return self


class DiscoveryRootConfig(FrozenModel):
    adapter: str = "curc"
    role: InputRole
    path: Path
    pattern: str = "**/*"
    name: str | None = None
    required: bool = True

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("discovery pattern cannot be empty")
        return value

    @field_validator("adapter")
    @classmethod
    def validate_adapter(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("discovery adapter cannot be empty")
        return normalized

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", normalized):
            raise ValueError(
                "discovery input name must begin with a letter and contain only "
                "lowercase letters, numbers, and underscores"
            )
        return normalized

    @model_validator(mode="after")
    def require_named_context(self) -> "DiscoveryRootConfig":
        if self.role in {InputRole.ANCILLARY, InputRole.LUT, InputRole.MASK}:
            if self.name is None:
                raise ValueError(f"{self.role.value} discovery roots require a name")
        return self


class InputsConfig(FrozenModel):
    files: tuple[InputFileConfig, ...] = ()
    roots: tuple[DiscoveryRootConfig, ...] = ()

    @model_validator(mode="after")
    def require_sources(self) -> "InputsConfig":
        if not self.files and not self.roots:
            raise ValueError("inputs must contain at least one explicit file or discovery root")
        return self


class R0ArtifactConfig(FrozenModel):
    id: str | None = None
    path: Path
    tile: str | None = None
    water_year: int | None = None
    start_date: Date | None = None
    end_date: Date | None = None

    @model_validator(mode="before")
    @classmethod
    def default_identity(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if not normalized.get("id") and normalized.get("path"):
            normalized["id"] = Path(normalized["path"]).stem
        return normalized

    @field_validator("tile")
    @classmethod
    def validate_tile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not re.fullmatch(r"h\d{2}v\d{2}", value):
            raise ValueError(f"invalid MODIS-grid tile {value!r}")
        return value

    @model_validator(mode="after")
    def validate_dates(self) -> "R0ArtifactConfig":
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("R0 start_date and end_date must be supplied together")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("R0 start_date must not be later than end_date")
        return self


class R0Config(FrozenModel):
    mode: R0Mode
    recipe: R0Recipe | None = None
    artifacts: tuple[R0ArtifactConfig, ...]

    @model_validator(mode="after")
    def validate_mode(self) -> "R0Config":
        if not self.artifacts:
            raise ValueError("r0.artifacts must contain at least one explicit artifact")
        if self.mode == R0Mode.BUILD and not self.recipe:
            raise ValueError("r0.recipe is required when r0.mode is 'build'")
        if self.mode == R0Mode.EXISTING and self.recipe is not None:
            raise ValueError("r0.recipe is only valid when r0.mode is 'build'")
        paths = [str(item.path) for item in self.artifacts]
        if len(paths) != len(set(paths)):
            raise ValueError("r0.artifacts contains duplicate paths")
        return self


class ScenePreparationConfig(FrozenModel):
    bands: tuple[str, ...] | None = None
    max_sensor_zenith: float = Field(default=65.0, ge=0.0, le=90.0)
    max_solar_zenith: float = Field(default=85.0, ge=0.0, le=90.0)
    min_obs_1km: int = Field(default=1, ge=1)
    min_obs_500m: int = Field(default=1, ge=1)
    water_mask_values: tuple[int, ...] = (0, 2, 3, 4, 5, 6, 7)
    mask_water_using_reflectance_qf: bool = True
    mask_water_using_external_file: bool = True
    mask_low_reflectance_for_inversion: bool = False
    low_reflectance_threshold: float = Field(default=0.1, ge=0.0)
    cloud_mask_var: str = "mask_cloud"
    cloud_shadow_mask_var: str = "mask_cloud_shadow"
    water_mask_var: str | None = None
    ice_mask_var: str | None = None
    playa_mask_var: str | None = None

    @field_validator("bands", mode="before")
    @classmethod
    def normalize_bands(cls, value: Any) -> Any:
        if value is None:
            return None
        normalized = tuple(str(item).strip().upper() for item in value)
        if not normalized:
            raise ValueError("science.invert.preparation.bands must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError(
                "science.invert.preparation.bands contains duplicate bands"
            )
        return normalized

    @field_validator(
        "cloud_mask_var",
        "cloud_shadow_mask_var",
        "water_mask_var",
        "ice_mask_var",
        "playa_mask_var",
    )
    @classmethod
    def validate_variable_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("mask variable names must not be empty")
        return normalized


class ClusteringScienceConfig(FrozenModel):
    enabled: bool = False
    features: tuple[str, ...] = (
        "reflectance",
        "background",
        "solar_zenith",
    )
    representative_method: Literal["cluster_mean", "first_pixel"] = "cluster_mean"
    reflectance_tol: float | tuple[float, ...] = 0.02
    background_tol: float | tuple[float, ...] = 0.02
    solar_zenith_tol: float | tuple[float, ...] = 2.0
    cosine_illumination_tol: float | tuple[float, ...] = 0.02

    @field_validator("features", mode="before")
    @classmethod
    def normalize_features(cls, value: Any) -> Any:
        normalized = tuple(str(item).strip().lower() for item in value)
        supported = {
            "reflectance",
            "background",
            "solar_zenith",
            "cosine_illumination",
        }
        if not normalized:
            raise ValueError("science.invert.clustering.features must not be empty")
        unknown = sorted(set(normalized) - supported)
        if unknown:
            raise ValueError(
                f"unsupported clustering features {unknown}; "
                f"supported features are {sorted(supported)}"
            )
        if len(normalized) != len(set(normalized)):
            raise ValueError(
                "science.invert.clustering.features contains duplicates"
            )
        return normalized

    @field_validator(
        "reflectance_tol",
        "background_tol",
        "solar_zenith_tol",
        "cosine_illumination_tol",
    )
    @classmethod
    def validate_tolerance(
        cls,
        value: float | tuple[float, ...],
    ) -> float | tuple[float, ...]:
        values = (value,) if isinstance(value, (int, float)) else tuple(value)
        if not values or any(float(item) <= 0 for item in values):
            raise ValueError("clustering tolerances must be strictly positive")
        return value


class R0BuildScienceConfig(FrozenModel):
    preparation: ScenePreparationConfig = Field(
        default_factory=ScenePreparationConfig
    )
    max_sensor_zenith: float = Field(default=30.0, ge=0.0, le=90.0)
    ndvi_tie_epsilon: float = Field(default=0.02, ge=0.0)
    min_blue_reflectance: float = Field(default=0.10, ge=0.0)
    show_progress: bool = False
    chunks: dict[str, int] | None = None

    @field_validator("chunks")
    @classmethod
    def validate_chunks(cls, value: dict[str, int] | None) -> dict[str, int] | None:
        if value is not None and any(int(size) < 1 for size in value.values()):
            raise ValueError("science.build_r0.chunks values must be positive")
        return value


class InvertScienceConfig(FrozenModel):
    preparation: ScenePreparationConfig = Field(
        default_factory=ScenePreparationConfig
    )
    clustering: ClusteringScienceConfig = Field(
        default_factory=ClusteringScienceConfig
    )
    algorithm: int = Field(default=6, ge=1, le=6)
    max_eval: int | None = Field(default=None, ge=1)
    initial_grain_radius_um: float = Field(default=250.0, gt=0.0)
    apply_valid_inversion_mask: bool = True
    n_workers: int = Field(default=1, ge=1)


class AlbedoScienceConfig(FrozenModel):
    apply_canopy_correction: bool = False
    apply_ice_adjustment: bool = False
    calculate_albedo: bool = True
    calculate_delta_vis: bool = False
    calculate_radiative_forcing: bool = False
    average_vertical_crown_radius: float = Field(default=4.644, gt=0.0)
    average_horizontal_crown_radius: float = Field(default=1.72, gt=0.0)

    @model_validator(mode="after")
    def require_operation(self) -> "AlbedoScienceConfig":
        if not any(
            (
                self.apply_canopy_correction,
                self.apply_ice_adjustment,
                self.calculate_albedo,
                self.calculate_delta_vis,
                self.calculate_radiative_forcing,
            )
        ):
            raise ValueError(
                "science.albedo must enable at least one postprocessing operation"
            )
        return self


class ScienceConfig(FrozenModel):
    build_r0: R0BuildScienceConfig = Field(default_factory=R0BuildScienceConfig)
    invert: InvertScienceConfig = Field(default_factory=InvertScienceConfig)
    albedo: AlbedoScienceConfig = Field(default_factory=AlbedoScienceConfig)


class TaskScienceConfig(FrozenModel):
    build_r0: R0BuildScienceConfig | None = None
    invert: InvertScienceConfig | None = None
    albedo: AlbedoScienceConfig | None = None

    @model_validator(mode="after")
    def require_selected_science(self) -> "TaskScienceConfig":
        if not any((self.build_r0, self.invert, self.albedo)):
            raise ValueError("resolved task science must contain at least one stage")
        return self


class OutputConfig(FrozenModel):
    root: Path
    product_contents: ProductContents = ProductContents.FULL
    existing_file_handling: ExistingFileHandling = ExistingFileHandling.WRITE_NEW_FILE
    existing_output_policy: ExistingOutputPolicy = ExistingOutputPolicy.ERROR


class StagingConfig(FrozenModel):
    enabled: bool = False
    root: Path | None = None
    verification: StagingVerification = StagingVerification.STAT
    reuse_valid: bool = True

    @model_validator(mode="after")
    def validate_enabled(self) -> "StagingConfig":
        if self.enabled and self.root is None:
            raise ValueError("execution.staging.root is required when staging is enabled")
        return self


class ResourceOverrides(FrozenModel):
    cluster: str | None = None
    partition: str | None = None
    account: str | None = None
    qos: str | None = None
    time_limit: str | None = None
    cpus_per_task: int | None = Field(default=None, ge=1)
    memory: str | None = None
    environment_name: str | None = None
    extra_directives: tuple[str, ...] = ()


class ExecutionConfig(FrozenModel):
    profile: str = "blanca-snow"
    max_concurrent_tasks: int = Field(default=20, ge=1)
    max_auto_retry_count: int = Field(default=3, ge=0)
    resources: ResourceOverrides = Field(default_factory=ResourceOverrides)
    staging: StagingConfig = Field(default_factory=StagingConfig)


class PreflightConfig(FrozenModel):
    metadata_check: MetadataCheck = MetadataCheck.SAMPLE


class RequestConfig(FrozenModel):
    artifact_type: Literal["spires_batch_request"] = "spires_batch_request"
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    run: RunConfig
    selection: SelectionConfig
    steps: tuple[Stage, ...]
    inputs: InputsConfig
    r0: R0Config | None = None
    science: ScienceConfig = Field(default_factory=ScienceConfig)
    output: OutputConfig
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    preflight: PreflightConfig = Field(default_factory=PreflightConfig)

    @field_validator("steps", mode="before")
    @classmethod
    def normalize_steps(cls, value: Any) -> Any:
        if not value:
            raise ValueError("steps must contain at least one explicit scientific stage")
        normalized = tuple(str(item.value if isinstance(item, Stage) else item) for item in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("steps contains duplicate stages")
        if normalized != tuple(sorted(normalized, key=STAGE_ORDER.__getitem__)):
            raise ValueError(
                "steps must follow build_r0 -> invert -> albedo -> interpolate order"
            )
        return normalized

    @model_validator(mode="after")
    def validate_stage_relationships(self) -> "RequestConfig":
        steps = set(self.steps)
        roles = {item.role for item in self.inputs.files}
        roles.update(root.role for root in self.inputs.roots)
        named_inputs = {
            (item.role, item.name)
            for item in (*self.inputs.files, *self.inputs.roots)
            if item.name is not None
        }

        supported_names = {
            InputRole.LUT: {
                "inversion_lut",
                "albedo_lookup",
                "forcing_lookup",
            },
            InputRole.ANCILLARY: {
                "dem",
                "slope",
                "aspect",
                "skyview",
                "canopy_fraction",
                "ice_fraction",
            },
            InputRole.MASK: {
                "cloud_mask",
                "water_mask",
                "ice_mask",
                "playa_mask",
            },
        }
        for role, supported in supported_names.items():
            unknown = sorted(
                name
                for item_role, name in named_inputs
                if item_role == role and name not in supported
            )
            if unknown:
                raise ValueError(
                    f"unsupported {role.value} input name(s) {unknown}; "
                    f"supported names are {sorted(supported)}"
                )

        if Stage.INTERPOLATE in steps:
            raise ValueError(
                "Interpolation not yet implemented; the stage is reserved in schema "
                "version 1, but Phase F must define temporal windows and dependencies"
            )
        if Stage.BUILD_R0 in steps:
            if self.r0 is None or self.r0.mode != R0Mode.BUILD:
                raise ValueError(
                    "steps includes 'build_r0', so r0.mode must be 'build' with explicit "
                    "artifact output paths"
                )
            if InputRole.R0_SOURCE not in roles:
                raise ValueError(
                    "steps includes 'build_r0', but inputs has no 'r0_source' files or roots"
                )
            expected_recipe = {
                "viirs": R0Recipe.VIIRS_SUMMER_COMPOSITE,
                "modis": R0Recipe.MODIS_SUMMER_COMPOSITE,
            }[self.run.sensor]
            if self.r0.recipe != expected_recipe:
                raise ValueError(
                    f"sensor {self.run.sensor!r} requires R0 recipe "
                    f"{expected_recipe.value!r}"
                )
        elif self.r0 is not None and self.r0.mode == R0Mode.BUILD:
            raise ValueError("r0.mode is 'build', but steps does not include 'build_r0'")

        if Stage.INVERT in steps and self.r0 is None:
            raise ValueError("steps includes 'invert', but the r0 section is missing")
        if Stage.INVERT in steps and InputRole.REFLECTANCE not in roles:
            raise ValueError(
                "steps includes 'invert', but inputs has no 'reflectance' files or roots"
            )
        if (
            Stage.INVERT in steps
            and (InputRole.LUT, "inversion_lut") not in named_inputs
        ):
            raise ValueError(
                "steps includes 'invert', but inputs has no LUT named "
                "'inversion_lut'"
            )
        if Stage.ALBEDO in steps and Stage.INVERT not in steps and InputRole.RAW not in roles:
            raise ValueError(
                "steps includes standalone 'albedo', but inputs has no existing 'raw' "
                "files or roots"
            )
        if Stage.ALBEDO in steps:
            albedo = self.science.albedo
            required_context: list[tuple[InputRole, str]] = []
            if albedo.apply_canopy_correction:
                required_context.append((InputRole.ANCILLARY, "canopy_fraction"))
            if albedo.apply_ice_adjustment:
                required_context.append((InputRole.ANCILLARY, "ice_fraction"))
            if albedo.calculate_albedo:
                required_context.extend(
                    (
                        (InputRole.LUT, "albedo_lookup"),
                        (InputRole.ANCILLARY, "dem"),
                        (InputRole.ANCILLARY, "slope"),
                        (InputRole.ANCILLARY, "aspect"),
                    )
                )
            if albedo.calculate_delta_vis or albedo.calculate_radiative_forcing:
                required_context.append((InputRole.LUT, "forcing_lookup"))
            missing_context = [
                f"{role.value}:{name}"
                for role, name in required_context
                if (role, name) not in named_inputs
            ]
            if missing_context:
                raise ValueError(
                    "selected albedo operations require missing named inputs "
                    f"{missing_context}"
                )
        if (
            self.output.existing_file_handling == ExistingFileHandling.UPDATE_ATOMICALLY
            and not (Stage.ALBEDO in steps and Stage.INVERT not in steps)
        ):
            raise ValueError(
                "output.existing_file_handling='update_atomically' is initially supported "
                "only for standalone albedo enrichment of an existing raw product"
            )
        return self


class ResourceProfile(FrozenModel):
    name: str
    scheduler: Literal["slurm"] = "slurm"
    cluster: str = "blanca"
    partition: str
    account: str | None = None
    qos: str | None = None
    time_limit: str = "04:00:00"
    cpus_per_task: int = Field(default=1, ge=1)
    memory: str = "8G"
    max_concurrent_tasks: int = Field(default=20, ge=1)
    environment_name: str = "spipy14"
    extra_directives: tuple[str, ...] = ()

    @field_validator("cluster", "partition", "account", "qos")
    @classmethod
    def validate_scheduler_token(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", normalized):
            raise ValueError(
                "scheduler cluster, partition, account, and qos values must "
                "contain only letters, numbers, '.', '_', or '-'"
            )
        return normalized


class ResolvedInput(FrozenModel):
    role: InputRole
    source_path: Path
    execution_path: Path
    name: str | None = None
    tile: str | None = None
    date: Date | None = None
    water_year: int | None = None
    product: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    mtime_ns: int | None = Field(default=None, ge=0)
    source_sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExpectedOutput(FrozenModel):
    path: Path
    content: Literal["r0", "raw", "interpolate"]
    existing_file_handling: ExistingFileHandling
    existing_output_policy: ExistingOutputPolicy = ExistingOutputPolicy.ERROR
    product_contents: ProductContents | None = None

    @model_validator(mode="after")
    def validate_product_contents(self) -> "ExpectedOutput":
        if self.content == "raw" and self.product_contents is None:
            raise ValueError("raw outputs require product_contents")
        if self.content != "raw" and self.product_contents is not None:
            raise ValueError(
                "product_contents applies only to persisted raw products"
            )
        return self


class Task(FrozenModel):
    task_id: str
    stages: tuple[Stage, ...]
    sensor: str
    platform: str
    product: str
    tile: str | None = None
    date: Date | None = None
    water_year: int | None = None
    r0_id: str | None = None
    r0_recipe: R0Recipe | None = None
    inputs: tuple[ResolvedInput, ...] = ()
    outputs: tuple[ExpectedOutput, ...]
    depends_on: tuple[str, ...] = ()
    science: TaskScienceConfig
    resource_profile: str


class PreflightIssue(FrozenModel):
    layer: CheckLayer
    severity: CheckSeverity
    code: str
    message: str
    path: Path | None = None
    task_id: str | None = None


class PreflightResult(FrozenModel):
    metadata_check: MetadataCheck
    started_at: datetime
    completed_at: datetime
    issues: tuple[PreflightIssue, ...] = ()

    @property
    def passed(self) -> bool:
        return not any(issue.severity == CheckSeverity.ERROR for issue in self.issues)


class ResolvedPlan(FrozenModel):
    artifact_type: Literal["spires_batch_resolved_plan"] = "spires_batch_resolved_plan"
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    run_id: str
    manifest_family_id: str
    created_at: datetime
    request: RequestConfig
    config_digest: str
    plan_digest: str
    tasks: tuple[Task, ...]
    resource_profiles: tuple[ResourceProfile, ...]
    preflight: PreflightResult
    software_versions: dict[str, str] = Field(default_factory=dict)
    retry_of_plan_digest: str | None = None
    retry_number: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_identity(self) -> "ResolvedPlan":
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("resolved plan contains duplicate task IDs")
        output_paths = [
            str(output.path)
            for task in self.tasks
            for output in task.outputs
        ]
        if len(output_paths) != len(set(output_paths)):
            raise ValueError("resolved plan contains duplicate output paths")
        known = set(task_ids)
        for task in self.tasks:
            missing = set(task.depends_on) - known
            if missing and self.retry_of_plan_digest is None:
                raise ValueError(
                    f"task {task.task_id!r} depends on unknown task IDs {sorted(missing)}"
                )
        return self


class TaskAttempt(FrozenModel):
    task_id: str
    attempt: int = Field(ge=1)
    status: TaskStatus
    started_at: datetime | None = None
    ended_at: datetime | None = None
    failure_class: FailureClass | None = None
    failure_code: str | None = None
    message: str | None = None
    slurm_job_id: str | None = None
    slurm_array_task_id: str | None = None
    log_path: Path | None = None

    @model_validator(mode="after")
    def validate_failure(self) -> "TaskAttempt":
        if self.status == TaskStatus.FAILED and self.failure_class is None:
            raise ValueError("failed attempts require failure_class")
        if self.failure_class is not None and self.status != TaskStatus.FAILED:
            raise ValueError("failure_class is valid only for failed attempts")
        return self


class WorkflowEvent(FrozenModel):
    artifact_type: Literal["spires_batch_event"] = "spires_batch_event"
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    timestamp: datetime
    event_type: str
    run_id: str
    task_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    status: TaskStatus | None = None
    failure_class: FailureClass | None = None
    failure_code: str | None = None
    message: str | None = None
    slurm_job_id: str | None = None
    slurm_array_task_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Reservation(FrozenModel):
    artifact_type: Literal["spires_batch_reservation"] = "spires_batch_reservation"
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    reservation_id: str
    state: ReservationState
    run_id: str
    task_id: str
    user: str
    created_at: datetime
    updated_at: datetime
    config_digest: str
    manifest_family_id: str | None = None
    plan_digest: str | None = None
    submission_id: str | None = None
    output_path: Path
    slurm_cluster: str | None = None
    slurm_job_id: str | None = None
    slurm_array_task_id: str | None = None
    submission_group_id: str | None = None
    message: str | None = None


class SubmissionReadinessCheck(FrozenModel):
    code: str
    message: str
    path: Path | None = None
    task_id: str | None = None


class SubmissionReservationIntent(FrozenModel):
    reservation_id: str
    task_id: str
    output_path: Path


class SubmissionGroupRecord(FrozenModel):
    group_id: str
    task_ids: tuple[str, ...]
    dependency_group_ids: tuple[str, ...] = ()
    resource_profile: str
    script_path: Path
    script_sha256: str
    index_path: Path
    index_sha256: str
    sbatch_command_preview: str


class SubmissionRecord(FrozenModel):
    artifact_type: Literal["spires_batch_submission_record"] = (
        "spires_batch_submission_record"
    )
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    submission_id: str
    submission_digest: str
    status: Literal[SubmissionStatus.PREPARED] = SubmissionStatus.PREPARED
    created_at: datetime
    attempt: int = Field(ge=1)
    run_id: str
    manifest_family_id: str
    config_digest: str
    plan_digest: str
    manifest_path: Path
    manifest_sha256: str
    state_root: Path
    output_directory: Path
    submit_script_path: Path
    submit_script_sha256: str
    readiness_checks: tuple[SubmissionReadinessCheck, ...]
    groups: tuple[SubmissionGroupRecord, ...]
    reservation_intents: tuple[SubmissionReservationIntent, ...]

    @model_validator(mode="after")
    def validate_submission_inventory(self) -> "SubmissionRecord":
        group_ids = [group.group_id for group in self.groups]
        if not group_ids or len(group_ids) != len(set(group_ids)):
            raise ValueError("submission record requires unique Slurm group IDs")
        known_groups = set(group_ids)
        for group in self.groups:
            missing = set(group.dependency_group_ids) - known_groups
            if missing:
                raise ValueError(
                    f"submission group {group.group_id!r} depends on unknown "
                    f"group IDs {sorted(missing)}"
                )
        task_ids = [
            task_id
            for group in self.groups
            for task_id in group.task_ids
        ]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("submission groups contain duplicate task IDs")
        output_paths = [
            str(intent.output_path) for intent in self.reservation_intents
        ]
        reservation_ids = [
            intent.reservation_id for intent in self.reservation_intents
        ]
        if len(output_paths) != len(set(output_paths)):
            raise ValueError("submission record contains duplicate output paths")
        if len(reservation_ids) != len(set(reservation_ids)):
            raise ValueError("submission record contains duplicate reservation IDs")
        if not self.readiness_checks:
            raise ValueError("submission record requires readiness checks")
        return self


class ReservationSet(FrozenModel):
    artifact_type: Literal["spires_batch_reservation_set"] = (
        "spires_batch_reservation_set"
    )
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    reservation_set_digest: str
    submission_id: str
    run_id: str
    plan_digest: str
    submission_record_path: Path | None = None
    state_root: Path
    acquired_at: datetime
    reservations: tuple[Reservation, ...]

    @model_validator(mode="after")
    def validate_reservations(self) -> "ReservationSet":
        reservation_ids = [
            reservation.reservation_id for reservation in self.reservations
        ]
        if not reservation_ids or len(reservation_ids) != len(set(reservation_ids)):
            raise ValueError("reservation set requires unique reservations")
        invalid = [
            reservation.reservation_id
            for reservation in self.reservations
            if reservation.submission_id != self.submission_id
            or reservation.plan_digest != self.plan_digest
        ]
        if invalid:
            raise ValueError(
                "reservation set contains reservations for a different submission: "
                f"{invalid}"
            )
        return self


class SubmissionEvent(FrozenModel):
    artifact_type: Literal["spires_batch_submission_event"] = (
        "spires_batch_submission_event"
    )
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    timestamp: datetime
    event_type: str
    submission_id: str
    run_id: str
    plan_digest: str
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SchedulerTestGroup(FrozenModel):
    group_id: str
    cluster: str
    tested_at: datetime
    command: tuple[str, ...]
    response: str


class SchedulerTestRecord(FrozenModel):
    artifact_type: Literal["spires_batch_scheduler_test"] = (
        "spires_batch_scheduler_test"
    )
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    scheduler_test_digest: str
    tested_at: datetime
    submission_id: str
    submission_digest: str
    reservation_set_digest: str
    run_id: str
    plan_digest: str
    groups: tuple[SchedulerTestGroup, ...]

    @model_validator(mode="after")
    def validate_test_groups(self) -> "SchedulerTestRecord":
        group_ids = [group.group_id for group in self.groups]
        if not group_ids or len(group_ids) != len(set(group_ids)):
            raise ValueError("scheduler test requires unique group IDs")
        return self


class SchedulerSubmissionGroup(FrozenModel):
    group_id: str
    cluster: str
    submitted_at: datetime
    job_id: str
    raw_response: str
    command: tuple[str, ...]
    task_ids: tuple[str, ...]
    dependency_job_ids: tuple[str, ...] = ()

    @field_validator("job_id")
    @classmethod
    def validate_job_id(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"\d+", normalized):
            raise ValueError(f"invalid Slurm job ID {value!r}")
        return normalized


class SchedulerSubmissionRecord(FrozenModel):
    artifact_type: Literal["spires_batch_scheduler_submission"] = (
        "spires_batch_scheduler_submission"
    )
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    scheduler_submission_digest: str
    submitted_at: datetime
    submission_id: str
    submission_digest: str
    reservation_set_digest: str
    scheduler_test_digest: str
    run_id: str
    plan_digest: str
    groups: tuple[SchedulerSubmissionGroup, ...]

    @model_validator(mode="after")
    def validate_submission_groups(self) -> "SchedulerSubmissionRecord":
        group_ids = [group.group_id for group in self.groups]
        job_ids = [(group.cluster, group.job_id) for group in self.groups]
        if not group_ids or len(group_ids) != len(set(group_ids)):
            raise ValueError("scheduler submission requires unique group IDs")
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("scheduler submission contains duplicate cluster/job IDs")
        known_groups = set(group_ids)
        for group in self.groups:
            if not group.task_ids:
                raise ValueError(
                    f"scheduler submission group {group.group_id!r} has no tasks"
                )
        if not known_groups:
            raise ValueError("scheduler submission requires at least one group")
        return self


class OperationalRunRecord(FrozenModel):
    artifact_type: Literal["spires_batch_operational_run"] = (
        "spires_batch_operational_run"
    )
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    operational_run_id: str
    operational_run_digest: str
    created_at: datetime
    manifest_family_id: str
    config_digest: str
    plan_digest: str
    manifest_path: Path
    manifest_sha256: str
    state_root: Path
    output_directory: Path


class OperationalAdvanceRecord(FrozenModel):
    artifact_type: Literal["spires_batch_operational_advance"] = (
        "spires_batch_operational_advance"
    )
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    advance_digest: str
    advanced_at: datetime
    operational_run_id: str
    completed_wave: int = Field(ge=1)
    status: Literal[
        "retry_submitted",
        "downstream_submitted",
        "succeeded",
        "failed",
    ]
    task_ids: tuple[str, ...]
    next_wave_directory: Path | None = None
    message: str

    @model_validator(mode="after")
    def validate_transition(self) -> "OperationalAdvanceRecord":
        if not self.task_ids or len(self.task_ids) != len(set(self.task_ids)):
            raise ValueError(
                "operational advance requires unique affected task IDs"
            )
        submitted = self.status in {
            "retry_submitted",
            "downstream_submitted",
        }
        if submitted != (self.next_wave_directory is not None):
            raise ValueError(
                "submitted operational advances require a next wave and "
                "terminal advances must not have one"
            )
        if not self.message.strip():
            raise ValueError("operational advance requires a message")
        return self
