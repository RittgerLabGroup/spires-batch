"""Durable pre-submission intent, readiness, and reservation coordination."""

from __future__ import annotations

import json
import os
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from spires_batch.models import (
    ExistingFileHandling,
    ExistingOutputPolicy,
    ReservationSet,
    ResolvedInput,
    ResolvedPlan,
    SubmissionEvent,
    SubmissionGroupRecord,
    SubmissionReadinessCheck,
    SubmissionRecord,
    SubmissionReservationIntent,
)
from spires_batch.reservations import ReservationBatchError, ReservationStore
from spires_batch.serialization import (
    file_sha256,
    load_json_object,
    load_plan,
    sha256_digest,
    write_immutable_json,
)
from spires_batch.slurm import SlurmArrayGroup, render_slurm


SUBMISSION_RECORD_NAME = "submission.json"
SUBMISSION_EVENTS_NAME = "submission-events.jsonl"
RESERVATION_SET_NAME = "reservation-set.json"
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, Any])


class SubmissionReadinessError(ValueError):
    """The immutable manifest is not currently safe to reserve or submit."""

    def __init__(self, issues: tuple[str, ...]):
        self.issues = issues
        super().__init__("submission readiness failed: " + "; ".join(issues))


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _require_writable_parent(path: Path, *, label: str, issues: list[str]) -> None:
    parent = _nearest_existing_parent(path.parent)
    if not parent.is_dir():
        issues.append(f"{label} has no existing parent directory: {path}")
    elif not os.access(parent, os.W_OK | os.X_OK):
        issues.append(f"{label} parent is not writable: {parent}")


def _validate_external_input(
    item: ResolvedInput,
    *,
    staging_sha256: bool,
    issues: list[str],
) -> None:
    path = item.execution_path
    if not path.is_file():
        issues.append(f"task input is unavailable at execution path: {path}")
        return
    stat = path.stat()
    if item.size_bytes is not None and stat.st_size != item.size_bytes:
        issues.append(
            f"task input size changed for {path}: "
            f"manifest={item.size_bytes}, current={stat.st_size}"
        )
    if item.mtime_ns is not None and stat.st_mtime_ns != item.mtime_ns:
        issues.append(
            f"task input mtime changed for {path}: "
            f"manifest={item.mtime_ns}, current={stat.st_mtime_ns}"
        )
    if item.source_sha256 is not None:
        actual = file_sha256(path)
        if actual != item.source_sha256:
            issues.append(
                f"task input digest changed for {path}: "
                f"manifest={item.source_sha256}, current={actual}"
            )
    if staging_sha256 and item.source_path != item.execution_path:
        if not item.source_path.is_file():
            issues.append(f"staging source is unavailable: {item.source_path}")
        elif file_sha256(item.source_path) != file_sha256(item.execution_path):
            issues.append(
                "staged input does not match its source by SHA-256: "
                f"{item.source_path} -> {item.execution_path}"
            )


