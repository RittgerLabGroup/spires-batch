"""Optional, validated staging of resolved inputs onto shared scratch."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from spires_batch.models import ResolvedInput, ResolvedPlan, StagingVerification
from spires_batch.serialization import file_sha256


@dataclass(frozen=True)
class StagingAction:
    source_path: Path
    destination_path: Path
    size_bytes: int
    verification: StagingVerification
    reusable: bool


@dataclass(frozen=True)
class StagingResult:
    action: StagingAction
    status: str
    message: str


def plan_staging(plan: ResolvedPlan) -> tuple[StagingAction, ...]:
    staging = plan.request.execution.staging
    if not staging.enabled:
        return ()
    unique: dict[tuple[Path, Path], ResolvedInput] = {}
    for task in plan.tasks:
        for item in task.inputs:
            if item.source_path != item.execution_path and item.size_bytes is not None:
                unique[(item.source_path, item.execution_path)] = item
    return tuple(
        StagingAction(
            source_path=item.source_path,
            destination_path=item.execution_path,
            size_bytes=item.size_bytes or 0,
            verification=staging.verification,
            reusable=staging.reuse_valid,
        )
        for _, item in sorted(
            unique.items(),
            key=lambda pair: (str(pair[0][1]), str(pair[0][0])),
        )
    )


def _valid_staged_file(action: StagingAction) -> bool:
    destination = action.destination_path
    if not destination.is_file() or destination.stat().st_size != action.size_bytes:
        return False
    if action.verification == StagingVerification.SHA256:
        return file_sha256(action.source_path) == file_sha256(destination)
    return True


def execute_staging(
    plan: ResolvedPlan,
    *,
    dry_run: bool = False,
) -> tuple[StagingResult, ...]:
    """Copy required inputs atomically and validate every staged artifact."""
    results: list[StagingResult] = []
    for action in plan_staging(plan):
        if action.reusable and _valid_staged_file(action):
            results.append(
                StagingResult(action, "reused", "existing staged input is valid")
            )
            continue
        if dry_run:
            results.append(
                StagingResult(action, "planned", "input would be copied to scratch")
            )
            continue

        destination = action.destination_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
            shutil.copy2(action.source_path, temporary)
            staged_action = StagingAction(
                source_path=action.source_path,
                destination_path=temporary,
                size_bytes=action.size_bytes,
                verification=action.verification,
                reusable=action.reusable,
            )
            if not _valid_staged_file(staged_action):
                raise IOError(
                    f"staged copy failed {action.verification.value} verification: "
                    f"{action.source_path} -> {temporary}"
                )
            os.replace(temporary, destination)
            temporary = None
            if not _valid_staged_file(action):
                raise IOError(f"promoted staged input failed validation: {destination}")
            results.append(StagingResult(action, "copied", "staged input is valid"))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return tuple(results)
