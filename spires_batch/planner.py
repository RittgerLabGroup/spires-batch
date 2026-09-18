"""Resolve validated requests into immutable task manifests."""

from __future__ import annotations

import importlib.metadata
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from spires_batch.discovery import DiscoveryResult, discover_inputs, water_year
from spires_batch.models import (
    CheckLayer,
    CheckSeverity,
    CloudMaskApplicationStage,
    CloudMaskConfig,
    CloudMaskMode,
    CloudMaskSourcePolicy,
    ExistingFileHandling,
    ExistingOutputPolicy,
    ExpectedOutput,
    InputRole,
    PreflightIssue,
    RequestConfig,
    ResolvedInput,
    ResolvedPlan,
    ResourceOverrides,
    ResourceProfile,
    R0ArtifactConfig,
    R0Mode,
    R0Recipe,
    Stage,
    Task,
    TaskScienceConfig,
)
from spires_batch.preflight import PreflightFailedError, run_preflight
from spires_batch.serialization import sha256_digest


class PlanningError(ValueError):
    pass


def _uses_external_cloud_mask_pre_inversion(request: RequestConfig) -> bool:
    invert = request.science.invert
    if invert is None:
        return False
    preparation = invert.preparation
    if (
        preparation.cloud_mask_application_stage
        != CloudMaskApplicationStage.PRE_INVERSION
    ):
        return False
    if preparation.cloud_mask_source_policy in {
        CloudMaskSourcePolicy.EXTERNAL_ONLY,
        CloudMaskSourcePolicy.QA_OR_EXTERNAL,
    }:
        return True
    if preparation.cloud_mask_source_policy is not None:
        return False
    named_inputs = (
        *request.inputs.files,
        *request.inputs.roots,
    )
    return Stage.CLOUD_MASK in request.steps or any(
        item.role == InputRole.MASK and item.name == "cloud_mask"
        for item in named_inputs
    )


def _resolve_resource_profile(
    name: str,
    *,
    max_concurrent_tasks: int,
    overrides: ResourceOverrides,
) -> ResourceProfile:
    builtins = {
        "blanca-snow": {"cluster": "blanca", "partition": "blanca-snow"},
        "blanca-rittger": {"cluster": "blanca", "partition": "blanca-rittger"},
        "alpine": {"cluster": "alpine", "partition": "acpu"},
    }
    defaults = builtins.get(name)
    if defaults is None and (
        overrides.cluster is None or overrides.partition is None
    ):
        raise PlanningError(
            f"unknown resource profile {name!r}; a custom profile must "
            "provide both resource cluster and partition overrides"
        )
    return ResourceProfile(
        name=name,
        cluster=overrides.cluster or (defaults or {})["cluster"],
        partition=overrides.partition or (defaults or {})["partition"],
        account=overrides.account,
        qos=overrides.qos,
        time_limit=overrides.time_limit or "04:00:00",
        cpus_per_task=overrides.cpus_per_task or 1,
        memory=overrides.memory or "8G",
        max_concurrent_tasks=max_concurrent_tasks,
        environment_name=overrides.environment_name or "spipy14",
        extra_directives=overrides.extra_directives,
    )


def resolve_resource_profiles(request: RequestConfig) -> tuple[ResourceProfile, ...]:
    execution = request.execution
    if execution.profiles:
        profiles = tuple(
            _resolve_resource_profile(
                name,
                max_concurrent_tasks=config.max_concurrent_tasks,
                overrides=config.resources,
            )
            for name, config in sorted(execution.profiles.items())
        )
    else:
        profiles = (
            _resolve_resource_profile(
                execution.profile,
                max_concurrent_tasks=execution.max_concurrent_tasks,
                overrides=execution.resources,
            ),
        )
    clusters = {profile.cluster for profile in profiles}
    if len(clusters) != 1:
        raise PlanningError(
            "all execution profiles in one operational request must use the "
            "same Slurm cluster"
        )
    return profiles


def resolve_resource_profile(request: RequestConfig) -> ResourceProfile:
    """Resolve the single effective profile for a legacy request."""
    profiles = resolve_resource_profiles(request)
    if len(profiles) != 1:
        raise PlanningError(
            "request resolves multiple resource profiles; use "
            "resolve_resource_profiles()"
        )
    return profiles[0]