def validate_submission_readiness(
    plan: ResolvedPlan,
    *,
    manifest_path: str | Path,
    state_root: str | Path,
    output_directory: str | Path | None = None,
    require_empty_output_directory: bool = False,
    check_reservations: bool = True,
    task_ids: tuple[str, ...] | None = None,
    allow_failed_reservations: bool = False,
) -> tuple[SubmissionReadinessCheck, ...]:
    """Validate all mutable prerequisites immediately before reservation."""
    manifest = _resolved(manifest_path)
    state = _resolved(state_root)
    output_dir = None if output_directory is None else _resolved(output_directory)
    issues: list[str] = []
    checks: list[SubmissionReadinessCheck] = [
        SubmissionReadinessCheck(
            code="manifest_digest_verified",
            message=(
                "manifest and embedded configuration digests were reloaded and verified"
            ),
            path=manifest,
        )
    ]

    if not plan.preflight.passed:
        issues.append("resolved manifest contains failed preflight state")
    else:
        checks.append(
            SubmissionReadinessCheck(
                code="preflight_passed",
                message="resolved manifest records a passing preflight",
            )
        )
    selected_ids = (
        {task.task_id for task in plan.tasks}
        if task_ids is None
        else set(task_ids)
    )
    known_ids = {task.task_id for task in plan.tasks}
    missing_ids = selected_ids - known_ids
    if missing_ids:
        issues.append(f"submission selects unknown task IDs {sorted(missing_ids)}")
    selected_tasks = tuple(
        task for task in plan.tasks if task.task_id in selected_ids
    )
    if not selected_tasks:
        issues.append("resolved manifest contains no tasks")

    if not state.is_dir():
        issues.append(f"reservation state root is not an existing directory: {state}")
    elif not os.access(state, os.W_OK | os.X_OK):
        issues.append(f"reservation state root is not writable: {state}")
    else:
        checks.append(
            SubmissionReadinessCheck(
                code="state_root_writable",
                message="reservation state root exists and is writable",
                path=state,
            )
        )

    if output_dir is not None:
        if require_empty_output_directory and output_dir.exists():
            if not output_dir.is_dir():
                issues.append(
                    f"submission output path is not a directory: {output_dir}"
                )
            elif any(output_dir.iterdir()):
                issues.append(
                    "submission output directory must be empty so its artifacts "
                    f"remain immutable: {output_dir}"
                )
        if output_dir.is_dir():
            if not os.access(output_dir, os.W_OK | os.X_OK):
                issues.append(
                    f"submission output directory is not writable: {output_dir}"
                )
        else:
            _require_writable_parent(
                output_dir,
                label="submission output directory",
                issues=issues,
            )
        if not any(
            "submission output" in issue for issue in issues
        ):
            checks.append(
                SubmissionReadinessCheck(
                    code="submission_directory_ready",
                    message="submission artifact directory can be created atomically",
                    path=output_dir,
                )
            )

    output_owners = {
        _resolved(output.path): task.task_id
        for task in selected_tasks
        for output in task.outputs
    }
    external_inputs: dict[Path, ResolvedInput] = {}
    generated_inputs = 0
    for task in selected_tasks:
        for item in task.inputs:
            path = _resolved(item.execution_path)
            producer = output_owners.get(path)
            if producer is not None:
                if producer not in task.depends_on:
                    issues.append(
                        f"task {task.task_id!r} consumes planned output {path} "
                        f"without depending on producer {producer!r}"
                    )
                else:
                    generated_inputs += 1
                continue
            external_inputs.setdefault(path, item)

    staging_sha256 = (
        plan.request.execution.staging.enabled
        and plan.request.execution.staging.verification.value == "sha256"
    )
    for item in external_inputs.values():
        _validate_external_input(
            item,
            staging_sha256=staging_sha256,
            issues=issues,
        )
    checks.append(
        SubmissionReadinessCheck(
            code="execution_inputs_ready",
            message=(
                f"validated {len(external_inputs)} external execution inputs; "
                f"{generated_inputs} inputs will be produced by upstream tasks"
            ),
        )
    )

    output_count = 0
    for task in selected_tasks:
        for output in task.outputs:
            output_count += 1
            path = _resolved(output.path)
            _require_writable_parent(
                path,
                label=f"task {task.task_id!r} output",
                issues=issues,
            )
            if (
                path.exists()
                and output.existing_file_handling
                == ExistingFileHandling.WRITE_NEW_FILE
                and output.existing_output_policy == ExistingOutputPolicy.ERROR
            ):
                issues.append(
                    f"task {task.task_id!r} output now exists under policy 'error': "
                    f"{path}"
                )
            if (
                output.existing_file_handling
                == ExistingFileHandling.UPDATE_ATOMICALLY
                and not path.is_file()
            ):
                issues.append(
                    f"task {task.task_id!r} atomic-update output is unavailable: {path}"
                )
    checks.append(
        SubmissionReadinessCheck(
            code="output_paths_ready",
            message=f"validated {output_count} task output paths and policies",
        )
    )

    intents = submission_reservation_intents(plan, task_ids=task_ids)
    if check_reservations and state.is_dir():
        store = ReservationStore(state)
        conflicts = [store.load(intent.output_path) for intent in intents]
        conflicts = [reservation for reservation in conflicts if reservation is not None]
        if allow_failed_reservations:
            intents_by_path = {
                _resolved(intent.output_path): intent for intent in intents
            }
            conflicts = [
                reservation
                for reservation in conflicts
                if not (
                    reservation.state.value == "failed"
                    and reservation.task_id
                    == intents_by_path[_resolved(reservation.output_path)].task_id
                    and reservation.config_digest == plan.config_digest
                    and reservation.manifest_family_id == plan.manifest_family_id
                )
            ]
        if conflicts:
            issues.extend(
                (
                    f"output {reservation.output_path} is already reserved by "
                    f"run={reservation.run_id}, task={reservation.task_id}, "
                    f"state={reservation.state.value}"
                )
                for reservation in conflicts
            )
        else:
            checks.append(
                SubmissionReadinessCheck(
                    code="reservations_available",
                    message=f"all {len(intents)} output reservations are available",
                    path=state,
                )
            )

    if issues:
        raise SubmissionReadinessError(tuple(issues))
    return tuple(checks)


