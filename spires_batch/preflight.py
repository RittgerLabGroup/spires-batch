"""Four-layer preflight validation and lightweight metadata inspection."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from spires_batch.discovery import DiscoveryResult
from spires_batch.models import (
    CheckLayer,
    CheckSeverity,
    InputRole,
    MetadataCheck,
    PreflightIssue,
    PreflightResult,
    ProductContents,
    RequestConfig,
    ResolvedInput,
    Stage,
    Task,
)


class PreflightFailedError(ValueError):
    def __init__(self, result: PreflightResult):
        self.result = result
        errors = [
            issue.message
            for issue in result.issues
            if issue.severity == CheckSeverity.ERROR
        ]
        super().__init__("preflight failed: " + "; ".join(errors))


def _schema_and_semantic_issues(request: RequestConfig) -> list[PreflightIssue]:
    return [
        PreflightIssue(
            layer=CheckLayer.SCHEMA,
            severity=CheckSeverity.INFO,
            code="schema_valid",
            message=f"request conforms to schema version {request.schema_version}",
        ),
        PreflightIssue(
            layer=CheckLayer.SEMANTIC,
            severity=CheckSeverity.INFO,
            code="semantics_valid",
            message="cross-section stage and policy relationships are valid",
        ),
    ]


def _inventory_issues(
    request: RequestConfig,
    discovery: DiscoveryResult,
    tasks: Iterable[Task],
) -> list[PreflightIssue]:
    issues = list(discovery.issues)
    steps = set(request.steps)
    task_list = tuple(tasks)
    for stage in request.steps:
        if not any(stage in task.stages for task in task_list):
            issues.append(
                PreflightIssue(
                    layer=CheckLayer.INVENTORY,
                    severity=CheckSeverity.ERROR,
                    code="empty_stage_plan",
                    message=(
                        f"selected stage {stage.value!r} resolved to no tasks; "
                        "check selections, discovery roots, patterns, and explicit inputs"
                    ),
                )
            )
    relevant_role = (
        InputRole.REFLECTANCE
        if Stage.INVERT in steps
        else InputRole.RAW
        if Stage.ALBEDO in steps or Stage.INTERPOLATE in steps
        else None
    )
    dated_inputs = [
        item for item in discovery.inputs if item.role == relevant_role
    ]

    if request.selection.dates and relevant_role in {
        InputRole.REFLECTANCE,
        InputRole.RAW,
    }:
        selected_tiles = request.selection.tiles or tuple(
            sorted({item.tile for item in dated_inputs if item.tile is not None})
        )
        present = {
            (item.tile, item.date)
            for item in dated_inputs
            if item.tile is not None and item.date is not None
        }
        for tile in selected_tiles:
            for acquisition_date in request.selection.dates:
                if (tile, acquisition_date) not in present:
                    issues.append(
                        PreflightIssue(
                            layer=CheckLayer.INVENTORY,
                            severity=CheckSeverity.ERROR,
                            code="missing_requested_date",
                            message=(
                                f"no {relevant_role.value} input resolved for "
                                f"tile={tile}, date={acquisition_date.isoformat()}"
                            ),
                        )
                    )

    output_owner: dict[Path, str] = {}
    for task in task_list:
        issues.extend(_task_input_issues(task))
        for output in task.outputs:
            previous = output_owner.get(output.path)
            if previous is not None:
                issues.append(
                    PreflightIssue(
                        layer=CheckLayer.INVENTORY,
                        severity=CheckSeverity.ERROR,
                        code="duplicate_output",
                        message=(
                            f"tasks {previous!r} and {task.task_id!r} resolve to the "
                            f"same output {output.path}"
                        ),
                        path=output.path,
                        task_id=task.task_id,
                    )
                )
            else:
                output_owner[output.path] = task.task_id

            if (
                output.path.exists()
                and output.existing_file_handling.value == "write_new_file"
            ):
                severity = (
                    CheckSeverity.ERROR
                    if request.output.existing_output_policy.value == "error"
                    else CheckSeverity.WARNING
                )
                issues.append(
                    PreflightIssue(
                        layer=CheckLayer.INVENTORY,
                        severity=severity,
                        code="existing_output",
                        message=(
                            f"expected output already exists under policy "
                            f"{request.output.existing_output_policy.value!r}: {output.path}"
                        ),
                        path=output.path,
                        task_id=task.task_id,
                    )
                )
    return issues


def _task_input_issues(task: Task) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []

    def require_count(
        role: InputRole,
        *,
        name: str | None = None,
        count: int = 1,
    ) -> None:
        matches = [
            item
            for item in task.inputs
            if item.role == role and (name is None or item.name == name)
        ]
        if len(matches) == count:
            return
        label = role.value if name is None else f"{role.value}:{name}"
        issues.append(
            PreflightIssue(
                layer=CheckLayer.INVENTORY,
                severity=CheckSeverity.ERROR,
                code="task_input_cardinality",
                message=(
                    f"task {task.task_id!r} requires exactly {count} {label} "
                    f"input(s), found {len(matches)}"
                ),
                task_id=task.task_id,
            )
        )

    if Stage.BUILD_R0 in task.stages:
        sources = [item for item in task.inputs if item.role == InputRole.R0_SOURCE]
        if not sources:
            issues.append(
                PreflightIssue(
                    layer=CheckLayer.INVENTORY,
                    severity=CheckSeverity.ERROR,
                    code="missing_task_input",
                    message=(
                        f"task {task.task_id!r} requires at least one r0_source input"
                    ),
                    task_id=task.task_id,
                )
            )

    if Stage.INVERT in task.stages:
        require_count(InputRole.REFLECTANCE)
        require_count(InputRole.R0)
        require_count(InputRole.LUT, name="inversion_lut")

    if Stage.ALBEDO in task.stages:
        if Stage.INVERT not in task.stages:
            require_count(InputRole.RAW)
        options = task.science.albedo
        if options is None:
            issues.append(
                PreflightIssue(
                    layer=CheckLayer.SEMANTIC,
                    severity=CheckSeverity.ERROR,
                    code="missing_stage_science",
                    message=f"task {task.task_id!r} has no albedo science options",
                    task_id=task.task_id,
                )
            )
        else:
            if options.apply_canopy_correction:
                require_count(InputRole.ANCILLARY, name="canopy_fraction")
            if options.apply_ice_adjustment:
                require_count(InputRole.ANCILLARY, name="ice_fraction")
            if options.calculate_albedo:
                require_count(InputRole.LUT, name="albedo_lookup")
                require_count(InputRole.ANCILLARY, name="dem")
                require_count(InputRole.ANCILLARY, name="slope")
                require_count(InputRole.ANCILLARY, name="aspect")
            if options.calculate_delta_vis or options.calculate_radiative_forcing:
                require_count(InputRole.LUT, name="forcing_lookup")

    named_roles = {InputRole.LUT, InputRole.ANCILLARY, InputRole.MASK}
    keys = {
        (item.role, item.name)
        for item in task.inputs
        if item.role in named_roles
    }
    for role, name in sorted(keys, key=lambda item: (item[0].value, item[1] or "")):
        require_count(role, name=name)
    return issues


def inspect_metadata_header(path: Path) -> dict[str, Any]:
    """Open only enough of a file to prove that its container header is readable."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        return {"container": "json", "top_level_type": type(value).__name__}

    if suffix in {".h5", ".hdf5", ".nc", ".mat"}:
        try:
            import h5py

            with h5py.File(path, "r") as source:
                return {
                    "container": "hdf5",
                    "root_keys": tuple(sorted(source.keys())),
                    "root_attributes": tuple(sorted(source.attrs.keys())),
                }
        except ImportError:
            pass
        except OSError:
            if suffix not in {".nc"}:
                raise

    if suffix == ".nc":
        try:
            import netCDF4

            with netCDF4.Dataset(path, "r") as source:
                return {
                    "container": source.data_model,
                    "dimensions": tuple(sorted(source.dimensions)),
                    "variables": tuple(sorted(source.variables)),
                    "groups": tuple(sorted(source.groups)),
                }
        except ImportError:
            pass

    if suffix in {".tif", ".tiff"}:
        try:
            import rasterio

            with rasterio.open(path) as source:
                return {
                    "container": source.driver,
                    "shape": (source.height, source.width),
                    "count": source.count,
                    "crs": None if source.crs is None else str(source.crs),
                }
        except ImportError:
            pass

    with path.open("rb") as stream:
        signature = stream.read(8)
    if not signature:
        raise ValueError("file is empty")
    return {"container": "opaque", "signature_hex": signature.hex()}


