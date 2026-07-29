"""Manifest-backed Phase D scientific task execution."""

from __future__ import annotations

import errno
import importlib.metadata
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spires_batch.models import (
    ExistingFileHandling,
    ExistingOutputPolicy,
    FailureClass,
    InputRole,
    ProductContents,
    R0Recipe,
    ResolvedInput,
    ResolvedPlan,
    ScenePreparationConfig,
    Stage,
    Task,
    TaskAttempt,
    TaskStatus,
)


_TRANSIENT_ERRNOS = {
    errno.EAGAIN,
    errno.EBUSY,
    errno.EDQUOT,
    errno.EIO,
    errno.EMFILE,
    errno.ENFILE,
    errno.ENOSPC,
    errno.ESTALE,
    errno.ETIMEDOUT,
}
_MASK_SOURCE_KWARGS = {
    "cloud_mask": "cloud_mask_source",
    "water_mask": "water_mask_source",
    "ice_mask": "ice_mask_source",
    "playa_mask": "playa_mask_source",
}
_SCIENTIFIC_DISTRIBUTIONS = (
    "spires-batch",
    "spires-contract",
    "spires-io",
    "spires-r0",
    "spires-inversion",
    "spires-postprocess",
)
_OPERATION_ORDER = (
    "canopy_correction",
    "ice_adjustment",
    "albedo",
    "delta_vis",
    "radiative_forcing",
)


class TaskExecutionError(RuntimeError):
    """Failure with an explicit retry classification and stable code."""

    def __init__(
        self,
        message: str,
        *,
        failure_class: FailureClass = FailureClass.DETERMINISTIC,
        failure_code: str = "task_configuration",
    ) -> None:
        self.failure_class = failure_class
        self.failure_code = failure_code
        super().__init__(message)


def product_identity(task: Task):
    """Construct the persisted scientific identity represented by one task."""
    if task.tile is None or task.date is None:
        raise TaskExecutionError(
            "raw-product tasks require tile and acquisition date identities",
            failure_code="missing_product_identity",
        )
    try:
        from spires_contract import ProductIdentity
    except ImportError as exc:
        raise TaskExecutionError(
            "spires-contract is required for scientific execution",
            failure_code="missing_scientific_dependency",
        ) from exc
    return ProductIdentity(
        sensor=task.sensor,
        platform=task.platform,
        product=task.product,
        spatial_id=task.tile,
        acquisition_time=task.date.isoformat(),
    )


def completed_operations(task: Task) -> tuple[str, ...]:
    """Return canonical postprocessing operations selected by a task."""
    options = task.science.albedo
    if options is None:
        return ()
    selected = (
        ("canopy_correction", options.apply_canopy_correction),
        ("ice_adjustment", options.apply_ice_adjustment),
        ("albedo", options.calculate_albedo),
        ("delta_vis", options.calculate_delta_vis),
        ("radiative_forcing", options.calculate_radiative_forcing),
    )
    return tuple(name for name, enabled in selected if enabled)


def _merge_operations(*collections: tuple[str, ...]) -> tuple[str, ...]:
    selected = {
        operation
        for collection in collections
        for operation in collection
    }
    return tuple(operation for operation in _OPERATION_ORDER if operation in selected)


def expected_content_profile(task: Task) -> str:
    if Stage.ALBEDO in task.stages:
        return "postprocessed_raw"
    if Stage.INVERT in task.stages:
        return "inversion_raw"
    raise TaskExecutionError(
        f"task stages {[stage.value for stage in task.stages]} do not produce a raw product",
        failure_code="unsupported_output_stage",
    )