def submission_reservation_intents(
    plan: ResolvedPlan,
    *,
    task_ids: tuple[str, ...] | None = None,
) -> tuple[SubmissionReservationIntent, ...]:
    selected = None if task_ids is None else set(task_ids)
    return tuple(
        SubmissionReservationIntent(
            reservation_id=ReservationStore.reservation_id(output.path),
            task_id=task.task_id,
            output_path=_resolved(output.path),
        )
        for task in plan.tasks
        if selected is None or task.task_id in selected
        for output in task.outputs
    )


def _command_preview(group: SlurmArrayGroup) -> str:
    arguments = ["sbatch", "--parsable"]
    if group.dependency_group_ids:
        placeholders = ":".join(
            f"<{group_id}-job-id>" for group_id in group.dependency_group_ids
        )
        arguments.append(f"--dependency=afterok:{placeholders}")
    arguments.append(str(group.script_path))
    return shlex.join(arguments)


def deterministic_submission_payload(
    record: SubmissionRecord | dict[str, Any],
) -> dict[str, Any]:
    if isinstance(record, SubmissionRecord):
        return record.model_dump(
            mode="json",
            exclude={"submission_id", "submission_digest"},
        )
    filtered = {
        key: value
        for key, value in record.items()
        if key not in {"submission_id", "submission_digest"}
    }
    return _JSON_OBJECT_ADAPTER.dump_python(filtered, mode="json")


def _expected_submission_id(
    *,
    run_id: str,
    attempt: int,
    submission_digest: str,
) -> str:
    return (
        f"{run_id}-attempt-{attempt}-"
        f"{submission_digest.split(':', 1)[1][:12]}"
    )


def prepare_submission(
    manifest_path: str | Path,
    *,
    state_root: str | Path,
    output_directory: str | Path,
    task_ids: tuple[str, ...] | None = None,
    attempt_number: int | None = None,
    allow_failed_reservations: bool = False,
) -> SubmissionRecord:
    """Create a fully audited, immutable submission preview without sbatch."""
    manifest = _resolved(manifest_path)
    state = _resolved(state_root)
    output_dir = _resolved(output_directory)
    plan = load_plan(manifest)
    readiness = validate_submission_readiness(
        plan,
        manifest_path=manifest,
        state_root=state,
        output_directory=output_dir,
        require_empty_output_directory=True,
        task_ids=task_ids,
        allow_failed_reservations=allow_failed_reservations,
    )
    rendered = render_slurm(
        plan,
        manifest_path=manifest,
        output_directory=output_dir,
        reservation_set_path=output_dir / RESERVATION_SET_NAME,
        task_ids=task_ids,
        attempt_number=attempt_number,
    )
    created_at = datetime.now(timezone.utc)
    attempt = plan.retry_number + 1 if attempt_number is None else attempt_number
    if attempt < 1:
        raise ValueError("submission attempt number must be positive")
    groups = tuple(
        SubmissionGroupRecord(
            group_id=group.group_id,
            task_ids=tuple(task.task_id for task in group.tasks),
            dependency_group_ids=group.dependency_group_ids,
            resource_profile=group.profile.name,
            script_path=group.script_path,
            script_sha256=file_sha256(group.script_path),
            index_path=group.index_path,
            index_sha256=file_sha256(group.index_path),
            sbatch_command_preview=_command_preview(group),
        )
        for group in rendered.groups
    )
    payload = {
        "artifact_type": "spires_batch_submission_record",
        "schema_version": plan.schema_version,
        "status": "prepared",
        "created_at": created_at,
        "attempt": attempt,
        "run_id": plan.run_id,
        "manifest_family_id": plan.manifest_family_id,
        "config_digest": plan.config_digest,
        "plan_digest": plan.plan_digest,
        "manifest_path": manifest,
        "manifest_sha256": file_sha256(manifest),
        "state_root": state,
        "output_directory": output_dir,
        "submit_script_path": rendered.submit_script,
        "submit_script_sha256": file_sha256(rendered.submit_script),
        "readiness_checks": readiness,
        "groups": groups,
        "reservation_intents": submission_reservation_intents(
            plan,
            task_ids=task_ids,
        ),
    }
    submission_digest = sha256_digest(deterministic_submission_payload(payload))
    record = SubmissionRecord(
        **payload,
        submission_digest=submission_digest,
        submission_id=_expected_submission_id(
            run_id=plan.run_id,
            attempt=attempt,
            submission_digest=submission_digest,
        ),
    )
    write_immutable_json(output_dir / SUBMISSION_RECORD_NAME, record)
    append_submission_event(
        output_dir / SUBMISSION_EVENTS_NAME,
        SubmissionEvent(
            timestamp=datetime.now(timezone.utc),
            event_type="submission_prepared",
            submission_id=record.submission_id,
            run_id=record.run_id,
            plan_digest=record.plan_digest,
            message="immutable submission preview prepared; no sbatch command executed",
            details={
                "groups": len(record.groups),
                "reservations": len(record.reservation_intents),
            },
        ),
    )
    return record


