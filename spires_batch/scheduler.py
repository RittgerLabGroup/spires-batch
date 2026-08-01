"""Audited Slurm test-only validation and live submission."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from spires_batch.models import (
    Reservation,
    ReservationSet,
    ReservationState,
    ResolvedPlan,
    SchedulerSubmissionGroup,
    SchedulerSubmissionRecord,
    SchedulerTestGroup,
    SchedulerTestRecord,
    SubmissionEvent,
    SubmissionRecord,
)
from spires_batch.reservations import ReservationConflict, ReservationStore
from spires_batch.serialization import load_json_object, sha256_digest, write_immutable_json
from spires_batch.submission import (
    SUBMISSION_EVENTS_NAME,
    SUBMISSION_RECORD_NAME,
    _verify_submission_artifacts,
    append_submission_event,
    load_reservation_set,
    load_submission_record,
    validate_submission_readiness,
)


SCHEDULER_TEST_NAME = "scheduler-test.json"
SCHEDULER_SUBMISSION_NAME = "scheduler-submission.json"
SCHEDULER_SUBMIT_LOCK_NAME = "scheduler-submit.lock"
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, Any])


class SchedulerSubmissionError(RuntimeError):
    """A Slurm validation or mutation could not be completed safely."""


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _serialize_payload(
    value: SchedulerTestRecord | SchedulerSubmissionRecord | dict[str, Any],
    *,
    digest_field: str,
) -> dict[str, Any]:
    if isinstance(value, (SchedulerTestRecord, SchedulerSubmissionRecord)):
        return value.model_dump(mode="json", exclude={digest_field})
    filtered = {key: item for key, item in value.items() if key != digest_field}
    return _JSON_OBJECT_ADAPTER.dump_python(filtered, mode="json")


def deterministic_scheduler_test_payload(
    value: SchedulerTestRecord | dict[str, Any],
) -> dict[str, Any]:
    return _serialize_payload(value, digest_field="scheduler_test_digest")


def deterministic_scheduler_submission_payload(
    value: SchedulerSubmissionRecord | dict[str, Any],
) -> dict[str, Any]:
    return _serialize_payload(value, digest_field="scheduler_submission_digest")


def _submission_record_path(
    reservation_set_path: str | Path,
    reservation_set: ReservationSet,
) -> Path:
    if reservation_set.submission_record_path is not None:
        return reservation_set.submission_record_path
    return _resolved(reservation_set_path).parent / SUBMISSION_RECORD_NAME


def _verify_current_reservations(
    record: SubmissionRecord,
    reservation_set: ReservationSet,
    *,
    require_unsubmitted: bool,
) -> ReservationStore:
    expected_intents = {
        intent.reservation_id: intent for intent in record.reservation_intents
    }
    recorded = {
        reservation.reservation_id: reservation
        for reservation in reservation_set.reservations
    }
    if set(expected_intents) != set(recorded):
        raise SchedulerSubmissionError(
            "reservation set does not exactly cover the submission outputs"
        )

    store = ReservationStore(reservation_set.state_root)
    for expected in reservation_set.reservations:
        current = store.load(expected.output_path)
        if current is None:
            raise SchedulerSubmissionError(
                f"required reservation is missing: {expected.output_path}"
            )
        if (
            current.reservation_id != expected.reservation_id
            or current.run_id != record.run_id
            or current.task_id != expected.task_id
            or current.plan_digest != record.plan_digest
            or current.submission_id != record.submission_id
        ):
            raise ReservationConflict(current)
        if current.state != ReservationState.ACTIVE:
            raise SchedulerSubmissionError(
                f"reservation {current.reservation_id} is "
                f"{current.state.value!r}, not active"
            )
        if require_unsubmitted and current.slurm_job_id is not None:
            raise SchedulerSubmissionError(
                f"reservation {current.reservation_id} is already attached to "
                f"Slurm job {current.slurm_job_id}"
            )
    return store


def _load_scheduler_context(
    reservation_set_path: str | Path,
    *,
    require_unsubmitted: bool = True,
) -> tuple[ReservationSet, SubmissionRecord, ResolvedPlan, ReservationStore]:
    reservation_set = load_reservation_set(reservation_set_path)
    record = load_submission_record(
        _submission_record_path(reservation_set_path, reservation_set)
    )
    if (
        reservation_set.submission_id != record.submission_id
        or reservation_set.run_id != record.run_id
        or reservation_set.plan_digest != record.plan_digest
        or reservation_set.state_root != record.state_root
    ):
        raise SchedulerSubmissionError(
            "reservation set does not match its immutable submission record"
        )
    plan = _verify_submission_artifacts(record)
    validate_submission_readiness(
        plan,
        manifest_path=record.manifest_path,
        state_root=record.state_root,
        check_reservations=False,
    )
    store = _verify_current_reservations(
        record,
        reservation_set,
        require_unsubmitted=require_unsubmitted,
    )
    return reservation_set, record, plan, store


def _profile_by_name(plan: ResolvedPlan) -> dict[str, Any]:
    return {profile.name: profile for profile in plan.resource_profiles}


def _command_text(command: tuple[str, ...]) -> str:
    return shlex.join(command)


def _run_sbatch(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        raise SchedulerSubmissionError("sbatch executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise SchedulerSubmissionError(
            f"Slurm command timed out: {_command_text(command)}"
        ) from exc


def _response_text(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(
        item.strip()
        for item in (result.stdout, result.stderr)
        if item and item.strip()
    )


def test_scheduler_submission(
    reservation_set_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> SchedulerTestRecord:
    """Run Slurm's non-mutating validation for every prepared array script."""
    reservation_set, record, plan, _ = _load_scheduler_context(
        reservation_set_path
    )
    destination = (
        record.output_directory / SCHEDULER_TEST_NAME
        if output_path is None
        else _resolved(output_path)
    )
    if destination.exists():
        raise FileExistsError(
            f"refusing to replace immutable scheduler test record {destination}"
        )

    profiles = _profile_by_name(plan)
    tested_groups: list[SchedulerTestGroup] = []
    for group in record.groups:
        profile = profiles[group.resource_profile]
        command = ("sbatch", "--test-only", str(group.script_path))
        result = _run_sbatch(command)
        response = _response_text(result)
        tested_at = datetime.now(timezone.utc)
        if result.returncode != 0:
            append_submission_event(
                record.output_directory / SUBMISSION_EVENTS_NAME,
                SubmissionEvent(
                    timestamp=tested_at,
                    event_type="scheduler_test_failed",
                    submission_id=record.submission_id,
                    run_id=record.run_id,
                    plan_digest=record.plan_digest,
                    message=response or f"sbatch exited {result.returncode}",
                    details={
                        "group_id": group.group_id,
                        "cluster": profile.cluster,
                        "command": _command_text(command),
                    },
                ),
            )
            raise SchedulerSubmissionError(
                f"Slurm test-only failed for {group.group_id}: "
                f"{response or f'exit {result.returncode}'}"
            )
        tested_groups.append(
            SchedulerTestGroup(
                group_id=group.group_id,
                cluster=profile.cluster,
                tested_at=tested_at,
                command=command,
                response=response,
            )
        )

    payload = {
        "artifact_type": "spires_batch_scheduler_test",
        "schema_version": record.schema_version,
        "tested_at": datetime.now(timezone.utc),
        "submission_id": record.submission_id,
        "submission_digest": record.submission_digest,
        "reservation_set_digest": reservation_set.reservation_set_digest,
        "run_id": record.run_id,
        "plan_digest": record.plan_digest,
        "groups": tuple(tested_groups),
    }
    digest = sha256_digest(deterministic_scheduler_test_payload(payload))
    test_record = SchedulerTestRecord(
        **payload,
        scheduler_test_digest=digest,
    )
    write_immutable_json(destination, test_record)
    append_submission_event(
        record.output_directory / SUBMISSION_EVENTS_NAME,
        SubmissionEvent(
            timestamp=datetime.now(timezone.utc),
            event_type="scheduler_test_passed",
            submission_id=record.submission_id,
            run_id=record.run_id,
            plan_digest=record.plan_digest,
            message="all Slurm test-only checks passed; no job was submitted",
            details={
                "scheduler_test": str(destination),
                "groups": len(tested_groups),
            },
        ),
    )
    return test_record