def validate_scientific_outputs(task: Task) -> tuple[bool, str]:
    """Validate every expected output using its scientific product contract."""
    try:
        for output in task.outputs:
            if output.content == "raw":
                import spires_io

                inspection = spires_io.validate_spires_product(
                    output.path,
                    expected_identity=product_identity(task),
                    expected_profile=expected_content_profile(task),
                    expected_contents=(
                        None
                        if output.product_contents is None
                        else output.product_contents.value
                    ),
                    validation="sample",
                )
                required_operations = set(completed_operations(task))
                actual_operations = (
                    set()
                    if inspection.metadata is None
                    else set(inspection.metadata.completed_operations)
                )
                missing = sorted(required_operations - actual_operations)
                if missing:
                    return (
                        False,
                        f"persisted output is missing completed operation(s) {missing}",
                    )
            elif output.content == "r0":
                import xarray as xr
                from spires_r0 import validate_r0_dataset

                with xr.open_dataset(output.path) as dataset:
                    validate_r0_dataset(dataset)
            else:
                return False, f"unsupported output content {output.content!r}"
    except Exception as exc:
        return False, str(exc)
    return True, "all outputs reopened and passed scientific validation"


def _runtime_versions() -> dict[str, str]:
    versions = {}
    for distribution in _SCIENTIFIC_DISTRIBUTIONS:
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = "uninstalled"
    return versions


def _failure_details(exc: Exception) -> tuple[FailureClass, str]:
    if isinstance(exc, TaskExecutionError):
        return exc.failure_class, exc.failure_code
    if isinstance(exc, FileNotFoundError):
        return FailureClass.DETERMINISTIC, "missing_input"
    if isinstance(exc, FileExistsError):
        return FailureClass.DETERMINISTIC, "existing_output"
    if isinstance(exc, PermissionError):
        return FailureClass.DETERMINISTIC, "permission_denied"
    if isinstance(exc, ImportError):
        return FailureClass.DETERMINISTIC, "missing_scientific_dependency"
    if isinstance(exc, MemoryError):
        return FailureClass.TRANSIENT, "memory_exhausted"
    if isinstance(exc, TimeoutError):
        return FailureClass.TRANSIENT, "io_timeout"
    if isinstance(exc, OSError):
        if exc.errno in _TRANSIENT_ERRNOS:
            return FailureClass.TRANSIENT, "transient_io"
        return FailureClass.DETERMINISTIC, "filesystem_error"
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        return FailureClass.DETERMINISTIC, "scientific_contract"
    if exc.__class__.__name__ == "ContractError":
        return FailureClass.DETERMINISTIC, "scientific_contract"
    return FailureClass.DETERMINISTIC, "scientific_execution"


def _one_input(
    task: Task,
    role: InputRole,
    *,
    name: str | None = None,
) -> ResolvedInput:
    matches = [
        item
        for item in task.inputs
        if item.role == role and (name is None or item.name == name)
    ]
    if len(matches) != 1:
        label = role.value if name is None else f"{role.value}:{name}"
        raise TaskExecutionError(
            f"task requires exactly one {label} input, found {len(matches)}",
            failure_code="input_cardinality",
        )
    path = matches[0].execution_path
    if not path.is_file():
        raise FileNotFoundError(f"task input is unavailable at execution path: {path}")
    return matches[0]


def _named_inputs(task: Task, role: InputRole) -> dict[str, ResolvedInput]:
    selected: dict[str, ResolvedInput] = {}
    for item in task.inputs:
        if item.role != role:
            continue
        if item.name is None:
            raise TaskExecutionError(
                f"{role.value} task input {item.execution_path} has no name",
                failure_code="unnamed_context_input",
            )
        if item.name in selected:
            raise TaskExecutionError(
                f"task has more than one {role.value} input named {item.name!r}",
                failure_code="input_cardinality",
            )
        if not item.execution_path.is_file():
            raise FileNotFoundError(
                f"task input is unavailable at execution path: {item.execution_path}"
            )
        selected[item.name] = item
    return selected


def _input_spec(item: ResolvedInput) -> str | Path | dict[str, Any]:
    variable = item.metadata.get("variable") or item.metadata.get("var")
    if variable is None:
        return item.execution_path
    return {"path": item.execution_path, "variable": variable}