def load_submission_record(path: str | Path) -> SubmissionRecord:
    record = SubmissionRecord.model_validate(load_json_object(path))
    actual = sha256_digest(deterministic_submission_payload(record))
    if actual != record.submission_digest:
        raise ValueError(
            f"submission record digest mismatch for {path}: "
            f"stored {record.submission_digest}, calculated {actual}"
        )
    expected_id = _expected_submission_id(
        run_id=record.run_id,
        attempt=record.attempt,
        submission_digest=record.submission_digest,
    )
    if record.submission_id != expected_id:
        raise ValueError(
            f"submission record ID mismatch for {path}: "
            f"stored {record.submission_id}, expected {expected_id}"
        )
    return record


def _verify_submission_artifacts(record: SubmissionRecord) -> ResolvedPlan:
    if file_sha256(record.manifest_path) != record.manifest_sha256:
        raise ValueError("submission manifest changed after preview preparation")
    plan = load_plan(record.manifest_path)
    if (
        plan.run_id != record.run_id
        or plan.plan_digest != record.plan_digest
        or plan.config_digest != record.config_digest
    ):
        raise ValueError("submission record does not match its resolved manifest")
    for group in record.groups:
        if file_sha256(group.script_path) != group.script_sha256:
            raise ValueError(
                f"rendered Slurm script changed after preview: {group.script_path}"
            )
        if file_sha256(group.index_path) != group.index_sha256:
            raise ValueError(
                f"rendered task index changed after preview: {group.index_path}"
            )
    if file_sha256(record.submit_script_path) != record.submit_script_sha256:
        raise ValueError("rendered submission preview changed after preparation")
    return plan


def deterministic_reservation_set_payload(
    reservation_set: ReservationSet | dict[str, Any],
) -> dict[str, Any]:
    if isinstance(reservation_set, ReservationSet):
        return reservation_set.model_dump(
            mode="json",
            exclude={"reservation_set_digest"},
            exclude_none=True,
        )
    filtered = {
        key: value
        for key, value in reservation_set.items()
        if key != "reservation_set_digest"
    }
    serialized = _JSON_OBJECT_ADAPTER.dump_python(filtered, mode="json")
    return _without_none(serialized)


