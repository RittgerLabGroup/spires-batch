"""Planning-only dry run and executor-neutral serial backend."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from spires_batch.events import EventLog, write_attempt
from spires_batch.models import (
    FailureClass,
    ResolvedPlan,
    Task,
    TaskAttempt,
    TaskStatus,
)


TaskExecutor = Callable[[Task, int], TaskAttempt]


@dataclass(frozen=True)
class DryRunTask:
    index: int
    task_id: str
    stages: tuple[str, ...]
    dependencies: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]


def topological_tasks(plan: ResolvedPlan) -> tuple[Task, ...]:
    tasks = {task.task_id: task for task in plan.tasks}
    ordered: list[Task] = []
    completed: set[str] = set()
    while len(ordered) < len(tasks):
        ready = sorted(
            (
                task
                for task in tasks.values()
                if task.task_id not in completed
                and set(task.depends_on).issubset(completed | (set(task.depends_on) - set(tasks)))
            ),
            key=lambda task: task.task_id,
        )
        if not ready:
            unresolved = sorted(set(tasks) - completed)
            raise ValueError(f"task dependency graph contains a cycle: {unresolved}")
        for task in ready:
            ordered.append(task)
            completed.add(task.task_id)
    return tuple(ordered)


class DryRunBackend:
    def render(self, plan: ResolvedPlan) -> tuple[DryRunTask, ...]:
        return tuple(
            DryRunTask(
                index=index,
                task_id=task.task_id,
                stages=tuple(stage.value for stage in task.stages),
                dependencies=task.depends_on,
                inputs=tuple(str(item.execution_path) for item in task.inputs),
                outputs=tuple(str(item.path) for item in task.outputs),
            )
            for index, task in enumerate(topological_tasks(plan))
        )


class SerialBackend:
    """Run tasks in dependency order through an injected scientific executor.

    Phase A deliberately supplies no scientific executor. Phase D can inject
    one without changing plan or attempt models.
    """

    def execute(
        self,
        plan: ResolvedPlan,
        executor: TaskExecutor,
        *,
        attempt_number: int = 1,
        log_directory: str | Path | None = None,
    ) -> tuple[TaskAttempt, ...]:
        terminal: dict[str, TaskAttempt] = {}
        attempts: list[TaskAttempt] = []
        for task in topological_tasks(plan):
            failed_dependencies = [
                dependency
                for dependency in task.depends_on
                if dependency in terminal
                and terminal[dependency].status
                not in {TaskStatus.SUCCEEDED, TaskStatus.LOADED_EXISTING}
            ]
            if failed_dependencies:
                now = datetime.now(timezone.utc)
                attempt = TaskAttempt(
                    task_id=task.task_id,
                    attempt=attempt_number,
                    status=TaskStatus.BLOCKED,
                    started_at=now,
                    ended_at=now,
                    message=f"blocked by dependencies {failed_dependencies}",
                )
            else:
                try:
                    attempt = executor(task, attempt_number)
                except Exception as exc:
                    now = datetime.now(timezone.utc)
                    attempt = TaskAttempt(
                        task_id=task.task_id,
                        attempt=attempt_number,
                        status=TaskStatus.FAILED,
                        started_at=now,
                        ended_at=now,
                        failure_class=FailureClass.DETERMINISTIC,
                        failure_code="executor_exception",
                        message=str(exc),
                    )
                if attempt.task_id != task.task_id:
                    raise ValueError(
                        f"executor returned attempt for {attempt.task_id!r}; "
                        f"expected {task.task_id!r}"
                    )
            terminal[task.task_id] = attempt
            attempts.append(attempt)
            if log_directory is not None:
                log_path = Path(log_directory) / f"{task.task_id}.jsonl"
                write_attempt(EventLog(log_path), plan.run_id, attempt)
        return tuple(attempts)
