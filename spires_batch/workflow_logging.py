"""Readable workflow reports and isolated scientific task logs."""

from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import csv
import io
import json
import os
from pathlib import Path
from uuid import uuid4

from .nrt_storage import atomic_json


def log_directory(record):
    return Path(record["log_root"]) / record["identity"]


def _text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex)
    temporary.write_text(content)
    temporary.replace(path)


def reports(state):
    root = Path(state["log_directory"])
    jobs = state["jobs"]
    lines = ["SPIReS workflow summary", f"Status: {state['status']}",
             f"Updated: {datetime.now(timezone.utc).isoformat()}",
             f"Workflow: {state['workflow']}", f"Submission ledger: {state['control_directory']}",
             f"Run directory: {state.get('output', '')}", f"Selection: {json.dumps(state.get('selection', {}))}",
             "Counts below are processing tasks, not pixels. Allowed missing observations are not successful retrievals.", ""]
    tiles = {tile: [] for job in jobs.values() for tile in job.get("tiles", [])}
    rows = []
    for name, job in jobs.items():
        attempts = job.get("attempts", [])
        retries = sum(entry.get("attempt", 1) > 1 for entry in attempts)
        retry_description = "retry details in managed batch reports" if job.get("operation_directory") else f"{retries} retried task attempts"
        lines.append(f"{name}: {job['status']}; {retry_description}")
        if "max_auto_retry_count" in job:
            lines.append(f"  Retry limit: {job['max_auto_retry_count']} after the initial attempt")
        latest = {(entry["stage"], entry["index"]): entry for entry in attempts}
        if "planned_tasks" in job:
            lines.append(f"  Verified tasks: {sum(entry.get('verified', False) for entry in latest.values())}/{job['planned_tasks']}")
        if job.get("error"):
            lines.append("  Stop reason: " + job["error"])
        if job.get("operation_directory"):
            lines.append("  Managed batch reports: " + str(Path(job["operation_directory"]) / "summaries"))
        lines.append("  Logs: " + str(root / name))
        for entry in attempts:
            rows.append(dict(job=name, **entry))
            for tile in entry.get("tiles", []):
                tiles.setdefault(tile, []).append(dict(job=name, **entry))
    if state.get("error"):
        lines.extend(["", "Workflow stop reason: " + state["error"]])
    for tile, entries in sorted(tiles.items()):
        latest = {(entry["job"], entry["stage"], entry["index"]): entry for entry in entries}
        verified = sum(entry.get("verified", False) for entry in latest.values())
        gaps = sorted({entry["date"] for entry in latest.values() if tile in entry.get("missing_tiles", []) and entry.get("verified")})
        tile_lines = [f"Tile {tile}", f"Workflow status: {state['status']}",
                      f"Verified tasks: {verified}/{len(latest)} observed tasks",
                      f"Allowed missing observation dates: {len(gaps)}", *["  " + day for day in gaps], "",
                      "Task attempts (shared date/domain tasks appear for every affected tile):"]
        for name, job in jobs.items():
            if tile in job.get("tiles", []):
                tile_lines.append(f"{name}: {job['status']}" + ("; " + job["error"] if job.get("error") else ""))
                if job.get("operation_directory"):
                    tile_lines.append("  Managed batch tile report: " + str(Path(job["operation_directory"]) / "summaries/tiles" / tile / "tile-summary.txt"))
        for entry in entries:
            tile_lines.append(f"{entry['job']}/{entry['stage']} {entry.get('date', '')} index={entry['index']} attempt={entry['attempt']}: {entry['state']} exit={entry['exit_code']} verified={entry.get('verified', False)} Slurm={entry['slurm_id']}")
            tile_lines.append("  Slurm logs: " + ", ".join(entry.get("logs", [str(root / entry["job"])])))
            tile_lines.append("  Scientific logs: " + str(root / entry["job"] / tile))
        _text(root / "tiles" / tile / "summary.txt", "\n".join(tile_lines) + "\n")
        atomic_json(root / "tiles" / tile / "summary.json", dict(tile=tile, status=state["status"], attempts=entries, missing_dates=gaps))
        lines.append(f"{tile}: {verified}/{len(latest)} observed tasks verified; {len(gaps)} allowed gaps; tiles/{tile}/summary.txt")
    _text(root / "summary.txt", "\n".join(lines) + "\n")
    atomic_json(root / "summary.json", state)
    columns = ["job", "stage", "index", "date", "attempt", "slurm_id", "state", "exit_code", "verified", "tiles", "missing_tiles", "logs"]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    _text(root / "attempts.csv", stream.getvalue())


@contextmanager
def task_log(directory, stage, *, tile=None, day=None):
    root = Path(os.environ.get("SPIRES_JOB_LOG_ROOT", str(Path(directory) / "logs")))
    folder = root / (tile or "shared") / (day or "undated")
    folder.mkdir(parents=True, exist_ok=True)
    slurm_id = os.environ.get("SLURM_JOB_ID", "local")
    path = folder / f"{stage}-{slurm_id}-{uuid4().hex}.log"
    metadata = dict(stage=stage, tile=tile, date=day, slurm_job_id=slurm_id,
                    slurm_array_job_id=os.environ.get("SLURM_ARRAY_JOB_ID"),
                    slurm_array_task_id=os.environ.get("SLURM_ARRAY_TASK_ID"),
                    started=datetime.now(timezone.utc).isoformat(), status="running")
    print(f"{stage} {tile or 'shared domain'} {day or ''}: {path}", flush=True)
    atomic_json(path.with_suffix(".json"), metadata)
    with path.open("w", buffering=1) as handle:
        handle.write(json.dumps(metadata) + "\n")
        try:
            yield handle
        except BaseException as error:
            metadata.update(status="failed", error=str(error))
            handle.write(str(error) + "\n")
            raise
        else:
            metadata["status"] = "complete"
        finally:
            metadata["ended"] = datetime.now(timezone.utc).isoformat()
            handle.write(json.dumps(metadata) + "\n")
            atomic_json(path.with_suffix(".json"), metadata)


@contextmanager
def serial_task_log(directory, stage, *, tile=None, day=None):
    with task_log(directory, stage, tile=tile, day=day) as handle:
        with redirect_stdout(handle), redirect_stderr(handle):
            yield