def _without_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_none(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [_without_none(item) for item in value]
    return value


def acquire_submission_reservations(
    submission_path: str | Path,
    *,
    reservation_set_path: str | Path | None = None,
    rearm_failed: bool = False,
) -> ReservationSet:
    """Recheck readiness and acquire every output reservation all-or-none."""
    record = load_submission_record(submission_path)
    plan = _verify_submission_artifacts(record)
    validate_submission_readiness(
        plan,
        manifest_path=record.manifest_path,
        state_root=record.state_root,
        check_reservations=True,
        task_ids=tuple(
            task_id for group in record.groups for task_id in group.task_ids
        ),
        allow_failed_reservations=rearm_failed,
    )
    destination = (
        record.output_directory / RESERVATION_SET_NAME
        if reservation_set_path is None
        else _resolved(reservation_set_path)
    )
    if destination.exists():
        raise FileExistsError(
            f"refusing to replace immutable reservation set {destination}"
        )
    store = ReservationStore(record.state_root)
    try:
        if rearm_failed:
            reservations = store.rearm_failed_many(
                record.reservation_intents,
                run_id=record.run_id,
                manifest_family_id=record.manifest_family_id,
                config_digest=record.config_digest,
                plan_digest=record.plan_digest,
                submission_id=record.submission_id,
            )
        else:
            reservations = store.acquire_many(
                record.reservation_intents,
                run_id=record.run_id,
                manifest_family_id=record.manifest_family_id,
                config_digest=record.config_digest,
                plan_digest=record.plan_digest,
                submission_id=record.submission_id,
            )
    except ReservationBatchError as exc:
        append_submission_event(
            record.output_directory / SUBMISSION_EVENTS_NAME,
            SubmissionEvent(
                timestamp=datetime.now(timezone.utc),
                event_type="reservation_acquisition_failed",
                submission_id=record.submission_id,
                run_id=record.run_id,
                plan_digest=record.plan_digest,
                message=str(exc),
                details={"rolled_back": len(exc.rolled_back)},
            ),
        )
        raise

    acquired_at = datetime.now(timezone.utc)
    payload = {
        "artifact_type": "spires_batch_reservation_set",
        "schema_version": record.schema_version,
        "submission_id": record.submission_id,
        "run_id": record.run_id,
        "plan_digest": record.plan_digest,
        "submission_record_path": _resolved(submission_path),
        "state_root": record.state_root,
        "acquired_at": acquired_at,
        "reservations": reservations,
    }
    digest = sha256_digest(deterministic_reservation_set_payload(payload))
    reservation_set = ReservationSet(
        **payload,
        reservation_set_digest=digest,
    )
    try:
        write_immutable_json(destination, reservation_set)
    except Exception:
        if rearm_failed:
            store.fail_rearmed(
                reservations,
                reason=(
                    "retry reservation-set artifact could not be written; "
                    "reservation remains protected in failed state"
                ),
            )
        else:
            store.rollback_acquired(
                reservations,
                reason=(
                    "reservation-set artifact could not be written before submission"
                ),
            )
        raise
    append_submission_event(
        record.output_directory / SUBMISSION_EVENTS_NAME,
        SubmissionEvent(
            timestamp=datetime.now(timezone.utc),
            event_type="reservations_acquired",
            submission_id=record.submission_id,
            run_id=record.run_id,
            plan_digest=record.plan_digest,
            message=(
                "failed output reservations re-armed for retry; no sbatch "
                "command executed"
                if rearm_failed
                else "all output reservations acquired; no sbatch command executed"
            ),
            details={
                "reservation_set": str(destination),
                "reservations": len(reservations),
                "rearmed_failed": rearm_failed,
            },
        ),
    )
    return reservation_set


def load_reservation_set(path: str | Path) -> ReservationSet:
    reservation_set = ReservationSet.model_validate(load_json_object(path))
    actual = sha256_digest(
        deterministic_reservation_set_payload(reservation_set)
    )
    if actual != reservation_set.reservation_set_digest:
        # Early E2 verification records included optional ``None`` reservation
        # fields in the digest before ``submission_record_path`` joined the
        # canonical payload. Validate that exact historical representation so
        # retained audit evidence remains readable without weakening checks on
        # newly written records.
        legacy_payload = reservation_set.model_dump(
            mode="json",
            exclude={
                "reservation_set_digest": True,
                "submission_record_path": True,
                "reservations": {
                    "__all__": {
                        "slurm_cluster",
                        "slurm_array_task_id",
                        "submission_group_id",
                    }
                },
            },
        )
        legacy = sha256_digest(legacy_payload)
        if legacy != reservation_set.reservation_set_digest:
            raise ValueError(
                f"reservation set digest mismatch for {path}: "
                f"stored {reservation_set.reservation_set_digest}, calculated {actual}"
            )
    return reservation_set


def rollback_submission_reservations(
    reservation_set_path: str | Path,
    *,
    reason: str,
) -> tuple:
    """Audit and remove a reservation set before any scheduler submission."""
    reservation_set = load_reservation_set(reservation_set_path)
    submission_record_path = (
        Path(reservation_set_path).resolve().parent / SUBMISSION_RECORD_NAME
        if reservation_set.submission_record_path is None
        else reservation_set.submission_record_path
    )
    record = load_submission_record(submission_record_path)
    if record.submission_id != reservation_set.submission_id:
        raise ValueError("reservation set does not belong to sibling submission record")
    store = ReservationStore(reservation_set.state_root)
    rolled_back = store.rollback_acquired(
        reservation_set.reservations,
        reason=reason,
    )
    append_submission_event(
        record.output_directory / SUBMISSION_EVENTS_NAME,
        SubmissionEvent(
            timestamp=datetime.now(timezone.utc),
            event_type="reservations_rolled_back",
            submission_id=record.submission_id,
            run_id=record.run_id,
            plan_digest=record.plan_digest,
            message=reason.strip(),
            details={"reservations": len(rolled_back)},
        ),
    )
    return rolled_back


def append_submission_event(path: str | Path, event: SubmissionEvent) -> None:
    """Append one durable submission lifecycle event and fsync it."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = event.model_dump(mode="json", exclude_none=True)
    descriptor = os.open(
        destination,
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        0o664,
    )
    try:
        os.write(
            descriptor,
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8"),
        )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
