"""Persistent duplicate-output reservations with auditable cleanup."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from spires_batch.models import (
    Reservation,
    ReservationSet,
    ReservationState,
    ResolvedPlan,
    SubmissionReservationIntent,
    Task,
    TaskAttempt,
    TaskStatus,
)
from spires_batch.serialization import write_immutable_json


class ReservationConflict(RuntimeError):
    def __init__(self, reservation: Reservation):
        self.reservation = reservation
        super().__init__(
            f"output {reservation.output_path} is reserved by run "
            f"{reservation.run_id}, task {reservation.task_id}, "
            f"state={reservation.state.value}, job={reservation.slurm_job_id or 'unsubmitted'}"
        )


class ReservationBatchError(RuntimeError):
    """Reservation acquisition failed after rolling back this batch's writes."""

    def __init__(
        self,
        message: str,
        *,
        rolled_back: tuple[Reservation, ...] = (),
    ):
        self.rolled_back = rolled_back
        super().__init__(message)


class WorkerReservationError(RuntimeError):
    """A task worker cannot prove ownership of its planned outputs."""


class ReservationStore:
    """Filesystem-backed reservations scoped to one configured state root."""

    def __init__(self, state_root: str | Path):
        self.state_root = Path(state_root).expanduser().resolve(strict=False)
        self.directory = self.state_root / ".spires-batch" / "reservations"
        self.audit_path = self.state_root / ".spires-batch" / "reservation-audit.jsonl"

    @staticmethod
    def reservation_id(output_path: str | Path) -> str:
        normalized = str(Path(output_path).expanduser().resolve(strict=False))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def path_for_output(self, output_path: str | Path) -> Path:
        return self.directory / f"{self.reservation_id(output_path)}.json"

    def load(self, output_path: str | Path) -> Reservation | None:
        path = self.path_for_output(output_path)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as stream:
            return Reservation.model_validate(json.load(stream))

    def acquire(
        self,
        *,
        run_id: str,
        task_id: str,
        config_digest: str,
        manifest_family_id: str | None = None,
        plan_digest: str | None = None,
        submission_id: str | None = None,
        output_path: str | Path,
        slurm_job_id: str | None = None,
        user: str | None = None,
    ) -> Reservation:
        existing = self.load(output_path)
        if existing is not None:
            raise ReservationConflict(existing)
        now = datetime.now(timezone.utc)
        reservation = Reservation(
            reservation_id=self.reservation_id(output_path),
            state=ReservationState.ACTIVE,
            run_id=run_id,
            task_id=task_id,
            user=user or getpass.getuser(),
            created_at=now,
            updated_at=now,
            config_digest=config_digest,
            manifest_family_id=manifest_family_id,
            plan_digest=plan_digest,
            submission_id=submission_id,
            output_path=Path(output_path).expanduser().resolve(strict=False),
            slurm_job_id=slurm_job_id,
        )
        try:
            write_immutable_json(self.path_for_output(output_path), reservation)
        except FileExistsError:
            conflict = self.load(output_path)
            if conflict is None:
                raise
            raise ReservationConflict(conflict)
        self._audit("acquired", reservation)
        return reservation

    def acquire_many(
        self,
        intents: Iterable[SubmissionReservationIntent],
        *,
        run_id: str,
        manifest_family_id: str | None = None,
        config_digest: str,
        plan_digest: str,
        submission_id: str,
        user: str | None = None,
    ) -> tuple[Reservation, ...]:
        """Acquire one submission's reservations with audited rollback on failure."""
        ordered = tuple(
            sorted(
                intents,
                key=lambda intent: (str(intent.output_path), intent.task_id),
            )
        )
        if not ordered:
            raise ValueError("reservation acquisition requires at least one output")
        acquired: list[Reservation] = []
        try:
            for intent in ordered:
                acquired.append(
                    self.acquire(
                        run_id=run_id,
                        task_id=intent.task_id,
                        config_digest=config_digest,
                        manifest_family_id=manifest_family_id,
                        plan_digest=plan_digest,
                        submission_id=submission_id,
                        output_path=intent.output_path,
                        user=user,
                    )
                )
        except Exception as exc:
            rolled_back = self.rollback_acquired(
                acquired,
                reason=(
                    "all-or-none acquisition rollback after reservation failure: "
                    f"{exc}"
                ),
            )
            raise ReservationBatchError(
                f"reservation batch acquisition failed: {exc}",
                rolled_back=rolled_back,
            ) from exc
        return tuple(acquired)

    def rearm_failed_many(
        self,
        intents: Iterable[SubmissionReservationIntent],
        *,
        run_id: str,
        manifest_family_id: str,
        config_digest: str,
        plan_digest: str,
        submission_id: str,
        user: str | None = None,
    ) -> tuple[Reservation, ...]:
        """Atomically re-arm one failed same-family reservation set for retry."""
        ordered = tuple(
            sorted(
                intents,
                key=lambda intent: (str(intent.output_path), intent.task_id),
            )
        )
        if not ordered:
            raise ValueError("reservation retry re-arm requires at least one output")

        current_reservations: list[Reservation] = []
        for intent in ordered:
            current = self.load(intent.output_path)
            if current is None:
                raise RuntimeError(
                    f"cannot re-arm missing failed reservation {intent.output_path}"
                )
            if (
                current.reservation_id != intent.reservation_id
                or current.task_id != intent.task_id
                or current.output_path
                != Path(intent.output_path).expanduser().resolve(strict=False)
                or current.manifest_family_id != manifest_family_id
                or current.config_digest != config_digest
            ):
                raise ReservationConflict(current)
            if current.state != ReservationState.FAILED:
                raise RuntimeError(
                    f"cannot re-arm reservation {current.reservation_id} from "
                    f"state {current.state.value!r}"
                )
            current_reservations.append(current)

        now = datetime.now(timezone.utc)
        retry_user = user or getpass.getuser()
        rearmed = tuple(
            current.model_copy(
                update={
                    "state": ReservationState.ACTIVE,
                    "run_id": run_id,
                    "user": retry_user,
                    "updated_at": now,
                    "plan_digest": plan_digest,
                    "submission_id": submission_id,
                    "slurm_cluster": None,
                    "slurm_job_id": None,
                    "slurm_array_task_id": None,
                    "submission_group_id": None,
                    "message": "failed reservation re-armed for eligible retry",
                }
            )
            for current in current_reservations
        )
        replaced: list[tuple[Reservation, Reservation]] = []
        try:
            for previous, retry in zip(current_reservations, rearmed, strict=True):
                self._replace(self.path_for_output(retry.output_path), retry)
                replaced.append((previous, retry))
                self._audit("rearmed_for_retry", retry)
        except Exception:
            for previous, retry in reversed(replaced):
                self._replace(self.path_for_output(retry.output_path), previous)
                self._audit("retry_rearm_rolled_back", previous)
            raise
        return rearmed

    def fail_rearmed(
        self,
        reservations: Iterable[Reservation],
        *,
        reason: str,
    ) -> tuple[Reservation, ...]:
        """Return an unsubmitted retry set to protected failed state."""
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("retry re-arm failure requires an audit reason")
        current_reservations: list[Reservation] = []
        for expected in reservations:
            current = self.load(expected.output_path)
            if current is None:
                raise RuntimeError(
                    f"cannot fail missing re-armed reservation {expected.output_path}"
                )
            if (
                current.reservation_id != expected.reservation_id
                or current.run_id != expected.run_id
                or current.task_id != expected.task_id
                or current.submission_id != expected.submission_id
            ):
                raise ReservationConflict(current)
            if current.state != ReservationState.ACTIVE:
                raise RuntimeError(
                    f"cannot fail re-armed reservation {current.reservation_id} "
                    f"from state {current.state.value!r}"
                )
            if current.slurm_job_id is not None:
                raise RuntimeError(
                    f"cannot fail submitted retry reservation "
                    f"{current.reservation_id}"
                )
            current_reservations.append(current)

        failed: list[Reservation] = []
        for current in current_reservations:
            restored = current.model_copy(
                update={
                    "state": ReservationState.FAILED,
                    "updated_at": datetime.now(timezone.utc),
                    "message": normalized_reason,
                }
            )
            self._replace(self.path_for_output(current.output_path), restored)
            self._audit("retry_rearm_failed", restored)
            failed.append(restored)
        return tuple(failed)

    def rollback_acquired(
        self,
        reservations: Iterable[Reservation],
        *,
        reason: str,
    ) -> tuple[Reservation, ...]:
        """Remove only active, unsubmitted reservations owned by this operation."""
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("reservation rollback requires an audit reason")
        validated: list[Reservation] = []
        for expected in reversed(tuple(reservations)):
            current = self.load(expected.output_path)
            if current is None:
                raise RuntimeError(
                    "cannot roll back missing reservation "
                    f"{expected.reservation_id}"
                )
            if current.reservation_id != expected.reservation_id:
                raise ReservationConflict(current)
            if (
                current.run_id != expected.run_id
                or current.task_id != expected.task_id
                or current.submission_id != expected.submission_id
            ):
                raise ReservationConflict(current)
            if current.state != ReservationState.ACTIVE:
                raise RuntimeError(
                    f"cannot roll back reservation {current.reservation_id} "
                    f"in state {current.state.value!r}"
                )
            if current.slurm_job_id is not None:
                raise RuntimeError(
                    f"cannot roll back submitted reservation "
                    f"{current.reservation_id}; job={current.slurm_job_id}"
                )
            validated.append(current)

        rolled_back: list[Reservation] = []
        for current in validated:
            released = current.model_copy(
                update={
                    "updated_at": datetime.now(timezone.utc),
                    "message": normalized_reason,
                }
            )
            self._audit("rolled_back_before_submission", released)
            self.path_for_output(current.output_path).unlink()
            rolled_back.append(released)
        return tuple(reversed(rolled_back))

    def attach_scheduler_job(
        self,
        output_path: str | Path,
        *,
        run_id: str,
        task_id: str,
        submission_id: str,
        cluster: str,
        job_id: str,
        array_task_id: str,
        group_id: str,
    ) -> Reservation:
        """Durably attach one submitted Slurm array element to its reservation."""
        if not job_id.isdigit():
            raise ValueError(f"invalid Slurm job ID {job_id!r}")
        if not array_task_id.isdigit():
            raise ValueError(f"invalid Slurm array task ID {array_task_id!r}")
        reservation = self.verify_owner(
            output_path,
            run_id=run_id,
            task_id=task_id,
        )
        if reservation.submission_id != submission_id:
            raise ReservationConflict(reservation)
        scheduler_identity = (
            reservation.slurm_cluster,
            reservation.slurm_job_id,
            reservation.slurm_array_task_id,
            reservation.submission_group_id,
        )
        expected_identity = (cluster, job_id, array_task_id, group_id)
        if reservation.slurm_job_id is not None:
            if scheduler_identity == expected_identity:
                return reservation
            raise RuntimeError(
                f"reservation {reservation.reservation_id} is already attached "
                f"to scheduler identity {scheduler_identity}"
            )
        attached = reservation.model_copy(
            update={
                "updated_at": datetime.now(timezone.utc),
                "slurm_cluster": cluster,
                "slurm_job_id": job_id,
                "slurm_array_task_id": array_task_id,
                "submission_group_id": group_id,
                "message": "Slurm job identity durably attached",
            }
        )
        self._replace(self.path_for_output(output_path), attached)
        self._audit("scheduler_job_attached", attached)
        return attached

    def verify_owner(
        self,
        output_path: str | Path,
        *,
        run_id: str,
        task_id: str,
    ) -> Reservation:
        reservation = self.load(output_path)
        if reservation is None:
            raise RuntimeError(f"no reservation exists for output {output_path}")
        if reservation.run_id != run_id or reservation.task_id != task_id:
            raise ReservationConflict(reservation)
        if reservation.state != ReservationState.ACTIVE:
            raise RuntimeError(
                f"reservation {reservation.reservation_id} is {reservation.state.value}, "
                "not active"
            )
        return reservation

    def complete(
        self,
        output_path: str | Path,
        *,
        run_id: str,
        task_id: str,
        message: str = "output reopened and validated",
    ) -> Reservation:
        reservation = self.verify_owner(
            output_path,
            run_id=run_id,
            task_id=task_id,
        )
        completed = reservation.model_copy(
            update={
                "state": ReservationState.COMPLETED,
                "updated_at": datetime.now(timezone.utc),
                "message": message,
            }
        )
        path = self.path_for_output(output_path)
        self._replace(path, completed)
        self._audit("completed", completed)
        path.unlink()
        return completed

    def mark_failed(
        self,
        output_path: str | Path,
        *,
        run_id: str,
        task_id: str,
        message: str,
    ) -> Reservation:
        reservation = self.verify_owner(
            output_path,
            run_id=run_id,
            task_id=task_id,
        )
        failed = reservation.model_copy(
            update={
                "state": ReservationState.FAILED,
                "updated_at": datetime.now(timezone.utc),
                "message": message,
            }
        )
        self._replace(self.path_for_output(output_path), failed)
        self._audit("failed", failed)
        return failed

    def reconcile_scheduler_terminal(
        self,
        reservation_set: ReservationSet,
        task: Task,
        attempt: TaskAttempt,
        *,
        group_id: str,
        cluster: str,
        job_id: str,
        array_task_id: str,
    ) -> tuple[Reservation, ...]:
        """Complete worker terminalization when the scheduler outlives a worker."""
        if attempt.task_id != task.task_id:
            raise WorkerReservationError(
                f"scheduler attempt belongs to {attempt.task_id!r}, "
                f"expected {task.task_id!r}"
            )
        if attempt.status not in {
            TaskStatus.SUCCEEDED,
            TaskStatus.LOADED_EXISTING,
            TaskStatus.FAILED,
        }:
            raise WorkerReservationError(
                f"cannot reconcile scheduler status {attempt.status.value!r}"
            )
        expected = tuple(
            reservation
            for reservation in reservation_set.reservations
            if reservation.task_id == task.task_id
        )
        if not expected:
            raise WorkerReservationError(
                f"reservation set has no outputs for task {task.task_id!r}"
            )

        successful = attempt.status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.LOADED_EXISTING,
        }
        terminalized: list[Reservation] = []
        for recorded in expected:
            current = self.load(recorded.output_path)
            if current is None:
                if successful:
                    terminalized.append(
                        recorded.model_copy(
                            update={
                                "state": ReservationState.COMPLETED,
                                "message": "worker already removed completed reservation",
                            }
                        )
                    )
                    continue
                raise WorkerReservationError(
                    f"failed task reservation disappeared: {recorded.output_path}"
                )
            immutable_identity = (
                current.reservation_id,
                current.run_id,
                current.task_id,
                current.config_digest,
                current.manifest_family_id,
                current.plan_digest,
                current.submission_id,
                current.output_path,
            )
            recorded_identity = (
                recorded.reservation_id,
                recorded.run_id,
                recorded.task_id,
                recorded.config_digest,
                recorded.manifest_family_id,
                recorded.plan_digest,
                recorded.submission_id,
                recorded.output_path,
            )
            if immutable_identity != recorded_identity:
                raise ReservationConflict(current)
            scheduler_identity = (
                current.slurm_cluster,
                current.slurm_job_id,
                current.slurm_array_task_id,
                current.submission_group_id,
            )
            if scheduler_identity != (
                cluster,
                job_id,
                array_task_id,
                group_id,
            ):
                raise WorkerReservationError(
                    "reservation scheduler identity does not match terminal "
                    f"accounting for {recorded.output_path}"
                )

            if successful:
                if current.state != ReservationState.ACTIVE:
                    raise WorkerReservationError(
                        f"successful task reservation is {current.state.value!r}"
                    )
                completed = current.model_copy(
                    update={
                        "state": ReservationState.COMPLETED,
                        "updated_at": datetime.now(timezone.utc),
                        "message": (
                            "controller completed reservation from durable "
                            f"task status {attempt.status.value}"
                        ),
                    }
                )
                path = self.path_for_output(current.output_path)
                self._replace(path, completed)
                self._audit("controller_completed", completed)
                path.unlink()
                terminalized.append(completed)
                continue

            if current.state == ReservationState.FAILED:
                terminalized.append(current)
                continue
            if current.state != ReservationState.ACTIVE:
                raise WorkerReservationError(
                    f"failed task reservation is {current.state.value!r}"
                )
            failed = current.model_copy(
                update={
                    "state": ReservationState.FAILED,
                    "updated_at": datetime.now(timezone.utc),
                    "message": (
                        "controller terminalized scheduler failure "
                        f"[{attempt.failure_class.value if attempt.failure_class else 'unknown'}/"
                        f"{attempt.failure_code or 'unknown'}]: "
                        f"{attempt.message or 'no task message'}"
                    ),
                }
            )
            self._replace(self.path_for_output(current.output_path), failed)
            self._audit("controller_failed", failed)
            terminalized.append(failed)
        return tuple(terminalized)

    def list(self) -> tuple[Reservation, ...]:
        if not self.directory.exists():
            return ()
        reservations: list[Reservation] = []
        for path in sorted(self.directory.glob("*.json")):
            with path.open("r", encoding="utf-8") as stream:
                reservations.append(Reservation.model_validate(json.load(stream)))
        return tuple(reservations)

    def prune_completed(
        self,
        *,
        older_than: timedelta = timedelta(days=7),
        apply: bool = False,
        output_validator: Callable[[Path], bool] | None = None,
    ) -> tuple[Reservation, ...]:
        """Remove completed leftovers only after validating their output."""
        validator = output_validator or (
            lambda path: path.is_file() and path.stat().st_size > 0
        )
        cutoff = datetime.now(timezone.utc) - older_than
        candidates = tuple(
            reservation
            for reservation in self.list()
            if reservation.state == ReservationState.COMPLETED
            and reservation.updated_at <= cutoff
            and validator(reservation.output_path)
        )
        if apply:
            for reservation in candidates:
                self._audit("pruned_completed", reservation)
                self.path_for_output(reservation.output_path).unlink(missing_ok=True)
        return candidates

    def release_stale(
        self,
        output_path: str | Path,
        *,
        expected_run_id: str,
        expected_task_id: str,
        reason: str,
        apply: bool = False,
    ) -> Reservation:
        if not reason.strip():
            raise ValueError("stale reservation release requires an audit reason")
        reservation = self.load(output_path)
        if reservation is None:
            raise RuntimeError(f"no reservation exists for output {output_path}")
        if (
            reservation.run_id != expected_run_id
            or reservation.task_id != expected_task_id
        ):
            raise ReservationConflict(reservation)
        released = reservation.model_copy(
            update={
                "updated_at": datetime.now(timezone.utc),
                "message": reason.strip(),
            }
        )
        if apply:
            self._audit("released_stale", released)
            self.path_for_output(output_path).unlink()
        return released

    def _replace(self, destination: Path, value: Reservation) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(
                    value.model_dump(mode="json", exclude_none=True),
                    stream,
                    indent=2,
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _audit(self, action: str, reservation: Reservation) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "reservation": reservation.model_dump(mode="json", exclude_none=True),
        }
        descriptor = os.open(
            self.audit_path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o664,
        )
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