def _task_resource_profile(
    request: RequestConfig,
    tile: str | None,
    stages: tuple[Stage, ...],
) -> str:
    for stage in stages:
        if stage in request.execution.stage_profiles:
            return request.execution.stage_profiles[stage]
    if not request.execution.tile_profiles:
        return request.execution.profile
    if tile is None:
        raise PlanningError(
            "explicit tile routing cannot assign a global task without a tile"
        )
    try:
        return request.execution.tile_profiles[tile]
    except KeyError as exc:
        raise PlanningError(
            f"explicit tile routing has no profile for tile={tile}"
        ) from exc


def _absolute(path: Path, base_dir: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = base_dir / expanded
    return expanded.resolve(strict=False)


def _raw_output_path(
    request: RequestConfig,
    *,
    tile: str,
    acquisition_date: date,
    base_dir: Path,
) -> Path:
    root = _absolute(request.output.root, base_dir)
    basename = (
        f"spires_{request.run.product}_{tile}_"
        f"{acquisition_date.strftime('%Y%m%d')}_raw.nc"
    )
    return (
        root
        / request.run.sensor
        / request.run.platform
        / tile
        / basename
    )


def _cloud_mask_output_path(
    request: RequestConfig,
    *,
    tile: str,
    acquisition_date: date,
    base_dir: Path,
) -> Path:
    config = request.cloud_mask
    if config is None or config.output_root is None:
        raise PlanningError("cloud-mask output path requires cloud_mask.output_root")
    root = _absolute(config.output_root, base_dir)
    basename = (
        f"{request.run.platform}_{tile}_"
        f"{acquisition_date.strftime('%Y%m%d')}_cloud_mask.tif"
    )
    return root / tile / str(water_year(acquisition_date)) / basename


def _cloud_classification_output_path(
    request: RequestConfig,
    *,
    tile: str,
    acquisition_date: date,
    base_dir: Path,
) -> Path:
    config = request.cloud_mask
    if config is None or config.output_root is None:
        raise PlanningError("cloud classification path requires cloud_mask.output_root")
    root = _absolute(config.output_root, base_dir)
    basename = (
        f"{request.run.platform}_{tile}_"
        f"{acquisition_date.strftime('%Y%m%d')}_cloud_classification.tif"
    )
    return root / tile / str(water_year(acquisition_date)) / basename


def _task_id(payload: dict[str, Any]) -> str:
    digest = sha256_digest(payload).split(":", 1)[1][:16]
    stages = "-".join(payload["stages"])
    product = payload["product"]
    tile = payload.get("tile") or "global"
    item_date = payload.get("date") or payload.get("water_year") or "undated"
    readable = re.sub(r"[^a-z0-9_.-]+", "-", f"{stages}-{product}-{tile}-{item_date}")
    return f"{readable}-{digest}"


def _make_task(
    *,
    stages: tuple[Stage, ...],
    request: RequestConfig,
    inputs: Iterable[ResolvedInput],
    outputs: tuple[ExpectedOutput, ...],
    tile: str | None,
    acquisition_date: date | None,
    item_water_year: int | None,
    r0_id: str | None,
    r0_recipe: R0Recipe | None = None,
    depends_on: tuple[str, ...] = (),
    cloud_mask_options: CloudMaskConfig | None = None,
) -> Task:
    resolved_inputs = tuple(
        sorted(inputs, key=lambda item: (item.role.value, str(item.execution_path)))
    )
    payload = {
        "stages": [stage.value for stage in stages],
        "sensor": request.run.sensor,
        "platform": request.run.platform,
        "product": request.run.product,
        "tile": tile,
        "date": acquisition_date.isoformat() if acquisition_date else None,
        "water_year": item_water_year,
        "r0_id": r0_id,
        "r0_recipe": r0_recipe,
        "inputs": [
            {
                "role": item.role.value,
                "name": item.name,
                "source_path": str(item.source_path),
                "execution_path": str(item.execution_path),
            }
            for item in resolved_inputs
        ],
        "outputs": [
            {
                "path": str(output.path),
                "content": output.content,
                "existing_file_handling": output.existing_file_handling.value,
                "existing_output_policy": output.existing_output_policy.value,
                "product_contents": (
                    None
                    if output.product_contents is None
                    else output.product_contents.value
                ),
            }
            for output in outputs
        ],
    }
    science_values: dict[str, Any] = {}
    for stage in stages:
        if stage == Stage.CLOUD_MASK:
            if cloud_mask_options is None:
                raise PlanningError("cloud-mask task requires resolved cloud-mask options")
            science_values[stage.value] = cloud_mask_options
        else:
            options = getattr(request.science, stage.value)
            if (
                request.run.sensor == "viirs"
                and stage in (Stage.INVERT, Stage.BUILD_R0)
                and options.preparation.observation_selection is None
            ):
                options = options.model_copy(update={
                    "preparation": options.preparation.model_copy(update={
                        "observation_selection": "iobs_res_v1",
                    }),
                })
            science_values[stage.value] = options
    science = TaskScienceConfig(**science_values)
    payload["science"] = science.model_dump(mode="json", exclude_none=True)
    return Task(
        task_id=_task_id(payload),
        stages=stages,
        sensor=request.run.sensor,
        platform=request.run.platform,
        product=request.run.product,
        tile=tile,
        date=acquisition_date,
        water_year=item_water_year,
        r0_id=r0_id,
        r0_recipe=r0_recipe,
        inputs=resolved_inputs,
        outputs=outputs,
        depends_on=depends_on,
        science=science,
        resource_profile=_task_resource_profile(request, tile, stages),
    )


def _r0_input(
    artifact: R0ArtifactConfig,
    request: RequestConfig,
    base_dir: Path,
    *,
    planned: bool,
) -> ResolvedInput:
    source = _absolute(artifact.path, base_dir)
    stat = None if planned or not source.is_file() else source.stat()
    staging = request.execution.staging
    execution_path = source
    if staging.enabled and staging.root is not None and not planned:
        root = _absolute(staging.root, base_dir)
        execution_path = (
            root
            / request.run.sensor
            / request.run.platform
            / request.run.product
            / "r0"
            / (artifact.tile or "global")
            / source.name
        )
    return ResolvedInput(
        role=InputRole.R0,
        source_path=source,
        execution_path=execution_path,
        name=artifact.id,
        tile=artifact.tile,
        water_year=artifact.water_year,
        product=request.run.product,
        size_bytes=None if stat is None else stat.st_size,
        mtime_ns=None if stat is None else stat.st_mtime_ns,
        metadata={
            "r0_id": artifact.id,
            "start_date": (
                None if artifact.start_date is None else artifact.start_date.isoformat()
            ),
            "end_date": (
                None if artifact.end_date is None else artifact.end_date.isoformat()
            ),
        },
    )


def _select_r0(
    artifacts: tuple[R0ArtifactConfig, ...],
    *,
    tile: str,
    acquisition_date: date,
) -> R0ArtifactConfig:
    candidates = [
        artifact
        for artifact in artifacts
        if artifact.tile is None or artifact.tile == tile
    ]
    item_water_year = water_year(acquisition_date)
    exact = [
        artifact
        for artifact in candidates
        if artifact.water_year == item_water_year
    ]
    if exact:
        candidates = exact
    else:
        candidates = [artifact for artifact in candidates if artifact.water_year is None]
    if len(candidates) != 1:
        descriptions = [f"{item.id}:{item.path}" for item in candidates]
        raise PlanningError(
            f"expected exactly one R0 artifact for tile={tile}, "
            f"water_year={item_water_year}; candidates={descriptions}"
        )
    return candidates[0]


def _matching_context(
    inputs: tuple[ResolvedInput, ...],
    *,
    tile: str,
    acquisition_date: date,
) -> tuple[ResolvedInput, ...]:
    return tuple(
        item
        for item in inputs
        if item.role in {InputRole.ANCILLARY, InputRole.LUT, InputRole.MASK}
        and (item.tile is None or item.tile == tile)
        and (item.date is None or item.date == acquisition_date)
    )


def build_tasks(
    request: RequestConfig,
    discovery: DiscoveryResult,
    *,
    base_dir: str | Path,
) -> tuple[Task, ...]:
    base = Path(base_dir).resolve()
    tasks: list[Task] = []
    r0_task_by_path: dict[Path, str] = {}
    r0_input_by_path: dict[Path, ResolvedInput] = {}
    cloud_task_by_scene: dict[tuple[str, date], str] = {}
    cloud_input_by_scene: dict[tuple[str, date], ResolvedInput] = {}

    if request.r0 is not None:
        planned_r0 = request.r0.mode == R0Mode.BUILD
        for artifact in request.r0.artifacts:
            item = _r0_input(artifact, request, base, planned=planned_r0)
            r0_input_by_path[item.source_path] = item
            if not planned_r0 and not item.source_path.is_file():
                raise PlanningError(
                    f"configured existing R0 artifact does not exist: {item.source_path}"
                )

    if Stage.BUILD_R0 in request.steps:
        assert request.r0 is not None
        source_inputs = tuple(
            item for item in discovery.inputs if item.role == InputRole.R0_SOURCE
        )
        for artifact in request.r0.artifacts:
            output_path = _absolute(artifact.path, base)
            artifact_sources = [
                item
                for item in source_inputs
                if (artifact.tile is None or item.tile in {None, artifact.tile})
                and (
                    artifact.start_date is None
                    or item.date is None
                    or artifact.start_date <= item.date <= artifact.end_date
                )
            ]
            if not artifact_sources:
                raise PlanningError(
                    f"no r0_source inputs resolve for R0 artifact {artifact.id!r}"
                )
            task = _make_task(
                stages=(Stage.BUILD_R0,),
                request=request,
                inputs=artifact_sources,
                outputs=(
                    ExpectedOutput(
                        path=output_path,
                        content="r0",
                        existing_file_handling=ExistingFileHandling.WRITE_NEW_FILE,
                        existing_output_policy=request.output.existing_output_policy,
                    ),
                ),
                tile=artifact.tile,
                acquisition_date=None,
                item_water_year=artifact.water_year,
                r0_id=artifact.id,
                r0_recipe=request.r0.recipe,
            )
            tasks.append(task)
            r0_task_by_path[output_path] = task.task_id

    reflectance_inputs = [
        item for item in discovery.inputs if item.role == InputRole.REFLECTANCE
    ]
    if Stage.CLOUD_MASK in request.steps:
        assert request.cloud_mask is not None
        assert request.cloud_mask.model_manifest is not None
        assert request.cloud_mask.output_root is not None
        resolved_cloud_config = request.cloud_mask.model_copy(
            update={
                "model_manifest": _absolute(request.cloud_mask.model_manifest, base),
                "output_root": _absolute(request.cloud_mask.output_root, base),
            }
        )
        for reflectance in reflectance_inputs:
            if reflectance.tile is None or reflectance.date is None:
                continue
            cloud_inputs: tuple[ResolvedInput, ...] = (reflectance,)
            if request.run.sensor == "modis":
                dem_inputs = tuple(
                    item
                    for item in _matching_context(
                        discovery.inputs,
                        tile=reflectance.tile,
                        acquisition_date=reflectance.date,
                    )
                    if item.role == InputRole.ANCILLARY and item.name == "dem"
                )
                if len(dem_inputs) != 1:
                    raise PlanningError(
                        "MODIS cloud-mask tasks require exactly one DEM for "
                        f"tile={reflectance.tile}; found {len(dem_inputs)}"
                    )
                cloud_inputs = (reflectance, dem_inputs[0])
            output_path = _cloud_mask_output_path(
                request,
                tile=reflectance.tile,
                acquisition_date=reflectance.date,
                base_dir=base,
            )
            classification_path = _cloud_classification_output_path(
                request,
                tile=reflectance.tile,
                acquisition_date=reflectance.date,
                base_dir=base,
            )
            output_policy = (
                ExistingOutputPolicy.REUSE_VALID
                if request.cloud_mask.mode == CloudMaskMode.ENSURE
                else ExistingOutputPolicy.ERROR
            )
            cloud_outputs = [
                ExpectedOutput(
                    path=output_path,
                    content="cloud_mask",
                    existing_file_handling=ExistingFileHandling.UPDATE_ATOMICALLY,
                    existing_output_policy=output_policy,
                )
            ]
            if request.run.sensor == "modis":
                cloud_outputs.append(
                    ExpectedOutput(
                        path=classification_path,
                        content="cloud_classification",
                        existing_file_handling=ExistingFileHandling.UPDATE_ATOMICALLY,
                        existing_output_policy=output_policy,
                    )
                )
            task = _make_task(
                stages=(Stage.CLOUD_MASK,),
                request=request,
                inputs=cloud_inputs,
                outputs=tuple(cloud_outputs),
                tile=reflectance.tile,
                acquisition_date=reflectance.date,
                item_water_year=reflectance.water_year,
                r0_id=None,
                cloud_mask_options=resolved_cloud_config,
            )
            tasks.append(task)
            scene_key = (reflectance.tile, reflectance.date)
            cloud_task_by_scene[scene_key] = task.task_id
            cloud_input_by_scene[scene_key] = ResolvedInput(
                role=InputRole.MASK,
                source_path=output_path,
                execution_path=output_path,
                name="cloud_mask",
                tile=reflectance.tile,
                date=reflectance.date,
                water_year=reflectance.water_year,
                product=request.run.product,
                metadata={
                    "planned_output": True,
                    "producer_task_id": task.task_id,
                    "model_manifest": str(resolved_cloud_config.model_manifest),
                },
            )

    if Stage.INVERT in request.steps:
        assert request.r0 is not None
        use_external_cloud_mask = _uses_external_cloud_mask_pre_inversion(request)
        for reflectance in reflectance_inputs:
            if reflectance.tile is None or reflectance.date is None:
                continue
            artifact = _select_r0(
                request.r0.artifacts,
                tile=reflectance.tile,
                acquisition_date=reflectance.date,
            )
            artifact_path = _absolute(artifact.path, base)
            r0_input = r0_input_by_path[artifact_path]
            stages = (
                (Stage.INVERT, Stage.ALBEDO)
                if Stage.ALBEDO in request.steps
                else (Stage.INVERT,)
            )
            scene_key = (reflectance.tile, reflectance.date)
            dependencies = tuple(
                dependency
                for dependency in (
                    r0_task_by_path.get(artifact_path),
                    (
                        cloud_task_by_scene.get(scene_key)
                        if use_external_cloud_mask
                        else None
                    ),
                )
                if dependency is not None
            )
            context = _matching_context(
                discovery.inputs,
                tile=reflectance.tile,
                acquisition_date=reflectance.date,
            )
            if not use_external_cloud_mask:
                context = tuple(
                    item
                    for item in context
                    if not (item.role == InputRole.MASK and item.name == "cloud_mask")
                )
            elif scene_key in cloud_input_by_scene:
                context = tuple(
                    item
                    for item in context
                    if not (item.role == InputRole.MASK and item.name == "cloud_mask")
                ) + (cloud_input_by_scene[scene_key],)
            if (
                use_external_cloud_mask
                and request.science.invert.preparation.cloud_mask_source_policy
                == CloudMaskSourcePolicy.EXTERNAL_ONLY
                and not any(
                    item.role == InputRole.MASK and item.name == "cloud_mask"
                    for item in context
                )
            ):
                raise PlanningError(
                    "cloud_mask_source_policy='external_only' requires a cloud mask "
                    f"for tile={reflectance.tile}, date={reflectance.date.isoformat()}"
                )
            task = _make_task(
                stages=stages,
                request=request,
                inputs=(
                    reflectance,
                    r0_input,
                    *context,
                ),
                outputs=(
                    ExpectedOutput(
                        path=_raw_output_path(
                            request,
                            tile=reflectance.tile,
                            acquisition_date=reflectance.date,
                            base_dir=base,
                        ),
                        content="raw",
                        existing_file_handling=ExistingFileHandling.WRITE_NEW_FILE,
                        existing_output_policy=request.output.existing_output_policy,
                        product_contents=request.output.product_contents,
                    ),
                ),
                tile=reflectance.tile,
                acquisition_date=reflectance.date,
                item_water_year=reflectance.water_year,
                r0_id=artifact.id,
                r0_recipe=request.r0.recipe,
                depends_on=dependencies,
            )
            tasks.append(task)

    elif Stage.ALBEDO in request.steps:
        raw_inputs = [item for item in discovery.inputs if item.role == InputRole.RAW]
        for raw_input in raw_inputs:
            if raw_input.tile is None or raw_input.date is None:
                continue
            output_path = (
                raw_input.source_path
                if request.output.existing_file_handling
                == ExistingFileHandling.UPDATE_ATOMICALLY
                else _raw_output_path(
                    request,
                    tile=raw_input.tile,
                    acquisition_date=raw_input.date,
                    base_dir=base,
                )
            )
            if (
                request.output.existing_file_handling
                == ExistingFileHandling.WRITE_NEW_FILE
                and output_path == raw_input.source_path
            ):
                raise PlanningError(
                    "standalone albedo with write_new_file resolves its input and output "
                    f"to the same path {output_path}; choose a different output root or "
                    "use update_atomically"
                )
            task = _make_task(
                stages=(Stage.ALBEDO,),
                request=request,
                inputs=(
                    raw_input,
                    *_matching_context(
                        discovery.inputs,
                        tile=raw_input.tile,
                        acquisition_date=raw_input.date,
                    ),
                ),
                outputs=(
                    ExpectedOutput(
                        path=output_path,
                        content="raw",
                        existing_file_handling=request.output.existing_file_handling,
                        existing_output_policy=request.output.existing_output_policy,
                        product_contents=request.output.product_contents,
                    ),
                ),
                tile=raw_input.tile,
                acquisition_date=raw_input.date,
                item_water_year=raw_input.water_year,
                r0_id=None,
            )
            tasks.append(task)

    return tuple(tasks)


def deterministic_plan_payload(plan: ResolvedPlan) -> dict[str, Any]:
    """Return the resolved, nonvolatile portion covered by ``plan_digest``."""
    return {
        "schema_version": plan.schema_version,
        "request": plan.request.model_dump(mode="json", exclude_none=True),
        "tasks": [
            task.model_dump(mode="json", exclude_none=True)
            for task in plan.tasks
        ],
        "resource_profiles": [
            profile.model_dump(mode="json", exclude_none=True)
            for profile in plan.resource_profiles
        ],
        "retry_of_plan_digest": plan.retry_of_plan_digest,
        "retry_number": plan.retry_number,
    }


def _software_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for distribution in (
        "spires-batch",
        "spires-contract",
        "spires-io",
        "spires-r0",
        "spires-inversion",
        "spires-postprocess",
        "pydantic",
    ):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = "uninstalled"
    return versions


def plan_request(
    request: RequestConfig,
    *,
    base_dir: str | Path = ".",
    fail_on_preflight_error: bool = True,
) -> ResolvedPlan:
    started = datetime.now(timezone.utc)
    base = Path(base_dir).resolve()
    config_digest = sha256_digest(request)
    discovery = discover_inputs(request, base_dir=base)
    if request.r0 is not None and request.r0.mode == R0Mode.EXISTING:
        existing_r0_inputs = tuple(
            _r0_input(artifact, request, base, planned=False)
            for artifact in request.r0.artifacts
            if _absolute(artifact.path, base).is_file()
        )
        discovery = DiscoveryResult(
            inputs=tuple((*discovery.inputs, *existing_r0_inputs)),
            issues=discovery.issues,
        )

    planning_issues: list[PreflightIssue] = []
    try:
        profiles = resolve_resource_profiles(request)
        tasks = build_tasks(request, discovery, base_dir=base)
    except PlanningError as exc:
        profiles = resolve_resource_profiles(request)
        tasks = ()
        planning_issues.append(
            PreflightIssue(
                layer=CheckLayer.SEMANTIC,
                severity=CheckSeverity.ERROR,
                code="planning_error",
                message=str(exc),
            )
        )

    if planning_issues:
        discovery = DiscoveryResult(
            inputs=discovery.inputs,
            issues=tuple((*discovery.issues, *planning_issues)),
        )
    preflight = run_preflight(request, discovery, tasks, started_at=started)
    if fail_on_preflight_error and not preflight.passed:
        raise PreflightFailedError(preflight)

    timestamp = started.strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{request.run.name}-{timestamp}-{config_digest.split(':', 1)[1][:12]}"
    placeholder = ResolvedPlan(
        run_id=run_id,
        manifest_family_id=run_id,
        created_at=started,
        request=request,
        config_digest=config_digest,
        plan_digest="sha256:" + "0" * 64,
        tasks=tasks,
        resource_profiles=profiles,
        preflight=preflight,
        software_versions=_software_versions(),
    )
    return placeholder.model_copy(
        update={"plan_digest": sha256_digest(deterministic_plan_payload(placeholder))}
    )