def load_scheduler_test_record(path: str | Path) -> SchedulerTestRecord:
    record = SchedulerTestRecord.model_validate(load_json_object(path))
    actual = sha256_digest(deterministic_scheduler_test_payload(record))
    if actual != record.scheduler_test_digest:
        raise ValueError(
            f"scheduler test digest mismatch for {path}: "
            f"stored {record.scheduler_test_digest}, calculated {actual}"
        )
    return record


def _parse_job_response(
    response: str,
    *,
    expected_cluster: str,
) -> tuple[str, str]:
    lines = [line.strip() for line in response.splitlines() if line.strip()]
    if not lines:
        raise SchedulerSubmissionError("sbatch returned an empty response")
    raw_identity = lines[-1]
    components = raw_identity.split(";", 1)
    job_id = components[0]
    if not job_id.isdigit():
        raise SchedulerSubmissionError(
            f"could not parse Slurm job ID from response {response!r}"
        )
    cluster = expected_cluster if len(components) == 1 else components[1]
    if cluster != expected_cluster:
        raise SchedulerSubmissionError(
            f"sbatch returned cluster {cluster!r}, expected {expected_cluster!r}"
        )
    return job_id, raw_identity


def _attach_group_reservations(
    store: ReservationStore,
    reservation_set: ReservationSet,
    record: SubmissionRecord,
    group: SchedulerSubmissionGroup,
) -> tuple[Reservation, ...]:
    by_task: dict[str, list[Reservation]] = {}
    for reservation in reservation_set.reservations:
        by_task.setdefault(reservation.task_id, []).append(reservation)
    attached: list[Reservation] = []
    for array_index, task_id in enumerate(group.task_ids):
        task_reservations = by_task.get(task_id, [])
        if not task_reservations:
            raise SchedulerSubmissionError(
                f"submitted task {task_id!r} has no output reservation"
            )
        for reservation in task_reservations:
            attached.append(
                store.attach_scheduler_job(
                    reservation.output_path,
                    run_id=record.run_id,
                    task_id=task_id,
                    submission_id=record.submission_id,
                    cluster=group.cluster,
                    job_id=group.job_id,
                    array_task_id=str(array_index),
                    group_id=group.group_id,
                )
            )
    return tuple(attached)