def _apply_ancillary_metadata(data, inputs: dict[str, ResolvedInput]):
    if data.ancillary is None:
        return data
    ancillary = data.ancillary.copy(deep=False)
    for name, item in inputs.items():
        if name not in ancillary:
            continue
        attrs = dict(ancillary[name].attrs)
        configured_attrs = item.metadata.get("attrs")
        if isinstance(configured_attrs, dict):
            attrs.update(configured_attrs)
        if item.metadata.get("units") is not None:
            attrs["units"] = item.metadata["units"]
        ancillary[name].attrs = attrs
    return data.assign_ancillary(ancillary)


def _netcdf_safe_attrs(name: str, source_attrs: dict[str, Any]) -> dict[str, Any]:
    attrs = dict(source_attrs)
    for structural_name in (
        "CLASS",
        "DIMENSION_LIST",
        "NAME",
        "REFERENCE_LIST",
    ):
        attrs.pop(structural_name, None)
    if "_FillValue" in attrs:
        fill_value = attrs.pop("_FillValue")
        existing = attrs.get("source_fill_value")
        if existing is not None and existing != fill_value:
            raise TaskExecutionError(
                f"{name!r} has conflicting source fill metadata",
                failure_code="conflicting_fill_metadata",
            )
        attrs["source_fill_value"] = fill_value
    return attrs


def _netcdf_safe_variables(value):
    """Preserve source fill metadata without using NetCDF's reserved attribute."""
    prepared = value.copy(deep=False)
    if hasattr(prepared, "data_vars"):
        for name in prepared.variables:
            prepared[name].attrs = _netcdf_safe_attrs(
                str(name),
                prepared[name].attrs,
            )
    else:
        prepared.attrs = _netcdf_safe_attrs(
            str(prepared.name or "background"),
            prepared.attrs,
        )
        for name in prepared.coords:
            prepared.coords[name].attrs = _netcdf_safe_attrs(
                str(name),
                prepared.coords[name].attrs,
            )
    return prepared


def _netcdf_safe_data(data):
    """Adapt reader metadata to the public SPIReS NetCDF writer boundary."""
    data = data.assign_scene(_netcdf_safe_variables(data.scene))
    if data.background is not None:
        data = data.assign_background(_netcdf_safe_variables(data.background))
    if data.ancillary is not None:
        data = data.assign_ancillary(_netcdf_safe_variables(data.ancillary))
    if data.results is not None:
        data = data.assign_results(_netcdf_safe_variables(data.results))
    return data


def _data_for_output_contents(data, product_contents: ProductContents):
    """Remove noncanonical scalar coordinates before compact persistence."""
    if product_contents == ProductContents.RESULTS_SUBSET:
        non_dimension_coordinates = [
            name
            for name in data.scene.coords
            if name not in data.scene.dims and name != "spatial_ref"
        ]
        if non_dimension_coordinates:
            data = data.assign_scene(
                data.scene.drop_vars(non_dimension_coordinates)
            )
    return _netcdf_safe_data(data)


def _close_data(data) -> None:
    """Release any lazy source handles retained by xarray-backed fields."""
    for value in (
        data.scene,
        data.background,
        data.ancillary,
        data.results,
    ):
        close = getattr(value, "close", None)
        if close is not None:
            close()


def _background_selected_bands(spires_io, path: Path) -> list[str]:
    """Resolve scene bands from a labeled background product."""
    background = spires_io.load_background_reflectance(path)
    try:
        values = background.coords["band"].values.tolist()
        if values == list(range(1, len(values) + 1)):
            raise TaskExecutionError(
                "science.invert.preparation.bands is required when the R0 "
                "background uses positional rather than sensor band labels",
                failure_code="unresolved_scene_bands",
            )
        return [str(value) for value in values]
    finally:
        background.close()