def _metadata_candidates(
    inputs: tuple[ResolvedInput, ...],
    mode: MetadataCheck,
) -> tuple[ResolvedInput, ...]:
    if mode == MetadataCheck.NONE:
        return ()
    if mode == MetadataCheck.ALL:
        return inputs

    groups: dict[tuple[str, str, str, str], list[ResolvedInput]] = defaultdict(list)
    for item in inputs:
        group = (
            item.role.value,
            item.product or "",
            item.source_path.suffix.lower(),
            item.name or "",
        )
        groups[group].append(item)
    return tuple(
        sorted(items, key=lambda item: str(item.source_path))[0]
        for _, items in sorted(groups.items())
    )


def _persisted_product_contents(path: Path) -> str | None:
    """Read the SPIReS root content marker without loading product arrays."""
    try:
        import h5py
    except ImportError:
        return None
    with h5py.File(path, "r") as source:
        value = source.attrs.get("spires_product_contents")
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    item = getattr(value, "item", None)
    if item is not None:
        value = item()
        if isinstance(value, bytes):
            return value.decode("utf-8")
    return str(value)


def _standalone_product_policy_issues(
    tasks: tuple[Task, ...],
) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    for task in tasks:
        if task.stages != (Stage.ALBEDO,):
            continue
        raw_inputs = [
            item for item in task.inputs if item.role == InputRole.RAW
        ]
        if len(raw_inputs) != 1 or not raw_inputs[0].source_path.is_file():
            continue
        try:
            contents = _persisted_product_contents(raw_inputs[0].source_path)
        except Exception:
            # Generic metadata inspection reports unreadable containers.
            continue
        if contents == ProductContents.RESULTS_SUBSET.value:
            issues.append(
                PreflightIssue(
                    layer=CheckLayer.METADATA,
                    severity=CheckSeverity.ERROR,
                    code="standalone_results_subset_unsupported",
                    message=(
                        "standalone albedo requires a full raw product; "
                        "results_subset inputs omit required scene and ancillary "
                        "context"
                    ),
                    path=raw_inputs[0].source_path,
                    task_id=task.task_id,
                )
            )
    return issues