@dataclass(frozen=True)
class WorkerReservationGuard:
    """Bind one worker task to its immutable and live reservation identities."""

    reservation_set: ReservationSet
    plan: ResolvedPlan
    task: Task
    group_id: str | None
    cluster: str | None
    job_id: str | None
    array_task_id: str | None
    attachment_timeout_seconds: float = 30.0

    def _expected_reservations(self) -> tuple[Reservation, ...]:
        if (
            self.reservation_set.run_id != self.plan.run_id
            or self.reservation_set.plan_digest != self.plan.plan_digest
        ):
            raise WorkerReservationError(
                "reservation set does not belong to the supplied resolved plan"
            )
        known_task = next(
            (item for item in self.plan.tasks if item.task_id == self.task.task_id),
            None,
        )
        if known_task is None or known_task != self.task:
            raise WorkerReservationError(
                "worker task is not an exact member of the resolved plan"
            )
        if not self.group_id:
            raise WorkerReservationError("task index has no submission group identity")
        if not self.cluster:
            raise WorkerReservationError("task index has no Slurm cluster identity")
        if self.job_id is None or not self.job_id.isdigit():
            raise WorkerReservationError(
                f"worker has invalid Slurm job identity {self.job_id!r}"
            )
        if self.array_task_id is None or not self.array_task_id.isdigit():
            raise WorkerReservationError(
                "worker has invalid Slurm array identity "
                f"{self.array_task_id!r}"
            )

        expected = tuple(
            reservation
            for reservation in self.reservation_set.reservations
            if reservation.task_id == self.task.task_id
        )
        expected_paths = {reservation.output_path for reservation in expected}
        task_paths = {output.path for output in self.task.outputs}
        if not expected or expected_paths != task_paths:
            raise WorkerReservationError(
                "immutable reservation set does not exactly cover the worker outputs"
            )
        for reservation in expected:
            if (
                reservation.reservation_id
                != ReservationStore.reservation_id(reservation.output_path)
                or reservation.run_id != self.plan.run_id
                or reservation.task_id != self.task.task_id
                or reservation.config_digest != self.plan.config_digest
                or (
                    reservation.manifest_family_id is not None
                    and reservation.manifest_family_id
                    != self.plan.manifest_family_id
                )
                or reservation.plan_digest != self.plan.plan_digest
                or reservation.submission_id != self.reservation_set.submission_id
                or reservation.state != ReservationState.ACTIVE
            ):
                raise WorkerReservationError(
                    "immutable reservation identity does not match the worker task: "
                    f"{reservation.output_path}"
                )
        return tuple(sorted(expected, key=lambda item: str(item.output_path)))

    def _verify_once(
        self,
        expected: tuple[Reservation, ...],
    ) -> tuple[Reservation, ...]:
        store = ReservationStore(self.reservation_set.state_root)
        current_reservations: list[Reservation] = []
        for recorded in expected:
            current = store.load(recorded.output_path)
            if current is None:
                raise WorkerReservationError(
                    f"required worker reservation is missing: {recorded.output_path}"
                )
            immutable_identity = (
                current.reservation_id,
                current.run_id,
                current.task_id,
                current.config_digest,
                current.manifest_family_id,
                current.plan_digest,
                current.submission_id,
                current.output_path,
            )
            expected_identity = (
                recorded.reservation_id,
                recorded.run_id,
                recorded.task_id,
                recorded.config_digest,
                recorded.manifest_family_id,
                recorded.plan_digest,
                recorded.submission_id,
                recorded.output_path,
            )
            if immutable_identity != expected_identity:
                raise WorkerReservationError(
                    "live reservation identity changed after acquisition: "
                    f"{recorded.output_path}"
                )
            if current.state != ReservationState.ACTIVE:
                raise WorkerReservationError(
                    f"reservation {current.reservation_id} is "
                    f"{current.state.value!r}, not active"
                )

            scheduler_identity = (
                current.slurm_cluster,
                current.slurm_job_id,
                current.slurm_array_task_id,
                current.submission_group_id,
            )
            expected_scheduler_identity = (
                self.cluster,
                self.job_id,
                self.array_task_id,
                self.group_id,
            )
            if scheduler_identity != expected_scheduler_identity:
                if all(value is None for value in scheduler_identity):
                    raise _SchedulerAttachmentPending(recorded.output_path)
                raise WorkerReservationError(
                    "live reservation belongs to a different Slurm worker: "
                    f"{recorded.output_path}; recorded={scheduler_identity}, "
                    f"worker={expected_scheduler_identity}"
                )
            current_reservations.append(current)
        return tuple(current_reservations)

    def verify(self, task: Task | None = None) -> tuple[Reservation, ...]:
        """Verify ownership, briefly waiting for submit-side job attachment."""
        if task is not None and task != self.task:
            raise WorkerReservationError(
                f"reservation guard is scoped to {self.task.task_id!r}, "
                f"not {task.task_id!r}"
            )
        expected = self._expected_reservations()
        deadline = time.monotonic() + self.attachment_timeout_seconds
        while True:
            try:
                return self._verify_once(expected)
            except _SchedulerAttachmentPending as exc:
                if time.monotonic() >= deadline:
                    raise WorkerReservationError(
                        "Slurm job identity was not attached to reservation before "
                        f"worker timeout: {exc.output_path}"
                    ) from exc
                time.sleep(0.25)

    def terminalize(self, attempt: TaskAttempt) -> tuple[Reservation, ...]:
        """Transition owned reservations from a durably recorded task outcome."""
        if attempt.task_id != self.task.task_id:
            raise WorkerReservationError(
                f"attempt belongs to {attempt.task_id!r}, expected {self.task.task_id!r}"
            )
        if (
            attempt.slurm_job_id != self.job_id
            or attempt.slurm_array_task_id != self.array_task_id
        ):
            raise WorkerReservationError(
                "terminal task attempt does not carry the verified Slurm worker "
                "identity"
            )
        if attempt.status not in {
            TaskStatus.SUCCEEDED,
            TaskStatus.LOADED_EXISTING,
            TaskStatus.FAILED,
        }:
            raise WorkerReservationError(
                f"cannot terminalize reservations from status {attempt.status.value!r}"
            )

        current_reservations = self.verify()
        store = ReservationStore(self.reservation_set.state_root)
        terminalized: list[Reservation] = []
        if attempt.status in {TaskStatus.SUCCEEDED, TaskStatus.LOADED_EXISTING}:
            message = (
                f"task attempt {attempt.attempt} completed after validated status "
                f"{attempt.status.value}: {attempt.message or 'no task message'}"
            )
            for current in current_reservations:
                completed = current.model_copy(
                    update={
                        "state": ReservationState.COMPLETED,
                        "updated_at": datetime.now(timezone.utc),
                        "message": message,
                    }
                )
                path = store.path_for_output(current.output_path)
                store._replace(path, completed)
                store._audit("completed", completed)
                path.unlink()
                terminalized.append(completed)
            return tuple(terminalized)

        message = (
            f"task attempt {attempt.attempt} failed "
            f"[{attempt.failure_class.value if attempt.failure_class else 'unknown'}/"
            f"{attempt.failure_code or 'unknown'}]: "
            f"{attempt.message or 'no task message'}"
        )
        for current in current_reservations:
            failed = current.model_copy(
                update={
                    "state": ReservationState.FAILED,
                    "updated_at": datetime.now(timezone.utc),
                    "message": message,
                }
            )
            store._replace(store.path_for_output(current.output_path), failed)
            store._audit("failed", failed)
            terminalized.append(failed)
        return tuple(terminalized)


class _SchedulerAttachmentPending(RuntimeError):
    def __init__(self, output_path: Path):
        self.output_path = output_path
        super().__init__(str(output_path))
