"""Output-derived status, retry manifests, and hierarchical summaries."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from spires_batch.events import EventLog
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
class AttemptStatusRecord:
    attempt: int
    status: TaskStatus
    failure_class: FailureClass | None
    failure_code: str | None
    message: str | None
    started_at: datetime | None
    ended_at: datetime | None
    elapsed_seconds: float | None
    slurm_job_id: str | None
    slurm_array_task_id: str | None
    log_path: str | None
    stdout_path: str | None
    stderr_path: str | None


@dataclass(frozen=True)
class TaskStatusRecord:
    task_id: str
    tile: str | None
    date: str | None
    stages: tuple[str, ...]
    status: TaskStatus
    attempt_count: int
    retry_count: int
    failure_code: str | None
    message: str | None
    output_paths: tuple[str, ...]
    log_path: str | None
    slurm_job_id: str | None
    slurm_array_task_id: str | None
    stdout_path: str | None
    stderr_path: str | None
    started_at: datetime | None
    ended_at: datetime | None
    elapsed_seconds: float | None
    attempt_history: tuple[AttemptStatusRecord, ...]


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


def attempts_from_event_paths(
    paths: Iterable[str | Path],
) -> tuple[TaskAttempt, ...]:
    """Read task attempts while retaining the event-log path for reporting."""
    attempts: list[TaskAttempt] = []
    for source in sorted(Path(path) for path in paths):
        for attempt in attempts_from_events(EventLog(source).read()):
            attempts.append(
                attempt.model_copy(
                    update={"log_path": attempt.log_path or source.resolve()}
                )
            )
    return tuple(
        sorted(
            attempts,
            key=lambda item: (
                item.task_id,
                item.attempt,
                "" if item.ended_at is None else item.ended_at.isoformat(),
            ),
        )
    )


def _elapsed_seconds(
    started_at: datetime | None,
    ended_at: datetime | None,
) -> float | None:
    if started_at is None or ended_at is None:
        return None
    return max(0.0, (ended_at - started_at).total_seconds())


def _attempt_status_record(attempt: TaskAttempt) -> AttemptStatusRecord:
    stdout_path, stderr_path = _scheduler_log_paths(attempt)
    return AttemptStatusRecord(
        attempt=attempt.attempt,
        status=attempt.status,
        failure_class=attempt.failure_class,
        failure_code=attempt.failure_code,
        message=attempt.message,
        started_at=attempt.started_at,
        ended_at=attempt.ended_at,
        elapsed_seconds=_elapsed_seconds(attempt.started_at, attempt.ended_at),
        slurm_job_id=attempt.slurm_job_id,
        slurm_array_task_id=attempt.slurm_array_task_id,
        log_path=None if attempt.log_path is None else str(attempt.log_path),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
    )


def _scheduler_log_paths(
    attempt: TaskAttempt,
) -> tuple[str | None, str | None]:
    if (
        attempt.log_path is None
        or attempt.slurm_job_id is None
        or attempt.slurm_array_task_id is None
    ):
        return None, None
    event_path = Path(attempt.log_path)
    if event_path.parent.name != "events":
        return None, None
    logs_directory = event_path.parent.parent / "logs"
    suffix = f"-{attempt.slurm_job_id}_{attempt.slurm_array_task_id}"
    stdout = tuple(sorted(logs_directory.glob(f"*{suffix}.out")))
    stderr = tuple(sorted(logs_directory.glob(f"*{suffix}.err")))
    return (
        None if not stdout else str(stdout[0].resolve()),
        None if not stderr else str(stderr[0].resolve()),
    )


def _summary_counts(
    records: Iterable[TaskStatusRecord],
) -> dict[str, int]:
    materialized = tuple(records)
    statuses = Counter(record.status.value for record in materialized)
    counts = {
        "total": len(materialized),
        "completed": (
            statuses[TaskStatus.SUCCEEDED.value]
            + statuses[TaskStatus.LOADED_EXISTING.value]
        ),
        "reused": statuses[TaskStatus.LOADED_EXISTING.value],
        "failed": statuses[TaskStatus.FAILED.value],
        "missing": statuses[TaskStatus.MISSING.value],
        "retried": sum(record.retry_count > 0 for record in materialized),
        "retry_attempts": sum(record.retry_count for record in materialized),
    }
    counts.update(
        {
            status.value: statuses[status.value]
            for status in TaskStatus
            if status.value not in counts
        }
    )
    return counts


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
        attempt_history = tuple(_attempt_status_record(item) for item in history)
        if not history:
            status = TaskStatus.PLANNED
            latest = None
            message = None
            failure_code = None
        else:
            latest = history[-1]
            status = latest.status
            message = latest.message
            failure_code = latest.failure_code
            if status in {TaskStatus.SUCCEEDED, TaskStatus.LOADED_EXISTING}:
                valid, validation_message = output_validator(task)
                if not valid:
                    status = TaskStatus.MISSING
                    message = validation_message
                    failure_code = "output_validation_failed"
        started_times = [
            item.started_at for item in history if item.started_at is not None
        ]
        ended_times = [
            item.ended_at for item in history if item.ended_at is not None
        ]
        elapsed_values = [
            item.elapsed_seconds
            for item in attempt_history
            if item.elapsed_seconds is not None
        ]
        records.append(
            TaskStatusRecord(
                task_id=task.task_id,
                tile=task.tile,
                date=None if task.date is None else task.date.isoformat(),
                stages=tuple(stage.value for stage in task.stages),
                status=status,
                attempt_count=len(history),
                retry_count=max(0, len(history) - 1),
                failure_code=failure_code,
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
                stdout_path=(
                    None if not attempt_history else attempt_history[-1].stdout_path
                ),
                stderr_path=(
                    None if not attempt_history else attempt_history[-1].stderr_path
                ),
                started_at=min(started_times) if started_times else None,
                ended_at=max(ended_times) if ended_times else None,
                elapsed_seconds=(
                    sum(elapsed_values) if elapsed_values else None
                ),
                attempt_history=attempt_history,
            )
        )
    terminal_stage = (
        plan.request.steps[-1].value if plan.request.steps else "none"
    )
    return Summary(
        run_id=plan.run_id,
        plan_digest=plan.plan_digest,
        generated_at=datetime.now(timezone.utc),
        terminal_stage=terminal_stage,
        counts=_summary_counts(records),
        records=tuple(records),
    )


def _datetime_text(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _attempt_payload(record: AttemptStatusRecord) -> dict[str, object]:
    return {
        "attempt": record.attempt,
        "status": record.status.value,
        "failure_class": (
            None if record.failure_class is None else record.failure_class.value
        ),
        "failure_code": record.failure_code,
        "message": record.message,
        "started_at": _datetime_text(record.started_at),
        "ended_at": _datetime_text(record.ended_at),
        "elapsed_seconds": record.elapsed_seconds,
        "slurm_job_id": record.slurm_job_id,
        "slurm_array_task_id": record.slurm_array_task_id,
        "log_path": record.log_path,
        "stdout_path": record.stdout_path,
        "stderr_path": record.stderr_path,
    }


def _task_payload(record: TaskStatusRecord) -> dict[str, object]:
    return {
        "task_id": record.task_id,
        "tile": record.tile,
        "date": record.date,
        "stages": list(record.stages),
        "status": record.status.value,
        "attempt_count": record.attempt_count,
        "retry_count": record.retry_count,
        "failure_code": record.failure_code,
        "message": record.message,
        "output_paths": list(record.output_paths),
        "log_path": record.log_path,
        "slurm_job_id": record.slurm_job_id,
        "slurm_array_task_id": record.slurm_array_task_id,
        "stdout_path": record.stdout_path,
        "stderr_path": record.stderr_path,
        "started_at": _datetime_text(record.started_at),
        "ended_at": _datetime_text(record.ended_at),
        "elapsed_seconds": record.elapsed_seconds,
        "attempt_history": [
            _attempt_payload(attempt) for attempt in record.attempt_history
        ],
    }


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

    tile_counts = {
        tile: _summary_counts(records)
        for tile, records in sorted(
            (
                (tile, tuple(group))
                for tile, group in _group_records_by_tile(summary.records).items()
            ),
            key=lambda item: item[0],
        )
    }
    payload = {
        "artifact_type": "spires_batch_summary",
        "schema_version": 1,
        "run_id": summary.run_id,
        "plan_digest": summary.plan_digest,
        "generated_at": summary.generated_at.isoformat(),
        "terminal_stage": summary.terminal_stage,
        "counts": summary.counts,
        "tile_counts": tile_counts,
        "records": [_task_payload(record) for record in summary.records],
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
        "retry_count",
        "failure_code",
        "message",
        "output_paths",
        "log_path",
        "slurm_job_id",
        "slurm_array_task_id",
        "stdout_path",
        "stderr_path",
        "started_at",
        "ended_at",
        "elapsed_seconds",
        "attempt_history",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for record in summary.records:
            row = _task_payload(record)
            row["stages"] = ",".join(record.stages)
            row["output_paths"] = ",".join(record.output_paths)
            row["attempt_history"] = json.dumps(
                row["attempt_history"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            writer.writerow(row)

    lines = [
        f"run_id: {summary.run_id}",
        f"plan_digest: {summary.plan_digest}",
        f"terminal_stage: {summary.terminal_stage}",
        "counts: "
        + ", ".join(f"{key}={value}" for key, value in summary.counts.items()),
        "",
    ]
    if tile_counts:
        lines.append("tiles:")
        lines.extend(
            "  "
            + tile
            + ": "
            + ", ".join(
                f"{key}={value}"
                for key, value in counts.items()
                if key in {"total", "completed", "reused", "failed", "missing", "retried"}
            )
            for tile, counts in tile_counts.items()
        )
        lines.append("")
    lines.extend(
        f"{record.status.value:16} attempts={record.attempt_count} "
        f"retries={record.retry_count} {record.task_id} "
        f"{record.message or ''}".rstrip()
        for record in summary.records
    )
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, csv_path, text_path


def _group_records_by_tile(
    records: Iterable[TaskStatusRecord],
) -> dict[str, list[TaskStatusRecord]]:
    grouped: dict[str, list[TaskStatusRecord]] = defaultdict(list)
    for record in records:
        grouped[record.tile or "global"].append(record)
    return grouped


def tile_summaries(summary: Summary) -> dict[str, Summary]:
    grouped = _group_records_by_tile(summary.records)
    return {
        tile: Summary(
            run_id=summary.run_id,
            plan_digest=summary.plan_digest,
            generated_at=summary.generated_at,
            terminal_stage=summary.terminal_stage,
            counts=_summary_counts(records),
            records=tuple(records),
        )
        for tile, records in sorted(grouped.items())
    }


def build_retry_plan(
    plan: ResolvedPlan,
    attempts: Iterable[TaskAttempt],
    *,
    eligible_task_ids: Iterable[str] | None = None,
    retry_number: int | None = None,
) -> ResolvedPlan:
    latest: dict[str, TaskAttempt] = {}
    for attempt in attempts:
        previous = latest.get(attempt.task_id)
        if previous is None or attempt.attempt > previous.attempt:
            latest[attempt.task_id] = attempt

    requested_ids = (
        None if eligible_task_ids is None else frozenset(eligible_task_ids)
    )
    eligible_ids = {
        task_id
        for task_id, attempt in latest.items()
        if attempt.status == TaskStatus.FAILED
        and attempt.failure_class == FailureClass.TRANSIENT
        and attempt.attempt <= plan.request.execution.max_auto_retry_count
        and (requested_ids is None or task_id in requested_ids)
    }
    retry_tasks = tuple(task for task in plan.tasks if task.task_id in eligible_ids)
    next_retry_number = (
        plan.retry_number + 1 if retry_number is None else retry_number
    )
    if next_retry_number < 1:
        raise ValueError("retry manifest number must be positive")
    placeholder = plan.model_copy(
        update={
            "run_id": f"{plan.manifest_family_id}-retry-{next_retry_number}",
            "created_at": datetime.now(timezone.utc),
            "plan_digest": "sha256:" + "0" * 64,
            "tasks": retry_tasks,
            "retry_of_plan_digest": plan.plan_digest,
            "retry_number": next_retry_number,
        }
    )
    return placeholder.model_copy(
        update={"plan_digest": sha256_digest(deterministic_plan_payload(placeholder))}
    )