def _metadata_issues(
    inputs: tuple[ResolvedInput, ...],
    mode: MetadataCheck,
) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    candidates = _metadata_candidates(inputs, mode)
    for item in candidates:
        try:
            summary = inspect_metadata_header(item.source_path)
        except Exception as exc:
            issues.append(
                PreflightIssue(
                    layer=CheckLayer.METADATA,
                    severity=CheckSeverity.ERROR,
                    code="metadata_unreadable",
                    message=f"metadata header inspection failed: {exc}",
                    path=item.source_path,
                )
            )
        else:
            issues.append(
                PreflightIssue(
                    layer=CheckLayer.METADATA,
                    severity=CheckSeverity.INFO,
                    code="metadata_readable",
                    message=(
                        f"metadata header is readable as {summary.get('container', 'unknown')}"
                    ),
                    path=item.source_path,
                )
            )
    if mode == MetadataCheck.NONE:
        issues.append(
            PreflightIssue(
                layer=CheckLayer.METADATA,
                severity=CheckSeverity.INFO,
                code="metadata_skipped",
                message="metadata header inspection was disabled",
            )
        )
    return issues


def run_preflight(
    request: RequestConfig,
    discovery: DiscoveryResult,
    tasks: tuple[Task, ...],
    *,
    started_at: datetime | None = None,
) -> PreflightResult:
    started = started_at or datetime.now(timezone.utc)
    issues = _schema_and_semantic_issues(request)
    issues.extend(_inventory_issues(request, discovery, tasks))
    issues.extend(_standalone_product_policy_issues(tasks))
    if not any(
        issue.severity == CheckSeverity.ERROR and issue.layer == CheckLayer.INVENTORY
        for issue in issues
    ):
        issues.extend(
            _metadata_issues(discovery.inputs, request.preflight.metadata_check)
        )
    else:
        issues.append(
            PreflightIssue(
                layer=CheckLayer.METADATA,
                severity=CheckSeverity.WARNING,
                code="metadata_not_run",
                message="metadata inspection was skipped because inventory validation failed",
            )
        )
    return PreflightResult(
        metadata_check=request.preflight.metadata_check,
        started_at=started,
        completed_at=datetime.now(timezone.utc),
        issues=tuple(issues),
    )