def _acquire_submit_lock(path: Path, record: SubmissionRecord) -> None:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o664)
    except FileExistsError as exc:
        raise SchedulerSubmissionError(
            f"scheduler submission lock already exists: {path}"
        ) from exc
    try:
        payload = {
            "submission_id": record.submission_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
        }
        os.write(
            descriptor,
            (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def submit_scheduler_submission(
    reservation_set_path: str | Path,
    *,
    scheduler_test_path: str | Path | None = None,
    output_path: str | Path | None = None,
) -> SchedulerSubmissionRecord:
    """Submit prepared arrays and durably bind returned Slurm job identities."""
    reservation_set, record, plan, store = _load_scheduler_context(
        reservation_set_path
    )
    test_path = (
        record.output_directory / SCHEDULER_TEST_NAME
        if scheduler_test_path is None
        else _resolved(scheduler_test_path)
    )
    test_record = load_scheduler_test_record(test_path)
    if (
        test_record.submission_id != record.submission_id
        or test_record.submission_digest != record.submission_digest
        or test_record.reservation_set_digest
        != reservation_set.reservation_set_digest
        or test_record.plan_digest != record.plan_digest
    ):
        raise SchedulerSubmissionError(
            "scheduler test record does not match the reserved submission"
        )
    if {group.group_id for group in test_record.groups} != {
        group.group_id for group in record.groups
    }:
        raise SchedulerSubmissionError(
            "scheduler test record does not cover every submission group"
        )

    destination = (
        record.output_directory / SCHEDULER_SUBMISSION_NAME
        if output_path is None
        else _resolved(output_path)
    )
    if destination.exists():
        raise FileExistsError(
            f"refusing to replace immutable scheduler submission {destination}"
        )
    lock_path = record.output_directory / SCHEDULER_SUBMIT_LOCK_NAME
    _acquire_submit_lock(lock_path, record)

    profiles = _profile_by_name(plan)
    submitted: list[SchedulerSubmissionGroup] = []
    try:
        submitted_by_group: dict[str, SchedulerSubmissionGroup] = {}
        for group in record.groups:
            profile = profiles[group.resource_profile]
            dependency_job_ids = tuple(
                submitted_by_group[group_id].job_id
                for group_id in group.dependency_group_ids
            )
            command_parts = ["sbatch", "--parsable"]
            if dependency_job_ids:
                command_parts.append(
                    "--dependency=afterok:" + ":".join(dependency_job_ids)
                )
            command_parts.append(str(group.script_path))
            command = tuple(command_parts)
            result = _run_sbatch(command)
            response = _response_text(result)
            if result.returncode != 0:
                raise SchedulerSubmissionError(
                    f"live sbatch failed for {group.group_id}: "
                    f"{response or f'exit {result.returncode}'}"
                )
            job_id, raw_response = _parse_job_response(
                result.stdout,
                expected_cluster=profile.cluster,
            )
            submitted_at = datetime.now(timezone.utc)
            submitted_group = SchedulerSubmissionGroup(
                group_id=group.group_id,
                cluster=profile.cluster,
                submitted_at=submitted_at,
                job_id=job_id,
                raw_response=raw_response,
                command=command,
                task_ids=group.task_ids,
                dependency_job_ids=dependency_job_ids,
            )
            submitted.append(submitted_group)
            submitted_by_group[group.group_id] = submitted_group
            append_submission_event(
                record.output_directory / SUBMISSION_EVENTS_NAME,
                SubmissionEvent(
                    timestamp=submitted_at,
                    event_type="scheduler_group_submitted",
                    submission_id=record.submission_id,
                    run_id=record.run_id,
                    plan_digest=record.plan_digest,
                    message=(
                        f"Slurm group {group.group_id} submitted as "
                        f"{profile.cluster}/{job_id}"
                    ),
                    details={
                        "group_id": group.group_id,
                        "cluster": profile.cluster,
                        "job_id": job_id,
                        "raw_response": raw_response,
                        "command": _command_text(command),
                    },
                ),
            )
            _attach_group_reservations(
                store,
                reservation_set,
                record,
                submitted_group,
            )

        payload = {
            "artifact_type": "spires_batch_scheduler_submission",
            "schema_version": record.schema_version,
            "submitted_at": datetime.now(timezone.utc),
            "submission_id": record.submission_id,
            "submission_digest": record.submission_digest,
            "reservation_set_digest": reservation_set.reservation_set_digest,
            "scheduler_test_digest": test_record.scheduler_test_digest,
            "run_id": record.run_id,
            "plan_digest": record.plan_digest,
            "groups": tuple(submitted),
        }
        digest = sha256_digest(
            deterministic_scheduler_submission_payload(payload)
        )
        scheduler_record = SchedulerSubmissionRecord(
            **payload,
            scheduler_submission_digest=digest,
        )
        write_immutable_json(destination, scheduler_record)
        append_submission_event(
            record.output_directory / SUBMISSION_EVENTS_NAME,
            SubmissionEvent(
                timestamp=datetime.now(timezone.utc),
                event_type="scheduler_submission_recorded",
                submission_id=record.submission_id,
                run_id=record.run_id,
                plan_digest=record.plan_digest,
                message="all Slurm group identities were durably recorded",
                details={
                    "scheduler_submission": str(destination),
                    "groups": len(submitted),
                },
            ),
        )
    except Exception as exc:
        append_submission_event(
            record.output_directory / SUBMISSION_EVENTS_NAME,
            SubmissionEvent(
                timestamp=datetime.now(timezone.utc),
                event_type=(
                    "scheduler_submission_partial"
                    if submitted
                    else "scheduler_submission_failed"
                ),
                submission_id=record.submission_id,
                run_id=record.run_id,
                plan_digest=record.plan_digest,
                message=str(exc),
                details={
                    "submitted_groups": [
                        {
                            "group_id": group.group_id,
                            "cluster": group.cluster,
                            "job_id": group.job_id,
                        }
                        for group in submitted
                    ],
                    "lock_retained": bool(submitted),
                },
            ),
        )
        if not submitted:
            lock_path.unlink(missing_ok=True)
        raise
    lock_path.unlink()
    return scheduler_record


def load_scheduler_submission_record(
    path: str | Path,
) -> SchedulerSubmissionRecord:
    record = SchedulerSubmissionRecord.model_validate(load_json_object(path))
    actual = sha256_digest(deterministic_scheduler_submission_payload(record))
    if actual != record.scheduler_submission_digest:
        raise ValueError(
            f"scheduler submission digest mismatch for {path}: "
            f"stored {record.scheduler_submission_digest}, calculated {actual}"
        )
    return record
