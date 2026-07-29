"""Direct Slurm array rendering for immutable resolved manifests."""

from __future__ import annotations

import shlex
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from spires_batch.backends import topological_tasks
from spires_batch.models import ResolvedPlan, ResourceProfile, Task
from spires_batch.serialization import write_immutable_json


@dataclass(frozen=True)
class SlurmArrayGroup:
    group_id: str
    tasks: tuple[Task, ...]
    dependency_group_ids: tuple[str, ...]
    profile: ResourceProfile
    script_path: Path
    index_path: Path


@dataclass(frozen=True)
class SlurmRenderResult:
    groups: tuple[SlurmArrayGroup, ...]
    submit_script: Path


def _group_tasks(plan: ResolvedPlan) -> tuple[tuple[Task, ...], ...]:
    profiles = {profile.name: profile for profile in plan.resource_profiles}
    ordered = topological_tasks(plan)
    grouped: dict[tuple[tuple[str, ...], str], list[Task]] = defaultdict(list)
    order: list[tuple[tuple[str, ...], str]] = []
    for task in ordered:
        key = (tuple(stage.value for stage in task.stages), task.resource_profile)
        if key not in grouped:
            order.append(key)
        grouped[key].append(task)
        if task.resource_profile not in profiles:
            raise ValueError(
                f"task {task.task_id!r} names unknown resource profile "
                f"{task.resource_profile!r}"
            )
    return tuple(tuple(grouped[key]) for key in order)


def _directive(flag: str, value: str | int | None) -> list[str]:
    return [] if value is None else [f"#SBATCH --{flag}={value}"]


def _write_exclusive_text(path: Path, text: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(text)
    except FileExistsError as exc:
        raise FileExistsError(f"refusing to replace rendered Slurm file {path}") from exc
    if executable:
        path.chmod(path.stat().st_mode | 0o110)


def render_slurm(
    plan: ResolvedPlan,
    *,
    manifest_path: str | Path,
    output_directory: str | Path,
) -> SlurmRenderResult:
    """Render strict dependency arrays without submitting any jobs."""
    manifest = Path(manifest_path).resolve()
    output_dir = Path(output_directory).resolve()
    profiles = {profile.name: profile for profile in plan.resource_profiles}
    raw_groups = _group_tasks(plan)
    task_to_group: dict[str, str] = {}
    groups: list[SlurmArrayGroup] = []

    for index, tasks in enumerate(raw_groups):
        stages = "-".join(stage.value for stage in tasks[0].stages)
        group_id = f"g{index:03d}-{stages}"
        dependency_groups = sorted(
            {
                task_to_group[dependency]
                for task in tasks
                for dependency in task.depends_on
                if dependency in task_to_group
            }
        )
        profile = profiles[tasks[0].resource_profile]
        script_path = output_dir / f"{group_id}.sbatch"
        index_path = output_dir / f"{group_id}.tasks.json"
        write_immutable_json(
            index_path,
            {
                "artifact_type": "spires_batch_slurm_task_index",
                "schema_version": plan.schema_version,
                "plan_digest": plan.plan_digest,
                "group_id": group_id,
                "task_ids": [task.task_id for task in tasks],
            },
        )

        directives = [
            "#!/bin/bash",
            *_directive("job-name", f"spires-{stages}"[:128]),
            *_directive("partition", profile.partition),
            *_directive("account", profile.account),
            *_directive("qos", profile.qos),
            *_directive("time", profile.time_limit),
            *_directive("cpus-per-task", profile.cpus_per_task),
            *_directive("mem", profile.memory),
            f"#SBATCH --array=0-{len(tasks) - 1}%{profile.max_concurrent_tasks}",
            f"#SBATCH --output={output_dir}/logs/{group_id}-%A_%a.out",
            f"#SBATCH --error={output_dir}/logs/{group_id}-%A_%a.err",
        ]
        for extra in profile.extra_directives:
            if "\n" in extra or "\r" in extra:
                raise ValueError("resource extra_directives cannot contain newlines")
            directives.append(
                extra if extra.startswith("#SBATCH ") else f"#SBATCH {extra}"
            )
        body = [
            "",
            "set -euo pipefail",
            "module load miniforge",
            (
                f"mamba run -n {shlex.quote(profile.environment_name)} "
                f"spires-batch execute-task --manifest {shlex.quote(str(manifest))} "
                f"--task-index {shlex.quote(str(index_path))} "
                '--array-index "${SLURM_ARRAY_TASK_ID}"'
            ),
            "",
        ]
        _write_exclusive_text(script_path, "\n".join((*directives, *body)))
        group = SlurmArrayGroup(
            group_id=group_id,
            tasks=tasks,
            dependency_group_ids=tuple(dependency_groups),
            profile=profile,
            script_path=script_path,
            index_path=index_path,
        )
        groups.append(group)
        for task in tasks:
            task_to_group[task.task_id] = group_id

    submit_path = output_dir / "submit.sh"
    submit_lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        "module load slurm/blanca",
        "",
    ]
    for group in groups:
        variable = group.group_id.replace("-", "_")
        dependency = ""
        if group.dependency_group_ids:
            dependency_values = ":".join(
                "${" + dependency_id.replace("-", "_") + "}"
                for dependency_id in group.dependency_group_ids
            )
            dependency = f" --dependency=afterok:{dependency_values}"
        submit_lines.append(
            f"{variable}=$(sbatch --parsable{dependency} "
            f"{shlex.quote(str(group.script_path))})"
        )
        submit_lines.append(f'echo "{group.group_id} ${{{variable}}}"')
    submit_lines.append("")
    _write_exclusive_text(submit_path, "\n".join(submit_lines), executable=True)
    return SlurmRenderResult(groups=tuple(groups), submit_script=submit_path)
