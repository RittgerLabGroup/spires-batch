"""Structured, append-only workflow event logs."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from spires_batch.models import (
    TaskAttempt,
    TaskStatus,
    WorkflowEvent,
)


class EventLog:
    """One JSON Lines event log, normally scoped to one task."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, event: WorkflowEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            event.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        descriptor = os.open(
            self.path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o664,
        )
        try:
            os.write(descriptor, (payload + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def read(self) -> tuple[WorkflowEvent, ...]:
        if not self.path.exists():
            return ()
        events: list[WorkflowEvent] = []
        with self.path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    events.append(WorkflowEvent.model_validate_json(line))
                except Exception as exc:
                    raise ValueError(
                        f"invalid event at {self.path}:{line_number}: {exc}"
                    ) from exc
        return tuple(events)


def attempt_events(
    run_id: str,
    attempt: TaskAttempt,
) -> tuple[WorkflowEvent, ...]:
    events: list[WorkflowEvent] = []
    if attempt.started_at is not None:
        events.append(
            WorkflowEvent(
                timestamp=attempt.started_at,
                event_type="task_started",
                run_id=run_id,
                task_id=attempt.task_id,
                attempt=attempt.attempt,
                status=TaskStatus.RUNNING,
                slurm_job_id=attempt.slurm_job_id,
                slurm_array_task_id=attempt.slurm_array_task_id,
            )
        )
    terminal_time = attempt.ended_at or datetime.now(timezone.utc)
    events.append(
        WorkflowEvent(
            timestamp=terminal_time,
            event_type="task_terminal",
            run_id=run_id,
            task_id=attempt.task_id,
            attempt=attempt.attempt,
            status=attempt.status,
            failure_class=attempt.failure_class,
            failure_code=attempt.failure_code,
            message=attempt.message,
            slurm_job_id=attempt.slurm_job_id,
            slurm_array_task_id=attempt.slurm_array_task_id,
        )
    )
    return tuple(events)


def write_attempt(log: EventLog, run_id: str, attempt: TaskAttempt) -> None:
    for event in attempt_events(run_id, attempt):
        log.append(event)


def read_event_logs(paths: Iterable[str | Path]) -> tuple[WorkflowEvent, ...]:
    events = [
        event
        for path in paths
        for event in EventLog(path).read()
    ]
    return tuple(sorted(events, key=lambda event: event.timestamp))