def _prepare_inversion_data(task: Task):
    import spires_io
    from spires_contract import (
        SpiresData,
        validate_for_inversion,
        validate_spatial_alignment,
        validate_spires_data,
    )

    options = task.science.invert
    if options is None:
        raise TaskExecutionError(
            "invert task has no resolved inversion options",
            failure_code="missing_stage_science",
        )
    reflectance = _one_input(task, InputRole.REFLECTANCE)
    background = _one_input(task, InputRole.R0)
    inversion_lut = _one_input(task, InputRole.LUT, name="inversion_lut")
    mask_inputs = _named_inputs(task, InputRole.MASK)
    ancillary_inputs = _named_inputs(task, InputRole.ANCILLARY)

    prepare_kwargs = options.preparation.model_dump(exclude_none=True)
    if "bands" not in prepare_kwargs:
        prepare_kwargs["bands"] = _background_selected_bands(
            spires_io,
            background.execution_path,
        )
    prepare_kwargs["lut_file"] = inversion_lut.execution_path
    for name, item in mask_inputs.items():
        keyword = _MASK_SOURCE_KWARGS[name]
        prepare_kwargs[keyword] = item.execution_path

    scene = spires_io.prepare_scene_for_inversion(
        reflectance.execution_path,
        sensor=task.sensor,
        platform=task.platform,
        **prepare_kwargs,
    )
    background_data = spires_io.load_background_reflectance(
        background.execution_path,
        target_scene=scene,
    )
    ancillary = spires_io.load_ancillary_layers(
        {
            name: _input_spec(item)
            for name, item in ancillary_inputs.items()
        },
        target_scene=scene,
    )
    albedo = task.science.albedo
    require_illumination = bool(
        (albedo is not None and albedo.calculate_albedo)
        or (
            options.clustering.enabled
            and "cosine_illumination" in options.clustering.features
        )
    )
    scene = spires_io.add_illumination_geometry(
        scene,
        ancillary,
        require_illumination=require_illumination,
    )
    data = SpiresData(
        scene=scene,
        background=background_data,
        ancillary=ancillary,
    )
    data = _apply_ancillary_metadata(data, ancillary_inputs)
    validate_spires_data(data)
    validate_spatial_alignment(data)
    validate_for_inversion(data)
    return data, inversion_lut.execution_path


def _selected_reflectance_lut(spires_inversion, data, path: Path):
    """Select a validated master LUT in exact scene-band order."""
    master_lut = spires_inversion.load_reflectance_lut(path)
    scene_bands = [str(value) for value in data.scene["band"].values]
    available_bands = {str(value) for value in master_lut["band"].values}
    missing_bands = [
        band for band in scene_bands if band not in available_bands
    ]
    if missing_bands:
        raise TaskExecutionError(
            "reflectance LUT cannot supply selected scene band(s) "
            f"{missing_bands}; available bands are "
            f"{[str(value) for value in master_lut['band'].values]}",
            failure_code="reflectance_lut_band_mismatch",
        )
    selected = master_lut.sel(band=scene_bands)
    selected.encoding["source"] = str(path)
    return selected


def _invert(task: Task):
    import spires_inversion
    import spires_io

    data, inversion_lut_path = _prepare_inversion_data(task)
    options = task.science.invert
    assert options is not None
    if options.clustering.enabled:
        cluster_kwargs = options.clustering.model_dump(exclude={"enabled"})
        data = spires_io.cluster(
            data,
            apply_valid_inversion_mask=options.apply_valid_inversion_mask,
            **cluster_kwargs,
        )
    inversion_kwargs = options.model_dump(
        exclude={"preparation", "clustering"},
        exclude_none=True,
    )
    inversion_lut = _selected_reflectance_lut(
        spires_inversion,
        data,
        inversion_lut_path,
    )
    return spires_inversion.invert(
        data,
        lut=inversion_lut,
        **inversion_kwargs,
    )


def _postprocess(task: Task, data):
    import spires_postprocess

    options = task.science.albedo
    if options is None:
        raise TaskExecutionError(
            "albedo task has no resolved postprocessing options",
            failure_code="missing_stage_science",
        )
    lut_inputs = _named_inputs(task, InputRole.LUT)
    kwargs = options.model_dump()
    kwargs["albedo_lookup"] = (
        None
        if "albedo_lookup" not in lut_inputs
        else lut_inputs["albedo_lookup"].execution_path
    )
    kwargs["forcing_lookup"] = (
        None
        if "forcing_lookup" not in lut_inputs
        else lut_inputs["forcing_lookup"].execution_path
    )
    return spires_postprocess.process(data, **kwargs)


