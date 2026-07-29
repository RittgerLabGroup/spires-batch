"""Persistent duplicate-output reservations with auditable cleanup."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from spires_batch.models import Reservation, ReservationState
from spires_batch.serialization import write_immutable_json


class ReservationConflict(RuntimeError):
    def __init__(self, reservation: Reservation):
        self.reservation = reservation
        super().__init__(
            f"output {reservation.output_path} is reserved by run "
            f"{reservation.run_id}, task {reservation.task_id}, "
            f"state={reservation.state.value}, job={reservation.slurm_job_id or 'unsubmitted'}"
        )


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
