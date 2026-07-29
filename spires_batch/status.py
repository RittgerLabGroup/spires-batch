"""Output-derived status, retry manifests, and hierarchical summaries."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from spires_batch.models import (
    FailureClass,
    ResolvedPlan,
    Task,
    TaskAttempt,
    TaskStatus,
    WorkflowEvent,
)
from spires_batch.planner import deterministic_plan_payload
from spires_batch.science import validate_scientific_outputs
from spires_batch.serialization import sha256_digest


OutputValidator = Callable[[Task], tuple[bool, str]]


@dataclass(frozen=True)
class TaskStatusRecord:
    task_id: str
    tile: str | None
    date: str | None
    stages: tuple[str, ...]
    status: TaskStatus
    attempt_count: int
    failure_code: str | None
    message: str | None
    output_paths: tuple[str, ...]
    log_path: str | None
    slurm_job_id: str | None
    slurm_array_task_id: str | None


@dataclass(frozen=True)
class Summary:
    run_id: str
    plan_digest: str
    generated_at: datetime
    terminal_stage: str
    counts: dict[str, int]
    records: tuple[TaskStatusRecord, ...]


def validate_nonempty_outputs(task: Task) -> tuple[bool, str]:
    """Compatibility validator for callers that explicitly request existence only."""
    missing = [
        str(output.path)
        for output in task.outputs
        if not output.path.is_file() or output.path.stat().st_size == 0
    ]
    if missing:
        return False, f"missing or empty output(s): {missing}"
    return True, "all outputs exist and are nonempty"


def attempts_from_events(events: Iterable[WorkflowEvent]) -> tuple[TaskAttempt, ...]:
    starts: dict[tuple[str, int], WorkflowEvent] = {}
    attempts: list[TaskAttempt] = []
    for event in sorted(events, key=lambda item: item.timestamp):
        if event.task_id is None or event.attempt is None:
            continue
        key = (event.task_id, event.attempt)
        if event.event_type == "task_started":
            starts[key] = event
        elif event.event_type == "task_terminal" and event.status is not None:
            start = starts.get(key)
            attempts.append(
                TaskAttempt(
                    task_id=event.task_id,
                    attempt=event.attempt,
                    status=event.status,
                    started_at=None if start is None else start.timestamp,
                    ended_at=event.timestamp,
                    failure_class=event.failure_class,
                    failure_code=event.failure_code,
                    message=event.message,
                    slurm_job_id=event.slurm_job_id,
                    slurm_array_task_id=event.slurm_array_task_id,
                )
            )
    return tuple(attempts)


def summarize(
    plan: ResolvedPlan,
    attempts: Iterable[TaskAttempt],
    *,
    output_validator: OutputValidator = validate_scientific_outputs,
) -> Summary:
    by_task: dict[str, list[TaskAttempt]] = defaultdict(list)
    for attempt in attempts:
        by_task[attempt.task_id].append(attempt)

    records: list[TaskStatusRecord] = []
    for task in plan.tasks:
        history = sorted(by_task.get(task.task_id, ()), key=lambda item: item.attempt)
        if not history:
            status = TaskStatus.PLANNED
            latest = None
            message = None
        else:
            latest = history[-1]
            status = latest.status
            message = latest.message
            if status in {TaskStatus.SUCCEEDED, TaskStatus.LOADED_EXISTING}:
                valid, validation_message = output_validator(task)
                if not valid:
                    status = TaskStatus.MISSING
                    message = validation_message
        records.append(
            TaskStatusRecord(
                task_id=task.task_id,
                tile=task.tile,
                date=None if task.date is None else task.date.isoformat(),
                stages=tuple(stage.value for stage in task.stages),
                status=status,
                attempt_count=len(history),
                failure_code=None if latest is None else latest.failure_code,
                message=message,
                output_paths=tuple(str(output.path) for output in task.outputs),
                log_path=(
                    None
                    if latest is None or latest.log_path is None
                    else str(latest.log_path)
                ),
                slurm_job_id=None if latest is None else latest.slurm_job_id,
                slurm_array_task_id=(
                    None if latest is None else latest.slurm_array_task_id
                ),
            )
        )
    counts = Counter(record.status.value for record in records)
    terminal_stage = (
        plan.request.steps[-1].value if plan.request.steps else "none"
    )
    return Summary(
        run_id=plan.run_id,
        plan_digest=plan.plan_digest,
        generated_at=datetime.now(timezone.utc),
        terminal_stage=terminal_stage,
        counts=dict(sorted(counts.items())),
        records=tuple(records),
    )


def write_summary_files(
    summary: Summary,
    directory: str | Path,
    *,
    basename: str = "run-summary",
) -> tuple[Path, Path, Path]:
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / f"{basename}.json"
    csv_path = destination / f"{basename}.csv"
    text_path = destination / f"{basename}.txt"

    payload = {
        "artifact_type": "spires_batch_summary",
        "schema_version": 1,
        "run_id": summary.run_id,
        "plan_digest": summary.plan_digest,
        "generated_at": summary.generated_at.isoformat(),
        "terminal_stage": summary.terminal_stage,
        "counts": summary.counts,
        "records": [
            {
                **record.__dict__,
                "status": record.status.value,
            }
            for record in summary.records
        ],
    }
    with json_path.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")

    fieldnames = [
        "task_id",
        "tile",
        "date",
        "stages",
        "status",
        "attempt_count",
        "failure_code",
        "message",
        "output_paths",
        "log_path",
        "slurm_job_id",
        "slurm_array_task_id",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for record in summary.records:
            row = record.__dict__.copy()
            row["stages"] = ",".join(record.stages)
            row["status"] = record.status.value
            row["output_paths"] = ",".join(record.output_paths)
            writer.writerow(row)

    lines = [
        f"run_id: {summary.run_id}",
        f"plan_digest: {summary.plan_digest}",
        f"terminal_stage: {summary.terminal_stage}",
        "counts: "
        + ", ".join(f"{key}={value}" for key, value in summary.counts.items()),
        "",
    ]
    lines.extend(
        f"{record.status.value:16} {record.task_id} "
        f"{record.message or ''}".rstrip()
        for record in summary.records
    )
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, csv_path, text_path


def tile_summaries(summary: Summary) -> dict[str, Summary]:
    grouped: dict[str, list[TaskStatusRecord]] = defaultdict(list)
    for record in summary.records:
        grouped[record.tile or "global"].append(record)
    return {
        tile: Summary(
            run_id=summary.run_id,
            plan_digest=summary.plan_digest,
            generated_at=summary.generated_at,
            terminal_stage=summary.terminal_stage,
            counts=dict(
                sorted(Counter(record.status.value for record in records).items())
            ),
            records=tuple(records),
        )
        for tile, records in grouped.items()
    }


def build_retry_plan(
    plan: ResolvedPlan,
    attempts: Iterable[TaskAttempt],
) -> ResolvedPlan:
    latest: dict[str, TaskAttempt] = {}
    for attempt in attempts:
        previous = latest.get(attempt.task_id)
        if previous is None or attempt.attempt > previous.attempt:
            latest[attempt.task_id] = attempt

    eligible_ids = {
        task_id
        for task_id, attempt in latest.items()
        if attempt.status == TaskStatus.FAILED
        and attempt.failure_class == FailureClass.TRANSIENT
        and attempt.attempt <= plan.request.execution.max_auto_retry_count
    }
    retry_tasks = tuple(task for task in plan.tasks if task.task_id in eligible_ids)
    retry_number = plan.retry_number + 1
    placeholder = plan.model_copy(
        update={
            "run_id": f"{plan.manifest_family_id}-retry-{retry_number}",
            "created_at": datetime.now(timezone.utc),
            "plan_digest": "sha256:" + "0" * 64,
            "tasks": retry_tasks,
            "retry_of_plan_digest": plan.plan_digest,
            "retry_number": retry_number,
        }
    )
    return placeholder.model_copy(
        update={"plan_digest": sha256_digest(deterministic_plan_payload(placeholder))}
    )