def _task_provenance(plan: ResolvedPlan, task: Task) -> dict[str, Any]:
    return {
        "batch_run_id": plan.run_id,
        "batch_manifest_family_id": plan.manifest_family_id,
        "batch_plan_digest": plan.plan_digest,
        "batch_config_digest": plan.config_digest,
        "batch_task_id": task.task_id,
        "r0_id": task.r0_id,
        "r0_recipe": task.r0_recipe,
        "resolved_inputs": [
            {
                "role": item.role.value,
                "name": item.name,
                "source_path": str(item.source_path),
                "execution_path": str(item.execution_path),
                "size_bytes": item.size_bytes,
                "mtime_ns": item.mtime_ns,
                "source_sha256": item.source_sha256,
            }
            for item in task.inputs
        ],
        "science": task.science.model_dump(mode="json", exclude_none=True),
    }


def _reuse_existing(task: Task) -> tuple[bool, str]:
    existing = [output for output in task.outputs if output.path.exists()]
    if not existing:
        return False, ""
    updating = any(
        output.existing_file_handling == ExistingFileHandling.UPDATE_ATOMICALLY
        for output in existing
    )
    if updating:
        if all(
            output.existing_output_policy == ExistingOutputPolicy.REUSE_VALID
            for output in existing
        ):
            valid, message = validate_scientific_outputs(task)
            if valid:
                return True, message
        return False, ""
    if any(
        output.existing_output_policy != ExistingOutputPolicy.REUSE_VALID
        for output in existing
    ):
        paths = [str(output.path) for output in existing]
        raise FileExistsError(f"task output already exists: {paths}")
    valid, message = validate_scientific_outputs(task)
    if not valid:
        raise TaskExecutionError(
            f"existing output is not reusable: {message}",
            failure_code="invalid_existing_output",
        )
    return True, message


def _write_daily_product(plan: ResolvedPlan, task: Task, data) -> None:
    import spires_io

    if len(task.outputs) != 1 or task.outputs[0].content != "raw":
        raise TaskExecutionError(
            "daily scientific tasks require exactly one raw output",
            failure_code="output_cardinality",
        )
    output = task.outputs[0]
    output.path.parent.mkdir(parents=True, exist_ok=True)
    try:
        spires_io.write_spires_data(
            _data_for_output_contents(data, output.product_contents),
            output.path,
            identity=product_identity(task),
            content_profile=expected_content_profile(task),
            product_contents=output.product_contents.value,
            completed_operations=completed_operations(task),
            provenance=_task_provenance(plan, task),
            package_versions=_runtime_versions(),
            validation="sample",
            overwrite=False,
        )
    finally:
        _close_data(data)


