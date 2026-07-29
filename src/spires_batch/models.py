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
    """Strict immutable base for every serialized Phase A model."""

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


class R0Mode(str, Enum):
    EXISTING = "existing"
    BUILD = "build"


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
    recipe: str | None = None
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


class ScienceConfig(FrozenModel):
    build_r0: dict[str, Any] = Field(default_factory=dict)
    invert: dict[str, Any] = Field(default_factory=dict)
    albedo: dict[str, Any] = Field(default_factory=dict)
    interpolate: dict[str, Any] = Field(default_factory=dict)


class OutputConfig(FrozenModel):
    root: Path
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
        elif self.r0 is not None and self.r0.mode == R0Mode.BUILD:
            raise ValueError("r0.mode is 'build', but steps does not include 'build_r0'")

        if Stage.INVERT in steps and self.r0 is None:
            raise ValueError("steps includes 'invert', but the r0 section is missing")
        if Stage.INVERT in steps and InputRole.REFLECTANCE not in roles:
            raise ValueError(
                "steps includes 'invert', but inputs has no 'reflectance' files or roots"
            )
        if Stage.ALBEDO in steps and Stage.INVERT not in steps and InputRole.RAW not in roles:
            raise ValueError(
                "steps includes standalone 'albedo', but inputs has no existing 'raw' "
                "files or roots"
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
    inputs: tuple[ResolvedInput, ...] = ()
    outputs: tuple[ExpectedOutput, ...]
    depends_on: tuple[str, ...] = ()
    science: dict[str, Any] = Field(default_factory=dict)
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
    output_path: Path
    slurm_job_id: str | None = None
    message: str | None = None
