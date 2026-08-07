"""Direct Slurm array rendering for immutable resolved manifests."""

from __future__ import annotations

import shlex
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from spires_batch.backends import topological_tasks
from spires_batch.models import ResolvedPlan, ResourceProfile, Task
from spires_batch.serialization import write_immutable_json


MAX_TASKS_PER_SLURM_ARRAY = 1000


@dataclass(frozen=True)
class SlurmArrayGroup:
    group_id: str
    tasks: tuple[Task, ...]
    dependency_group_ids: tuple[str, ...]
    profile: ResourceProfile
    max_concurrent_tasks: int
    script_path: Path
    index_path: Path


@dataclass(frozen=True)
class SlurmRenderResult:
    groups: tuple[SlurmArrayGroup, ...]
    submit_script: Path


def _group_tasks(
    plan: ResolvedPlan,
    *,
    task_ids: frozenset[str] | None = None,
) -> tuple[tuple[Task, ...], ...]:
    profiles = {profile.name: profile for profile in plan.resource_profiles}
    ordered = tuple(
        task
        for task in topological_tasks(plan)
        if task_ids is None or task.task_id in task_ids
    )
    if task_ids is not None:
        missing = task_ids - {task.task_id for task in ordered}
        if missing:
            raise ValueError(f"Slurm selection contains unknown task IDs {sorted(missing)}")
    if not ordered:
        raise ValueError("Slurm rendering requires at least one selected task")
    grouped: dict[tuple[tuple[str, ...], str, str], list[Task]] = defaultdict(list)
    order: list[tuple[tuple[str, ...], str, str]] = []
    for task in ordered:
        key = (
            tuple(stage.value for stage in task.stages),
            task.resource_profile,
            task.tile or "global",
        )
        if key not in grouped:
            order.append(key)
        grouped[key].append(task)
        if task.resource_profile not in profiles:
            raise ValueError(
                f"task {task.task_id!r} names unknown resource profile "
                f"{task.resource_profile!r}"
            )
    return tuple(
        tuple(tasks[offset : offset + MAX_TASKS_PER_SLURM_ARRAY])
        for key in order
        for tasks in (grouped[key],)
        for offset in range(0, len(tasks), MAX_TASKS_PER_SLURM_ARRAY)
    )


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
    reservation_set_path: str | Path | None = None,
    task_ids: tuple[str, ...] | None = None,
    attempt_number: int | None = None,
) -> SlurmRenderResult:
    """Render strict dependency arrays without submitting any jobs."""
    manifest = Path(manifest_path).resolve()
    output_dir = Path(output_directory).resolve()
    reservation_set = (
        None
        if reservation_set_path is None
        else Path(reservation_set_path).resolve()
    )
    profiles = {profile.name: profile for profile in plan.resource_profiles}
    selected_task_ids = None if task_ids is None else frozenset(task_ids)
    raw_groups = _group_tasks(plan, task_ids=selected_task_ids)
    group_counts: dict[tuple[tuple[str, ...], str], int] = defaultdict(int)
    for tasks in raw_groups:
        key = (
            tuple(stage.value for stage in tasks[0].stages),
            tasks[0].resource_profile,
        )
        group_counts[key] += 1
    group_ordinals: dict[tuple[tuple[str, ...], str], int] = defaultdict(int)
    task_to_group: dict[str, str] = {}
    groups: list[SlurmArrayGroup] = []
    (output_dir / "logs").mkdir(parents=True, exist_ok=True)

    for index, tasks in enumerate(raw_groups):
        stages = "-".join(stage.value for stage in tasks[0].stages)
        tile = tasks[0].tile or "global"
        group_id = f"g{index:03d}-{stages}-{tile}"
        dependency_groups = sorted(
            {
                task_to_group[dependency]
                for task in tasks
                for dependency in task.depends_on
                if dependency in task_to_group
            }
        )
        profile = profiles[tasks[0].resource_profile]
        concurrency_key = (
            tuple(stage.value for stage in tasks[0].stages),
            tasks[0].resource_profile,
        )
        group_ordinal = group_ordinals[concurrency_key]
        group_ordinals[concurrency_key] += 1
        sibling_count = group_counts[concurrency_key]
        if profile.max_concurrent_tasks < sibling_count:
            raise ValueError(
                f"resource profile {profile.name!r} has "
                f"max_concurrent_tasks={profile.max_concurrent_tasks}, but stage "
                f"{stages!r} renders {sibling_count} sibling arrays; raise the "
                "profile cap to at least the number of arrays"
            )
        base_concurrency, remainder = divmod(
            profile.max_concurrent_tasks,
            sibling_count,
        )
        array_concurrency = max(
            1,
            base_concurrency + (1 if group_ordinal < remainder else 0),
        )
        script_path = output_dir / f"{group_id}.sbatch"
        index_path = output_dir / f"{group_id}.tasks.json"
        write_immutable_json(
            index_path,
            {
                "artifact_type": "spires_batch_slurm_task_index",
                "schema_version": plan.schema_version,
                "plan_digest": plan.plan_digest,
                "group_id": group_id,
                "cluster": profile.cluster,
                "resource_profile": profile.name,
                "task_ids": [task.task_id for task in tasks],
            },
        )

        directives = [
            "#!/bin/bash",
            *_directive("clusters", profile.cluster),
            *_directive("job-name", f"spires-{stages}"[:128]),
            *_directive("comment", f"spires-batch:{plan.run_id}:{group_id}"),
            *_directive("partition", profile.partition),
            *_directive("account", profile.account),
            *_directive("qos", profile.qos),
            *_directive("time", profile.time_limit),
            *_directive("cpus-per-task", profile.cpus_per_task),
            *_directive("mem", profile.memory),
            f"#SBATCH --array=0-{len(tasks) - 1}%{array_concurrency}",
            f"#SBATCH --output={output_dir}/logs/{group_id}-%A_%a.out",
            f"#SBATCH --error={output_dir}/logs/{group_id}-%A_%a.err",
        ]
        for extra in profile.extra_directives:
            if "\n" in extra or "\r" in extra:
                raise ValueError("resource extra_directives cannot contain newlines")
            directives.append(
                extra if extra.startswith("#SBATCH ") else f"#SBATCH {extra}"
            )
        worker_command = (
            f"mamba run -n {shlex.quote(profile.environment_name)} "
            f"spires-batch execute-task --manifest {shlex.quote(str(manifest))} "
            f"--task-index {shlex.quote(str(index_path))} "
            '--array-index "${SLURM_ARRAY_TASK_ID}"'
        )
        if attempt_number is not None:
            if attempt_number < 1:
                raise ValueError("Slurm worker attempt number must be positive")
            worker_command += f" --attempt {attempt_number}"
        if reservation_set is not None:
            worker_command += (
                " --reservation-set "
                f"{shlex.quote(str(reservation_set))}"
            )
        body = [
            "",
            "set -euo pipefail",
            "module load miniforge",
            worker_command,
            "",
        ]
        _write_exclusive_text(script_path, "\n".join((*directives, *body)))
        group = SlurmArrayGroup(
            group_id=group_id,
            tasks=tasks,
            dependency_group_ids=tuple(dependency_groups),
            profile=profile,
            max_concurrent_tasks=array_concurrency,
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