def _build_r0(task: Task) -> None:
    import spires_r0

    if len(task.outputs) != 1 or task.outputs[0].content != "r0":
        raise TaskExecutionError(
            "R0 tasks require exactly one R0 output",
            failure_code="output_cardinality",
        )
    options = task.science.build_r0
    if options is None:
        raise TaskExecutionError(
            "build_r0 task has no resolved R0 science options",
            failure_code="missing_stage_science",
        )
    expected_recipe = {
        "viirs": R0Recipe.VIIRS_SUMMER_COMPOSITE,
        "modis": R0Recipe.MODIS_SUMMER_COMPOSITE,
    }.get(task.sensor)
    if expected_recipe is None or task.r0_recipe != expected_recipe:
        raise TaskExecutionError(
            f"sensor {task.sensor!r} cannot execute R0 recipe "
            f"{None if task.r0_recipe is None else task.r0_recipe.value!r}",
            failure_code="r0_recipe_sensor_mismatch",
        )
    sources = [
        item.execution_path
        for item in task.inputs
        if item.role == InputRole.R0_SOURCE
    ]
    if not sources:
        raise TaskExecutionError(
            "build_r0 task requires at least one r0_source input",
            failure_code="input_cardinality",
        )
    for path in sources:
        if not path.is_file():
            raise FileNotFoundError(
                f"task input is unavailable at execution path: {path}"
            )

    default_preparation = ScenePreparationConfig()
    if (
        options.preparation.max_sensor_zenith
        != default_preparation.max_sensor_zenith
    ):
        raise TaskExecutionError(
            "R0 source preparation currently requires the public reader default "
            "max_sensor_zenith=65; configure science.build_r0.max_sensor_zenith "
            "for composite screening",
            failure_code="unsupported_r0_preparation_option",
        )
    prepare_kwargs = options.preparation.model_dump(
        exclude={"max_sensor_zenith"},
        exclude_none=True,
    )
    builder = {
        R0Recipe.VIIRS_SUMMER_COMPOSITE: spires_r0.build_viirs_r0_from_sources,
        R0Recipe.MODIS_SUMMER_COMPOSITE: spires_r0.build_modis_r0_from_sources,
    }[task.r0_recipe]
    output = task.outputs[0]
    result = builder(
        sources,
        r0_path=output.path,
        overwrite=False,
        show_progress=options.show_progress,
        max_sensor_zenith=options.max_sensor_zenith,
        ndvi_tie_epsilon=options.ndvi_tie_epsilon,
        min_blue_reflectance=options.min_blue_reflectance,
        chunks=options.chunks,
        **prepare_kwargs,
    )
    close = getattr(result, "close", None)
    if close is not None:
        close()


def _standalone_albedo(plan: ResolvedPlan, task: Task) -> None:
    import spires_io
    import xarray as xr
    from spires_contract import validate_spatial_alignment, validate_spires_data

    raw_input = _one_input(task, InputRole.RAW)
    inspection = spires_io.validate_spires_product(
        raw_input.execution_path,
        expected_identity=product_identity(task),
        validation="sample",
    )
    metadata = inspection.metadata
    if metadata is None:
        raise TaskExecutionError(
            "standalone albedo input has no persisted metadata",
            failure_code="invalid_raw_input",
        )
    if metadata.product_contents == ProductContents.RESULTS_SUBSET.value:
        raise TaskExecutionError(
            "standalone albedo does not support results_subset inputs",
            failure_code="standalone_results_subset_unsupported",
        )

    data = spires_io.read_spires_data(
        raw_input.execution_path,
        expected_identity=metadata.identity,
        expected_profile=metadata.content_profile,
        expected_contents=metadata.product_contents,
    )
    ancillary_inputs = _named_inputs(task, InputRole.ANCILLARY)
    if ancillary_inputs:
        additions = spires_io.load_ancillary_layers(
            {
                name: _input_spec(item)
                for name, item in ancillary_inputs.items()
            },
            target_scene=data.scene,
        )
        if additions is not None:
            ancillary = (
                additions
                if data.ancillary is None
                else xr.merge(
                    (additions, data.ancillary),
                    compat="override",
                    join="exact",
                )
            )
            data = data.assign_ancillary(ancillary)
            data = _apply_ancillary_metadata(data, ancillary_inputs)
    options = task.science.albedo
    assert options is not None
    scene = spires_io.add_illumination_geometry(
        data.scene,
        data.ancillary,
        require_illumination=options.calculate_albedo,
    )
    data = data.assign_scene(scene)
    validate_spires_data(data)
    validate_spatial_alignment(data)
    data = _postprocess(task, data)
    output = task.outputs[0]
    requested_operations = completed_operations(task)
    if output.existing_file_handling == ExistingFileHandling.UPDATE_ATOMICALLY:
        if output.path != raw_input.source_path:
            raise TaskExecutionError(
                "atomic standalone albedo output must be the source raw path",
                failure_code="atomic_update_path_mismatch",
            )
        if output.product_contents.value != metadata.product_contents:
            raise TaskExecutionError(
                "atomic updates preserve product_contents; configured output does not "
                "match the existing product",
                failure_code="atomic_update_contents_mismatch",
            )
        spires_io.update_spires_data_atomically(
            output.path,
            data.results,
            completed_operations=requested_operations,
            provenance=_task_provenance(plan, task),
            package_versions=_runtime_versions(),
            expected_identity=metadata.identity,
            expected_contents=metadata.product_contents,
            validation="sample",
        )
        return

    operations = _merge_operations(
        tuple(metadata.completed_operations),
        requested_operations,
    )
    output.path.parent.mkdir(parents=True, exist_ok=True)
    spires_io.write_spires_data(
        _data_for_output_contents(data, output.product_contents),
        output.path,
        identity=metadata.identity,
        content_profile="postprocessed_raw",
        product_contents=output.product_contents.value,
        completed_operations=operations,
        provenance={
            **dict(metadata.provenance),
            **_task_provenance(plan, task),
        },
        package_versions={
            **dict(metadata.package_versions),
            **_runtime_versions(),
        },
        validation="sample",
        overwrite=False,
    )


