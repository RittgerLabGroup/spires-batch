"""Stage-gated Slurm execution, scheduler reconciliation, and automatic retries."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from pydantic import TypeAdapter

from spires_batch.events import EventLog, read_event_logs, write_attempt
from spires_batch.models import (
    FailureClass,
    OperationalAdvanceRecord,
    OperationalRunRecord,
    ResolvedPlan,
    SchedulerSubmissionGroup,
    SchedulerSubmissionRecord,
    SubmissionEvent,
    Task,
    TaskAttempt,
    TaskStatus,
)
from spires_batch.reservations import ReservationStore
from spires_batch.scheduler import (
    SCHEDULER_SUBMISSION_NAME,
    _parse_job_response,
    _response_text,
    _run_sbatch,
    load_scheduler_submission_record,
    submit_scheduler_submission,
    test_scheduler_submission,
)
from spires_batch.serialization import (
    file_sha256,
    load_json_object,
    load_plan,
    sha256_digest,
    write_immutable_json,
    write_plan,
)
from spires_batch.status import attempts_from_events, build_retry_plan
from spires_batch.submission import (
    RESERVATION_SET_NAME,
    SUBMISSION_RECORD_NAME,
    acquire_submission_reservations,
    append_submission_event,
    load_reservation_set,
    load_submission_record,
    prepare_submission,
)


OPERATION_RECORD_NAME = "operation.json"
OPERATION_EVENTS_NAME = "operation-events.jsonl"
OPERATION_TERMINAL_NAME = "operational-result.json"
ADVANCE_RECORD_NAME = "advance-result.json"
ADVANCE_LOCK_NAME = "advance.lock"
COORDINATOR_SCRIPT_NAME = "coordinator.sbatch"
COORDINATOR_RECORD_NAME = "coordinator-submission.json"

SacctRunner = Callable[
    [tuple[str, ...]],
    subprocess.CompletedProcess[str],
]
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, Any])

_SUCCESS_STATUSES = {TaskStatus.SUCCEEDED, TaskStatus.LOADED_EXISTING}
_TRANSIENT_SLURM_STATES = {
    "BOOT_FAIL",
    "NODE_FAIL",
    "OUT_OF_MEMORY",
    "PREEMPTED",
    "TIMEOUT",
}
_TERMINAL_SLURM_STATES = _TRANSIENT_SLURM_STATES | {
    "CANCELLED",
    "COMPLETED",
    "DEADLINE",
    "FAILED",
    "REVOKED",
    "SPECIAL_EXIT",
}


@dataclass(frozen=True)
class OperationalLaunch:
    operation: OperationalRunRecord
    wave_directory: Path
    scheduler_submission: SchedulerSubmissionRecord
    coordinator_job_id: str


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _operation_payload(
    value: OperationalRunRecord | dict[str, Any],
) -> dict[str, Any]:
    if isinstance(value, OperationalRunRecord):
        return value.model_dump(
            mode="json",
            exclude={"operational_run_id", "operational_run_digest"},
        )
    return _JSON_OBJECT_ADAPTER.dump_python(
        {
            key: item
            for key, item in value.items()
            if key not in {"operational_run_id", "operational_run_digest"}
        },
        mode="json",
    )


def _advance_payload(
    value: OperationalAdvanceRecord | dict[str, Any],
) -> dict[str, Any]:
    if isinstance(value, OperationalAdvanceRecord):
        return value.model_dump(mode="json", exclude={"advance_digest"})
    return _JSON_OBJECT_ADAPTER.dump_python(
        {
            key: item
            for key, item in value.items()
            if key != "advance_digest"
        },
        mode="json",
    )


def load_operational_run(path: str | Path) -> OperationalRunRecord:
    record = OperationalRunRecord.model_validate(load_json_object(path))
    actual = sha256_digest(_operation_payload(record))
    if actual != record.operational_run_digest:
        raise ValueError(
            f"operational run digest mismatch for {path}: "
            f"stored {record.operational_run_digest}, calculated {actual}"
        )
    expected_id = (
        f"{record.manifest_family_id}-operational-"
        f"{record.operational_run_digest.split(':', 1)[1][:12]}"
    )
    if record.operational_run_id != expected_id:
        raise ValueError(
            f"operational run ID mismatch for {path}: "
            f"stored {record.operational_run_id}, expected {expected_id}"
        )
    if file_sha256(record.manifest_path) != record.manifest_sha256:
        raise ValueError("operational run manifest changed after creation")
    plan = load_plan(record.manifest_path)
    if (
        plan.manifest_family_id != record.manifest_family_id
        or plan.config_digest != record.config_digest
        or plan.plan_digest != record.plan_digest
    ):
        raise ValueError("operational run does not match its resolved manifest")
    return record


def load_operational_advance(path: str | Path) -> OperationalAdvanceRecord:
    record = OperationalAdvanceRecord.model_validate(load_json_object(path))
    actual = sha256_digest(_advance_payload(record))
    if actual != record.advance_digest:
        raise ValueError(
            f"operational advance digest mismatch for {path}: "
            f"stored {record.advance_digest}, calculated {actual}"
        )
    return record


def _append_operation_event(
    operation: OperationalRunRecord,
    *,
    event_type: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    path = operation.output_directory / OPERATION_EVENTS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "artifact_type": "spires_batch_operational_event",
        "schema_version": operation.schema_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
        "operational_run_id": operation.operational_run_id,
        "manifest_family_id": operation.manifest_family_id,
        "message": message,
        "details": details or {},
    }
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o664)
    try:
        os.write(
            descriptor,
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8"),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive_text(
    path: Path,
    text: str,
    *,
    executable: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise FileExistsError(f"refusing to replace operational file {path}") from exc
    if executable:
        path.chmod(path.stat().st_mode | 0o110)


def _coordinator_script(
    operation_path: Path,
    wave_directory: Path,
    *,
    cluster: str,
    partition: str,
    account: str | None,
    qos: str | None,
    environment_name: str,
) -> str:
    directives = [
        "#!/bin/bash",
        f"#SBATCH --clusters={cluster}",
        "#SBATCH --job-name=spires-e5-advance",
        f"#SBATCH --partition={partition}",
        "#SBATCH --time=00:10:00",
        "#SBATCH --cpus-per-task=1",
        "#SBATCH --mem=1G",
        f"#SBATCH --output={wave_directory}/coordinator-%j.out",
        f"#SBATCH --error={wave_directory}/coordinator-%j.err",
    ]
    if account is not None:
        directives.append(f"#SBATCH --account={account}")
    if qos is not None:
        directives.append(f"#SBATCH --qos={qos}")
    body = ["", "set -euo pipefail"]
    if cluster == "blanca":
        body.append("module try-load slurm/blanca")
    body.extend(
        [
            "module load miniforge",
            (
                f"mamba run -n {shlex.quote(environment_name)} "
                "spires-batch submission advance "
                f"{shlex.quote(str(operation_path))} "
                f"--wave-dir {shlex.quote(str(wave_directory))}"
            ),
            "",
        ]
    )
    return "\n".join((*directives, *body))


def _submit_coordinator(
    operation_path: Path,
    operation: OperationalRunRecord,
    wave_directory: Path,
    plan: ResolvedPlan,
    scheduler_record: SchedulerSubmissionRecord,
) -> str:
    clusters = {group.cluster for group in scheduler_record.groups}
    if len(clusters) != 1:
        raise ValueError(
            "operational waves must use exactly one Slurm cluster so one "
            "afterany coordinator can reconcile the complete wave"
        )
    submission_record = load_submission_record(
        wave_directory / SUBMISSION_RECORD_NAME
    )
    profile_names = {
        group.resource_profile for group in submission_record.groups
    }
    profiles = {profile.name: profile for profile in plan.resource_profiles}
    environments = {profiles[name].environment_name for name in profile_names}
    if len(environments) != 1:
        raise ValueError(
            "operational waves must use one environment for their coordinator"
        )
    first_profile = profiles[sorted(profile_names)[0]]
    cluster = next(iter(clusters))
    script_path = wave_directory / COORDINATOR_SCRIPT_NAME
    _write_exclusive_text(
        script_path,
        _coordinator_script(
            operation_path,
            wave_directory,
            cluster=cluster,
            partition=first_profile.partition,
            account=first_profile.account,
            qos=first_profile.qos,
            environment_name=next(iter(environments)),
        ),
        executable=True,
    )
    test_command = ("sbatch", "--test-only", str(script_path))
    test_result = _run_sbatch(test_command)
    if test_result.returncode != 0:
        raise RuntimeError(
            "operational coordinator failed Slurm test-only validation: "
            f"{_response_text(test_result) or f'exit {test_result.returncode}'}"
        )
    dependency = ":".join(group.job_id for group in scheduler_record.groups)
    command = (
        "sbatch",
        "--parsable",
        f"--dependency=afterany:{dependency}",
        str(script_path),
    )
    result = _run_sbatch(command)
    if result.returncode != 0:
        raise RuntimeError(
            "operational coordinator submission failed: "
            f"{_response_text(result) or f'exit {result.returncode}'}"
        )
    job_id, raw_response = _parse_job_response(
        result.stdout,
        expected_cluster=cluster,
    )
    record = {
        "artifact_type": "spires_batch_operational_coordinator_submission",
        "schema_version": operation.schema_version,
        "submitted_at": datetime.now(timezone.utc),
        "operational_run_id": operation.operational_run_id,
        "wave_directory": wave_directory,
        "cluster": cluster,
        "job_id": job_id,
        "raw_response": raw_response,
        "dependency_job_ids": tuple(
            group.job_id for group in scheduler_record.groups
        ),
        "command": command,
        "script_path": script_path,
        "script_sha256": file_sha256(script_path),
    }
    write_immutable_json(
        wave_directory / COORDINATOR_RECORD_NAME,
        _JSON_OBJECT_ADAPTER.dump_python(record, mode="json"),
    )
    _append_operation_event(
        operation,
        event_type="coordinator_submitted",
        message=(
            f"afterany coordinator {cluster}/{job_id} will reconcile "
            f"{wave_directory.name}"
        ),
        details={
            "wave_directory": str(wave_directory),
            "job_id": job_id,
            "dependency_job_ids": list(record["dependency_job_ids"]),
        },
    )
    return job_id


def _launch_wave(
    operation_path: Path,
    operation: OperationalRunRecord,
    *,
    wave_number: int,
    kind: str,
    manifest_path: Path,
    task_ids: tuple[str, ...] | None,
    attempt_number: int,
    rearm_failed: bool,
) -> tuple[Path, SchedulerSubmissionRecord, str]:
    wave_directory = (
        operation.output_directory / "waves" / f"{wave_number:04d}-{kind}"
    )
    record = prepare_submission(
        manifest_path,
        state_root=operation.state_root,
        output_directory=wave_directory,
        task_ids=task_ids,
        attempt_number=attempt_number,
        allow_failed_reservations=rearm_failed,
    )
    plan = load_plan(manifest_path)
    profiles = {profile.name: profile for profile in plan.resource_profiles}
    wave_profiles = tuple(
        profiles[group.resource_profile] for group in record.groups
    )
    if len({profile.cluster for profile in wave_profiles}) != 1:
        raise ValueError(
            "operational wave groups must use one Slurm cluster"
        )
    if len({profile.environment_name for profile in wave_profiles}) != 1:
        raise ValueError(
            "operational wave groups must use one coordinator environment"
        )
    reservation_set = acquire_submission_reservations(
        wave_directory / SUBMISSION_RECORD_NAME,
        rearm_failed=rearm_failed,
    )
    test_scheduler_submission(wave_directory / RESERVATION_SET_NAME)
    scheduler_record = submit_scheduler_submission(
        wave_directory / RESERVATION_SET_NAME
    )
    try:
        coordinator_job_id = _submit_coordinator(
            operation_path,
            operation,
            wave_directory,
            plan,
            scheduler_record,
        )
    except Exception as exc:
        _append_operation_event(
            operation,
            event_type="coordinator_submission_failed",
            message=str(exc),
            details={
                "wave_number": wave_number,
                "wave_directory": str(wave_directory),
                "submitted_groups": [
                    {
                        "group_id": group.group_id,
                        "cluster": group.cluster,
                        "job_id": group.job_id,
                    }
                    for group in scheduler_record.groups
                ],
                "manual_advance_required": True,
            },
        )
        raise RuntimeError(
            "wave jobs were submitted but the afterany coordinator was not; "
            f"inspect {wave_directory} and advance it manually after every "
            "array is terminal"
        ) from exc
    _append_operation_event(
        operation,
        event_type="wave_submitted",
        message=(
            f"submitted {kind} wave {wave_number} with "
            f"{len(record.groups)} group(s)"
        ),
        details={
            "wave_number": wave_number,
            "kind": kind,
            "manifest_path": str(manifest_path),
            "task_ids": [
                task_id for group in record.groups for task_id in group.task_ids
            ],
            "attempt": attempt_number,
            "reservation_set_digest": reservation_set.reservation_set_digest,
            "scheduler_submission_digest": (
                scheduler_record.scheduler_submission_digest
            ),
            "coordinator_job_id": coordinator_job_id,
        },
    )
    return wave_directory, scheduler_record, coordinator_job_id


def start_operational_run(
    manifest_path: str | Path,
    *,
    state_root: str | Path,
    output_directory: str | Path,
) -> OperationalLaunch:
    """Create an operational run and submit only its dependency-free first wave."""
    manifest = _resolved(manifest_path)
    state = _resolved(state_root)
    output = _resolved(output_directory)
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(
            f"operational output directory must be empty: {output}"
        )
    plan = load_plan(manifest)
    root_task_ids = tuple(
        task.task_id for task in plan.tasks if not task.depends_on
    )
    if not root_task_ids:
        raise ValueError("operational plan has no dependency-free tasks")
    created_at = datetime.now(timezone.utc)
    payload = {
        "artifact_type": "spires_batch_operational_run",
        "schema_version": plan.schema_version,
        "created_at": created_at,
        "manifest_family_id": plan.manifest_family_id,
        "config_digest": plan.config_digest,
        "plan_digest": plan.plan_digest,
        "manifest_path": manifest,
        "manifest_sha256": file_sha256(manifest),
        "state_root": state,
        "output_directory": output,
    }
    digest = sha256_digest(_operation_payload(payload))
    operation = OperationalRunRecord(
        **payload,
        operational_run_digest=digest,
        operational_run_id=(
            f"{plan.manifest_family_id}-operational-"
            f"{digest.split(':', 1)[1][:12]}"
        ),
    )
    operation_path = output / OPERATION_RECORD_NAME
    write_immutable_json(operation_path, operation)
    _append_operation_event(
        operation,
        event_type="operational_run_created",
        message="stage-gated operational run created",
        details={"root_task_ids": list(root_task_ids)},
    )
    wave_directory, scheduler_record, coordinator_job_id = _launch_wave(
        operation_path,
        operation,
        wave_number=1,
        kind="initial",
        manifest_path=manifest,
        task_ids=root_task_ids,
        attempt_number=1,
        rearm_failed=False,
    )
    return OperationalLaunch(
        operation=operation,
        wave_directory=wave_directory,
        scheduler_submission=scheduler_record,
        coordinator_job_id=coordinator_job_id,
    )


def _default_sacct_runner(
    command: tuple[str, ...],
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("sacct executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"sacct timed out: {shlex.join(command)}") from exc


def _normalize_slurm_state(value: str) -> str:
    return value.strip().split(maxsplit=1)[0].rstrip("+")


def _parse_sacct_time(value: str) -> datetime | None:
    normalized = value.strip()
    if not normalized or normalized in {"Unknown", "N/A", "None"}:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _scheduler_failure_attempt(
    *,
    task_id: str,
    attempt_number: int,
    job_id: str,
    array_task_id: str,
    state: str,
    exit_code: str,
    started_at: datetime | None,
    ended_at: datetime | None,
) -> TaskAttempt:
    normalized = _normalize_slurm_state(state)
    if normalized not in _TERMINAL_SLURM_STATES:
        raise RuntimeError(
            f"Slurm task {job_id}_{array_task_id} is not terminal: {state!r}"
        )
    if normalized in _TRANSIENT_SLURM_STATES:
        failure_class = FailureClass.TRANSIENT
        failure_code = f"scheduler_{normalized.lower()}"
    elif normalized == "CANCELLED":
        failure_class = FailureClass.CANCELLED
        failure_code = "scheduler_cancelled"
    elif normalized == "COMPLETED":
        failure_class = FailureClass.DETERMINISTIC
        failure_code = "missing_terminal_event"
    else:
        failure_class = FailureClass.DETERMINISTIC
        failure_code = f"scheduler_{normalized.lower()}"
    return TaskAttempt(
        task_id=task_id,
        attempt=attempt_number,
        status=TaskStatus.FAILED,
        started_at=started_at,
        ended_at=ended_at or datetime.now(timezone.utc),
        failure_class=failure_class,
        failure_code=failure_code,
        message=(
            f"Slurm terminal state {normalized} with exit code "
            f"{exit_code or 'unknown'}; worker emitted no terminal event"
        ),
        slurm_job_id=job_id,
        slurm_array_task_id=array_task_id,
    )


def _query_scheduler_attempt(
    group: SchedulerSubmissionGroup,
    *,
    task_id: str,
    array_task_id: str,
    attempt_number: int,
    runner: SacctRunner,
) -> TaskAttempt:
    command = (
        "sacct",
        "-n",
        "-P",
        "-M",
        group.cluster,
        "-j",
        group.job_id,
        "--format=JobIDRaw,State,ExitCode,Start,End",
    )
    result = runner(command)
    if result.returncode != 0:
        response = "\n".join(
            item.strip()
            for item in (result.stdout, result.stderr)
            if item and item.strip()
        )
        raise RuntimeError(
            f"sacct failed for {group.cluster}/{group.job_id}: "
            f"{response or f'exit {result.returncode}'}"
        )
    expected_job = f"{group.job_id}_{array_task_id}"
    accepted_job_ids = {expected_job}
    if len(group.task_ids) == 1 and array_task_id == "0":
        # Slurm may collapse a singleton ``--array=0-0`` allocation to the
        # base job identity in accounting even though the worker still has
        # ``SLURM_ARRAY_TASK_ID=0``.
        accepted_job_ids.add(group.job_id)
    for line in result.stdout.splitlines():
        fields = line.split("|")
        if len(fields) < 5 or fields[0].strip() not in accepted_job_ids:
            continue
        return _scheduler_failure_attempt(
            task_id=task_id,
            attempt_number=attempt_number,
            job_id=group.job_id,
            array_task_id=array_task_id,
            state=fields[1],
            exit_code=fields[2],
            started_at=_parse_sacct_time(fields[3]),
            ended_at=_parse_sacct_time(fields[4]),
        )
    raise RuntimeError(
        f"sacct returned no array-element record for {group.cluster}/{expected_job}"
    )


def _wave_attempts(
    wave_directory: Path,
    scheduler_record: SchedulerSubmissionRecord,
    submission_attempt: int,
    *,
    runner: SacctRunner,
) -> tuple[TaskAttempt, ...]:
    events_directory = wave_directory / "events"
    event_paths = tuple(sorted(events_directory.glob("*.jsonl")))
    recorded_attempts = attempts_from_events(read_event_logs(event_paths))
    by_identity = {
        (
            attempt.task_id,
            attempt.attempt,
            attempt.slurm_job_id,
            attempt.slurm_array_task_id,
        ): attempt
        for attempt in recorded_attempts
    }
    attempts: list[TaskAttempt] = []
    for group in scheduler_record.groups:
        for array_index, task_id in enumerate(group.task_ids):
            array_task_id = str(array_index)
            identity = (
                task_id,
                submission_attempt,
                group.job_id,
                array_task_id,
            )
            attempt = by_identity.get(identity)
            if attempt is None:
                attempt = _query_scheduler_attempt(
                    group,
                    task_id=task_id,
                    array_task_id=array_task_id,
                    attempt_number=submission_attempt,
                    runner=runner,
                )
                write_attempt(
                    EventLog(events_directory / f"{task_id}.jsonl"),
                    load_submission_record(
                        wave_directory / SUBMISSION_RECORD_NAME
                    ).run_id,
                    attempt,
                )
            attempts.append(attempt)
    return tuple(attempts)


def _reconcile_wave_reservations(
    wave_directory: Path,
    scheduler_record: SchedulerSubmissionRecord,
    attempts: Iterable[TaskAttempt],
) -> None:
    reservation_set = load_reservation_set(
        wave_directory / RESERVATION_SET_NAME
    )
    submission = load_submission_record(
        wave_directory / SUBMISSION_RECORD_NAME
    )
    plan = load_plan(submission.manifest_path)
    tasks = {task.task_id: task for task in plan.tasks}
    groups_by_task: dict[str, tuple[SchedulerSubmissionGroup, int]] = {}
    for group in scheduler_record.groups:
        for array_index, task_id in enumerate(group.task_ids):
            groups_by_task[task_id] = (group, array_index)
    store = ReservationStore(reservation_set.state_root)
    for attempt in attempts:
        group, array_index = groups_by_task[attempt.task_id]
        terminalized = store.reconcile_scheduler_terminal(
            reservation_set,
            tasks[attempt.task_id],
            attempt,
            group_id=group.group_id,
            cluster=group.cluster,
            job_id=group.job_id,
            array_task_id=str(array_index),
        )
        append_submission_event(
            wave_directory / "submission-events.jsonl",
            SubmissionEvent(
                timestamp=datetime.now(timezone.utc),
                event_type="controller_reservations_reconciled",
                submission_id=submission.submission_id,
                run_id=submission.run_id,
                plan_digest=submission.plan_digest,
                message=(
                    f"controller reconciled {len(terminalized)} reservation(s) "
                    f"from task status {attempt.status.value}"
                ),
                details={
                    "task_id": attempt.task_id,
                    "attempt": attempt.attempt,
                    "reservation_ids": [
                        reservation.reservation_id
                        for reservation in terminalized
                    ],
                },
            ),
        )


def _all_operational_attempts(
    operation: OperationalRunRecord,
) -> tuple[TaskAttempt, ...]:
    paths = tuple(
        sorted(
            (
                *operation.output_directory.glob("waves/*/events/*.jsonl"),
                *operation.output_directory.glob("terminal-events/*.jsonl"),
            )
        )
    )
    return attempts_from_events(read_event_logs(paths))


def _latest_attempts(
    attempts: Iterable[TaskAttempt],
) -> dict[str, TaskAttempt]:
    latest: dict[str, TaskAttempt] = {}
    for attempt in attempts:
        previous = latest.get(attempt.task_id)
        if previous is None or attempt.attempt > previous.attempt:
            latest[attempt.task_id] = attempt
    return latest


def _block_remaining_tasks(
    operation: OperationalRunRecord,
    plan: ResolvedPlan,
    latest: dict[str, TaskAttempt],
    *,
    message: str,
) -> tuple[str, ...]:
    blocked: list[str] = []
    now = datetime.now(timezone.utc)
    directory = operation.output_directory / "terminal-events"
    for task in plan.tasks:
        if task.task_id in latest:
            continue
        attempt = TaskAttempt(
            task_id=task.task_id,
            attempt=1,
            status=TaskStatus.BLOCKED,
            started_at=now,
            ended_at=now,
            message=message,
        )
        write_attempt(
            EventLog(directory / f"{task.task_id}.jsonl"),
            plan.run_id,
            attempt,
        )
        blocked.append(task.task_id)
    return tuple(blocked)


def _write_advance(
    operation: OperationalRunRecord,
    wave_directory: Path,
    *,
    wave_number: int,
    status: str,
    task_ids: tuple[str, ...],
    next_wave_directory: Path | None,
    message: str,
) -> OperationalAdvanceRecord:
    payload = {
        "artifact_type": "spires_batch_operational_advance",
        "schema_version": operation.schema_version,
        "advanced_at": datetime.now(timezone.utc),
        "operational_run_id": operation.operational_run_id,
        "completed_wave": wave_number,
        "status": status,
        "task_ids": task_ids,
        "next_wave_directory": next_wave_directory,
        "message": message,
    }
    digest = sha256_digest(_advance_payload(payload))
    record = OperationalAdvanceRecord(**payload, advance_digest=digest)
    write_immutable_json(wave_directory / ADVANCE_RECORD_NAME, record)
    return record


def _write_terminal_operation(
    operation: OperationalRunRecord,
    advance: OperationalAdvanceRecord,
) -> None:
    write_immutable_json(
        operation.output_directory / OPERATION_TERMINAL_NAME,
        _JSON_OBJECT_ADAPTER.dump_python(
            {
                "artifact_type": "spires_batch_operational_result",
                "schema_version": operation.schema_version,
                "operational_run_id": operation.operational_run_id,
                "manifest_family_id": operation.manifest_family_id,
                "completed_at": advance.advanced_at,
                "status": advance.status,
                "message": advance.message,
                "terminal_advance_digest": advance.advance_digest,
            },
            mode="json",
        ),
    )


def advance_operational_run(
    operation_path: str | Path,
    *,
    wave_directory: str | Path,
    sacct_runner: SacctRunner | None = None,
) -> OperationalAdvanceRecord:
    """Reconcile one terminal wave, retry it, or submit the next ready wave."""
    operation_source = _resolved(operation_path)
    operation = load_operational_run(operation_source)
    wave = _resolved(wave_directory)
    waves_root = (operation.output_directory / "waves").resolve()
    if wave.parent != waves_root:
        raise ValueError(
            f"wave directory is outside operational run: {wave}"
        )
    try:
        wave_number = int(wave.name.split("-", 1)[0])
    except ValueError as exc:
        raise ValueError(f"invalid operational wave directory {wave.name!r}") from exc
    advance_path = wave / ADVANCE_RECORD_NAME
    if advance_path.exists():
        return load_operational_advance(advance_path)

    lock_path = wave / ADVANCE_LOCK_NAME
    try:
        lock_descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o664,
        )
    except FileExistsError as exc:
        raise RuntimeError(
            f"operational wave is already being advanced: {lock_path}"
        ) from exc
    os.write(
        lock_descriptor,
        (
            json.dumps(
                {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "pid": os.getpid(),
                },
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )
    os.fsync(lock_descriptor)
    os.close(lock_descriptor)
    completed = False
    scheduler_mutation_started = False
    try:
        submission = load_submission_record(wave / SUBMISSION_RECORD_NAME)
        scheduler_record = load_scheduler_submission_record(
            wave / SCHEDULER_SUBMISSION_NAME
        )
        attempts = _wave_attempts(
            wave,
            scheduler_record,
            submission.attempt,
            runner=sacct_runner or _default_sacct_runner,
        )
        _reconcile_wave_reservations(wave, scheduler_record, attempts)
        plan = load_plan(operation.manifest_path)
        all_attempts = _all_operational_attempts(operation)
        latest = _latest_attempts(all_attempts)
        current_failures = tuple(
            attempt for attempt in attempts if attempt.status not in _SUCCESS_STATUSES
        )
        next_wave_number = wave_number + 1

        if current_failures:
            retryable = tuple(
                attempt
                for attempt in current_failures
                if attempt.status == TaskStatus.FAILED
                and attempt.failure_class == FailureClass.TRANSIENT
                and attempt.attempt
                <= plan.request.execution.max_auto_retry_count
            )
            if retryable:
                retry_sequence = (
                    len(
                        tuple(
                            (
                                operation.output_directory / "manifests"
                            ).glob("retry-*.json")
                        )
                    )
                    + 1
                )
                retry_plan = build_retry_plan(
                    plan,
                    all_attempts,
                    eligible_task_ids=(
                        attempt.task_id for attempt in retryable
                    ),
                    retry_number=retry_sequence,
                )
                if not retry_plan.tasks:
                    raise RuntimeError(
                        "retry eligibility changed while building retry manifest"
                    )
                retry_manifest = (
                    operation.output_directory
                    / "manifests"
                    / f"retry-{retry_sequence:04d}.json"
                )
                write_plan(retry_manifest, retry_plan)
                retry_attempt = max(
                    attempt.attempt for attempt in retryable
                ) + 1
                scheduler_mutation_started = True
                next_wave, _, _ = _launch_wave(
                    operation_source,
                    operation,
                    wave_number=next_wave_number,
                    kind="retry",
                    manifest_path=retry_manifest,
                    task_ids=None,
                    attempt_number=retry_attempt,
                    rearm_failed=True,
                )
                message = (
                    f"submitted capped retry attempt {retry_attempt} for "
                    f"{len(retry_plan.tasks)} transient failure(s)"
                )
                advance = _write_advance(
                    operation,
                    wave,
                    wave_number=wave_number,
                    status="retry_submitted",
                    task_ids=tuple(
                        task.task_id for task in retry_plan.tasks
                    ),
                    next_wave_directory=next_wave,
                    message=message,
                )
                _append_operation_event(
                    operation,
                    event_type="retry_submitted",
                    message=message,
                    details={
                        "retry_manifest": str(retry_manifest),
                        "next_wave": str(next_wave),
                    },
                )
                completed = True
                return advance

            failure_message = (
                "strict dependency execution stopped after deterministic, "
                "cancelled, or retry-exhausted task failure"
            )
            blocked = _block_remaining_tasks(
                operation,
                plan,
                latest,
                message=failure_message,
            )
            advance = _write_advance(
                operation,
                wave,
                wave_number=wave_number,
                status="failed",
                task_ids=tuple(
                    attempt.task_id for attempt in current_failures
                )
                + blocked,
                next_wave_directory=None,
                message=failure_message,
            )
            _write_terminal_operation(operation, advance)
            _append_operation_event(
                operation,
                event_type="operational_run_failed",
                message=failure_message,
                details={
                    "failed_task_ids": [
                        attempt.task_id for attempt in current_failures
                    ],
                    "blocked_task_ids": list(blocked),
                },
            )
            completed = True
            return advance

        family_failures = tuple(
            attempt
            for attempt in latest.values()
            if attempt.status not in _SUCCESS_STATUSES
        )
        if family_failures:
            failure_message = (
                "strict dependency execution stopped because the manifest "
                "family retains a deterministic, cancelled, or "
                "retry-exhausted task failure"
            )
            blocked = _block_remaining_tasks(
                operation,
                plan,
                latest,
                message=failure_message,
            )
            advance = _write_advance(
                operation,
                wave,
                wave_number=wave_number,
                status="failed",
                task_ids=tuple(
                    attempt.task_id for attempt in family_failures
                )
                + blocked,
                next_wave_directory=None,
                message=failure_message,
            )
            _write_terminal_operation(operation, advance)
            _append_operation_event(
                operation,
                event_type="operational_run_failed",
                message=failure_message,
                details={
                    "failed_task_ids": [
                        attempt.task_id for attempt in family_failures
                    ],
                    "blocked_task_ids": list(blocked),
                },
            )
            completed = True
            return advance

        successful = {
            task_id
            for task_id, attempt in latest.items()
            if attempt.status in _SUCCESS_STATUSES
        }
        unattempted = tuple(
            task for task in plan.tasks if task.task_id not in latest
        )
        ready = tuple(
            task
            for task in unattempted
            if set(task.depends_on).issubset(successful)
        )
        if ready:
            scheduler_mutation_started = True
            next_wave, _, _ = _launch_wave(
                operation_source,
                operation,
                wave_number=next_wave_number,
                kind="downstream",
                manifest_path=operation.manifest_path,
                task_ids=tuple(task.task_id for task in ready),
                attempt_number=1,
                rearm_failed=False,
            )
            message = (
                f"released {len(ready)} downstream task(s) after every "
                "required upstream task reached validated success"
            )
            advance = _write_advance(
                operation,
                wave,
                wave_number=wave_number,
                status="downstream_submitted",
                task_ids=tuple(task.task_id for task in ready),
                next_wave_directory=next_wave,
                message=message,
            )
            _append_operation_event(
                operation,
                event_type="downstream_submitted",
                message=message,
                details={"next_wave": str(next_wave)},
            )
            completed = True
            return advance

        if unattempted:
            message = (
                "strict dependency graph has remaining tasks but none are "
                "eligible for release"
            )
            blocked = _block_remaining_tasks(
                operation,
                plan,
                latest,
                message=message,
            )
            advance = _write_advance(
                operation,
                wave,
                wave_number=wave_number,
                status="failed",
                task_ids=blocked,
                next_wave_directory=None,
                message=message,
            )
            _write_terminal_operation(operation, advance)
            completed = True
            return advance

        message = "all operational tasks reached validated terminal success"
        advance = _write_advance(
            operation,
            wave,
            wave_number=wave_number,
            status="succeeded",
            task_ids=tuple(attempt.task_id for attempt in attempts),
            next_wave_directory=None,
            message=message,
        )
        _write_terminal_operation(operation, advance)
        _append_operation_event(
            operation,
            event_type="operational_run_succeeded",
            message=message,
        )
        completed = True
        return advance
    finally:
        if completed or not scheduler_mutation_started:
            lock_path.unlink(missing_ok=True)