@dataclass(frozen=True)
class ScientificExecutor:
    """Execute resolved tasks through the public SPIReS package APIs."""

    plan: ResolvedPlan

    def __post_init__(self) -> None:
        if not self.plan.preflight.passed:
            raise ValueError("scientific execution requires a preflighted plan")

    def __call__(self, task: Task, attempt_number: int) -> TaskAttempt:
        started = datetime.now(timezone.utc)
        slurm_job_id = os.environ.get("SLURM_JOB_ID")
        slurm_array_task_id = os.environ.get("SLURM_ARRAY_TASK_ID")
        try:
            known_task = next(
                (item for item in self.plan.tasks if item.task_id == task.task_id),
                None,
            )
            if known_task is None or known_task != task:
                raise TaskExecutionError(
                    "task is not an exact member of the supplied resolved plan",
                    failure_code="task_manifest_mismatch",
                )

            reused, reuse_message = _reuse_existing(task)
            if reused:
                return TaskAttempt(
                    task_id=task.task_id,
                    attempt=attempt_number,
                    status=TaskStatus.LOADED_EXISTING,
                    started_at=started,
                    ended_at=datetime.now(timezone.utc),
                    message=reuse_message,
                    slurm_job_id=slurm_job_id,
                    slurm_array_task_id=slurm_array_task_id,
                )

            if task.stages == (Stage.BUILD_R0,):
                _build_r0(task)
            elif task.stages == (Stage.INVERT,):
                _write_daily_product(self.plan, task, _invert(task))
            elif task.stages == (Stage.INVERT, Stage.ALBEDO):
                _write_daily_product(
                    self.plan,
                    task,
                    _postprocess(task, _invert(task)),
                )
            elif task.stages == (Stage.ALBEDO,):
                _standalone_albedo(self.plan, task)
            else:
                raise TaskExecutionError(
                    f"unsupported task stage combination {task.stages}",
                    failure_code="unsupported_stage_combination",
                )

            valid, validation_message = validate_scientific_outputs(task)
            if not valid:
                raise TaskExecutionError(
                    f"final output validation failed: {validation_message}",
                    failure_code="invalid_final_output",
                )
            return TaskAttempt(
                task_id=task.task_id,
                attempt=attempt_number,
                status=TaskStatus.SUCCEEDED,
                started_at=started,
                ended_at=datetime.now(timezone.utc),
                message=validation_message,
                slurm_job_id=slurm_job_id,
                slurm_array_task_id=slurm_array_task_id,
            )
        except Exception as exc:
            failure_class, failure_code = _failure_details(exc)
            return TaskAttempt(
                task_id=task.task_id,
                attempt=attempt_number,
                status=TaskStatus.FAILED,
                started_at=started,
                ended_at=datetime.now(timezone.utc),
                failure_class=failure_class,
                failure_code=failure_code,
                message=str(exc),
                slurm_job_id=slurm_job_id,
                slurm_array_task_id=slurm_array_task_id,
            )
