"""Durable, event-driven submission of the existing scientific controllers."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from uuid import uuid4

from .historical import write_new
from .interpolation import file_record
from .nrt_storage import atomic_json
from .workflow import load_plan
from .workflow_logging import log_directory, reports
from .operational import _TRANSIENT_SLURM_STATES, _TERMINAL_SLURM_STATES


def _command(arguments, *, cwd=None):
    result = subprocess.run(arguments, cwd=cwd, text=True, capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f"Command failed: {shlex.join(arguments)}\n{result.stderr}")
    return result.stdout.strip()


@contextmanager
def _state(plan_path):
    record = load_plan(plan_path)
    directory = Path(record["output"]) / "control"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        path = directory / "state.json"
        state = json.loads(path.read_text()) if path.exists() else None
        if state is not None and state["workflow"] != record["identity"]:
            raise ValueError("This run directory belongs to a different workflow")
        yield record, directory, state, handle


def _save(directory, state):
    atomic_json(directory / "state.json", state)
    reports(state)


def _submit(script, directory, state, *, parents=()):
    _command(["sbatch", "--test-only", str(script)], cwd=script.parent)
    token = uuid4().hex
    intent = directory / f"submission-{token}.json"
    arguments = ["sbatch", "--parsable", "--hold", f"--comment=spires-workflow-{token}"]
    if parents:
        if any(not re.fullmatch(r"\d+", parent) for parent in parents):
            raise ValueError("Invalid parent Slurm job identity")
        arguments.append("--dependency=afterany:" + ":".join(parents))
    arguments.append(str(script))
    write_new(intent, dict(command=arguments, script=file_record(script, checksum=True)))
    state["submissions"].append(dict(intent=str(intent), status="submitting"))
    _save(directory, state)
    response = _command(arguments, cwd=script.parent)
    match = re.fullmatch(r"(\d+)(?:;blanca)?", response)
    if match is None:
        raise RuntimeError(f"Ambiguous sbatch response; inspect {intent} before recovery")
    job_id = match[1]
    write_new(intent.with_suffix(".accepted.json"), dict(job_id=job_id, response=response))
    state["submissions"][-1].update(status="held", job_id=job_id)
    _save(directory, state)
    return job_id


def _release(job_id, directory, state):
    _command(["scontrol", "-M", "blanca", "release", job_id])
    for submission in state["submissions"]:
        if submission.get("job_id") == job_id:
            submission["status"] = "released"
    _save(directory, state)


def _schedule(plan_path, record, directory, state, name, *, parents=(), worker=False):
    job_state = state["jobs"][name]
    token = uuid4().hex
    resources = record["jobs"][name]["resources"] if worker else record["coordinator"]
    interpreter = record["scheduler"].get("python", sys.executable)
    if not os.path.isabs(interpreter) or not os.access(interpreter, os.X_OK):
        raise ValueError("scheduler.python must identify an executable science interpreter")
    if resources["cluster"] != "blanca":
        raise ValueError("Unified workflows currently require the Blanca cluster")
    directives = {
        "clusters": "blanca", "partition": resources["partition"],
        "account": resources["account"], "cpus-per-task": resources["cpus"],
        "mem": resources["memory"], "time": resources["time"],
        "nodes": 1, "ntasks": 1,
    }
    if resources.get("qos"):
        directives["qos"] = resources["qos"]
    script = directory / f"{name}-{token}.sbatch"
    logs = log_directory(record) / name
    logs.mkdir(parents=True, exist_ok=True)
    command = [interpreter, "-m", "spires_batch.workflow"]
    command += (["execute-job", str(Path(plan_path).resolve()), name] if worker else
                ["advance", str(Path(plan_path).resolve()), name, token])
    lines = ["#!/bin/bash", *[f"#SBATCH --{key}={value}" for key, value in directives.items()]]
    if resources.get("exclusive"):
        lines.append("#SBATCH --exclusive")
    exclusions = record["scheduler"]["snapshot"]["exclude_nodes"]
    if exclusions:
        lines.append("#SBATCH --exclude=" + ",".join(exclusions))
    lines.extend([
        f"#SBATCH --output={shlex.quote(str(logs / ('worker-%j.out' if worker else 'controller-%j.out')))}",
        "set -euo pipefail", "module try-load slurm/blanca",
        "export SPIRES_JOB_LOG_ROOT=" + shlex.quote(str(logs)),
        "export PYTHONUNBUFFERED=1",
        "export OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK:-1}",
        shlex.join(command),
    ])
    script.write_text("\n".join(lines) + "\n")
    if not worker:
        job_state.update(token=token, status="waiting" if parents else "queued")
    job_id = _submit(script, directory, state, parents=parents)
    if worker:
        job_state["worker_job_id"] = job_id
        job_state["worker_attempt"] = job_state.get("worker_attempt", 0) + 1
        _save(directory, state)
        _schedule(plan_path, record, directory, state, name, parents=[job_id])
        _release(job_id, directory, state)
        return
    job_state["controller_job_id"] = job_id
    _save(directory, state)
    _release(job_id, directory, state)


def _ready(plan_path, record, directory, state):
    if state["status"] != "running":
        return
    if any(job["status"] in {"failed", "blocked"} for job in state["jobs"].values()):
        return
    for name in record["order"]:
        job = record["jobs"][name]
        if state["jobs"][name]["status"] != "pending":
            continue
        if all(state["jobs"][parent]["status"] == "complete" for parent in job["depends_on"]):
            _schedule(plan_path, record, directory, state, name)
    if all(job["status"] == "complete" for job in state["jobs"].values()):
        state["status"] = "complete"
    _save(directory, state)


def _job_tiles(record, name):
    request = record["jobs"][name]["request"]
    for section in (request.get("preparation", {}), request, request.get("selection", {}), record["selection"]):
        if "tiles" in section:
            return sorted(section["tiles"])
    return []


def start(plan_path):
    with _state(plan_path) as (record, directory, state, handle):
        if state is not None:
            raise FileExistsError("Workflow already submitted; inspect workflow status, do not resubmit")
        state = dict(workflow=record["identity"], status="running", submissions=[],
                     output=record["output"], selection=record["selection"],
                     log_directory=str(log_directory(record)), control_directory=str(directory),
                     jobs={name: dict(status="pending", tiles=_job_tiles(record, name),
                                      max_auto_retry_count=record["jobs"][name]["max_auto_retry_count"])
                           for name in record["order"]})
        _save(directory, state)
        try:
            _ready(plan_path, record, directory, state)
        except Exception as error:
            state.update(status="failed", error=str(error))
            _save(directory, state)
            raise
        return state


def status(plan_path):
    record = load_plan(plan_path)
    path = Path(record["output"]) / "control/state.json"
    if not path.exists():
        return dict(status="not_submitted", order=record["order"])
    state = json.loads(path.read_text())
    if state["workflow"] != record["identity"]:
        raise ValueError("Workflow identity differs from submission")
    return state


def _indices(script):
    match = re.search(r"^#SBATCH --array=([0-9,-]+)%[1-9][0-9]*$", script.read_text(), re.M)
    if match is None:
        raise ValueError("Expected a renderer-produced bounded task array")
    indices = []
    for part in match[1].split(","):
        limits = part.split("-")
        indices.extend(range(int(limits[0]), int(limits[-1]) + 1))
    if not indices or len(set(indices)) != len(indices):
        raise ValueError("Empty or repeated array indices")
    return indices


def _accounting(job_id, indices=None):
    expected = {f"{job_id}_{index}": index for index in indices} if indices is not None else {job_id: 0}
    for attempt in range(6):
        output = _command(["sacct", "-M", "blanca", "-j", job_id, "--noheader", "--parsable2",
                           "--format=JobID%64,State%40,ExitCode"])
        rows = {fields[0].strip(): [field.strip() for field in fields[1:3]]
                for line in output.splitlines() if len(fields := line.strip().split("|")) >= 3}
        if len(expected) == 1 and job_id in rows and not set(expected).issubset(rows):
            rows[next(iter(expected))] = rows[job_id]
        for fields in rows.values():
            fields[0] = fields[0].split()[0].rstrip("+")
        if set(expected).issubset(rows) and all(rows[name][0] in _TERMINAL_SLURM_STATES for name in expected):
            return {index: dict(state=rows[name][0], exit_code=rows[name][1], slurm_id=name)
                    for name, index in expected.items()}
        if attempt < 5:
            time.sleep(10)
    raise RuntimeError("Scheduler accounting is incomplete or unrecognized; no retry or downstream work submitted")


def _retry_indices(outcomes, attempt, limit):
    failed = {index: result for index, result in outcomes.items()
              if (result["state"], result["exit_code"]) != ("COMPLETED", "0:0")}
    permanent = {index: result for index, result in failed.items()
                 if result["state"] not in _TRANSIENT_SLURM_STATES}
    if permanent:
        raise RuntimeError(f"Non-retryable task failure: {permanent}")
    if failed and attempt > limit:
        raise RuntimeError(f"Automatic retries exhausted ({limit} retries after the first attempt): {failed}")
    return sorted(failed)


def _array_logs(script, job_id, index):
    paths = []
    for value in re.findall(r"^#SBATCH --(?:output|error)=(.*)$", script.read_text(), re.M):
        paths.append(shlex.split(value)[0].replace("%A", job_id).replace("%a", str(index)))
    return paths


def _verify_historical(plan_path, indices):
    import h5py
    from .historical import load_plan as load_historical, receipt_inventory

    plan = load_historical(plan_path)
    request = plan["request"]
    for index in indices:
        day = plan["days"][index]
        receipt = json.loads((Path(request["output"]) / "daily" / day / "prepared-receipts.json").read_text())
        if receipt["identity"] != plan["identity"] or receipt["date"] != day:
            raise ValueError("Historical completion receipt belongs to a different plan/date")
        receipt_inventory(receipt, request["preparation"]["tiles"], plan)
        for item in receipt["receipts"]:
            if item["prepared"] is None:
                continue
            if item["daily_albedo"] != file_record(item["daily_albedo"]["path"]):
                raise ValueError("Daily albedo changed or is missing")
            with h5py.File(item["daily_albedo"]["path"]) as albedo:
                if (not albedo.attrs.get("complete") or albedo.attrs.get("identity") != plan["identity"]
                    or albedo.attrs.get("tile") != item["tile"] or albedo.attrs.get("date") != day):
                    raise ValueError("Daily albedo failed completion verification")
            with h5py.File(item["prepared"]["state"]["path"]) as prepared:
                if (not prepared.attrs.get("complete") or prepared.attrs.get("date") != day
                    or prepared.attrs.get("tile") != item["tile"]
                    or prepared.attrs.get("prepared_context") != item["prepared_context"]):
                    raise ValueError("Historical prepared observations are incomplete or mismatched")


def _verify_interpolation(plan_path, stage, indices):
    from .interpolation import load_plan as load_interpolation
    from spires_interpolation.raster_io import screen_path, spatial_path, property_path, output_is_complete
    from spires_interpolation.publication import current_generation, artifact_paths

    plan = load_interpolation(plan_path)
    request = plan["request"]
    for index in indices:
        task = plan["tasks"][stage][index]
        if stage == "export":
            root = Path(request["output"]) / "products" / task["tile"]
            generation = current_generation(root)
            if generation is None or generation["request"] != dict(plan=plan["identity"], tile=task["tile"]):
                raise ValueError("Interpolation publication belongs to a different plan")
            artifacts = artifact_paths(root, generation)
            if not {"first", "checkpoints"}.issubset(artifacts):
                raise ValueError("Interpolation publication lacks products/checkpoints")
            continue
        paths = ([spatial_path(request, tile, task["day"]) for tile in request["tiles"]]
                 if stage == "spatial" else
                 [(screen_path if stage == "screen" else property_path)(request, **task)])
        if not all(output_is_complete(path, plan["identity"]) for path in paths):
            raise ValueError(f"Incomplete {stage} scientific products")


def _arrays(plan_path, record, directory, state, name, work):
    from . import historical, interpolation

    job = record["jobs"][name]
    current = state["jobs"][name]
    scientific_plan = work / "scientific-plan.json"
    if "scripts" not in current:
        request = work / "request.json"
        write_new(request, job["request"])
        if job["kind"] == "historical":
            historical.plan(request, scientific_plan)
            historical.render(scientific_plan, work / "arrays")
            scripts = [dict(stage="historical", path=str(path)) for path in sorted((work / "arrays").glob("*.sbatch"))]
        else:
            if job["historical_job"]:
                parent = record["jobs"][job["historical_job"]]
                historical.assemble(request, parent["request"]["output"], work / "bound")
                request = work / "bound/interpolation.json"
            interpolation.plan(request, scientific_plan)
            scripts = interpolation.render(scientific_plan, work / "arrays")
        if not scripts:
            raise ValueError("Scientific plan produced no arrays")
        logs = log_directory(record) / name
        logs.mkdir(parents=True, exist_ok=True)
        for script in scripts:
            path = Path(script["path"])
            content = re.sub(r"^#SBATCH --output=.*$", lambda match: "#SBATCH --output=" + shlex.quote(str(logs / (path.stem + "-%A_%a.out"))), path.read_text(), flags=re.M)
            content = re.sub(r"^#SBATCH --error=.*$", lambda match: "#SBATCH --error=" + shlex.quote(str(logs / (path.stem + "-%A_%a.err"))), content, flags=re.M)
            content = content.replace("set -euo pipefail", "set -euo pipefail\nexport PYTHONUNBUFFERED=1\nexport SPIRES_JOB_LOG_ROOT=" + shlex.quote(str(logs)))
            path.write_text(content)
            script["identity"] = file_record(script["path"], checksum=True)
        current.update(scripts=scripts, cursor=0, planned_tasks=sum(len(_indices(Path(script["path"]))) for script in scripts))
        _save(directory, state)
    if "array_job_id" in current:
        script = current.get("retry_script", current["scripts"][current["cursor"]])
        path = Path(script["path"])
        if file_record(path, checksum=True) != script["identity"]:
            raise ValueError("Rendered array script changed")
        outcomes = _accounting(current["array_job_id"], _indices(path))
        attempt = current.get("array_attempt", 1)
        planned = json.loads(scientific_plan.read_text())
        entries = []
        for index, outcome in outcomes.items():
            if job["kind"] == "historical":
                day = planned["days"][index]
                tiles = sorted(planned["request"]["preparation"]["tiles"])
                task = dict(date=day, tiles=tiles, missing_tiles=[tile for tile in tiles if planned["inventory"][tile + "/" + day] is None])
            else:
                task = planned["tasks"][script["stage"]][index]
                task = dict(date=task.get("day", ""), tiles=[task["tile"]] if "tile" in task else sorted(planned["request"]["tiles"]))
            entries.append(dict(stage=script["stage"], index=index, attempt=attempt, verified=False,
                                logs=_array_logs(path, current["array_job_id"], index), **task, **outcome))
        current.setdefault("attempts", []).extend(entries)
        _save(directory, state)
        for entry in entries:
            if (entry["state"], entry["exit_code"]) != ("COMPLETED", "0:0"):
                continue
            if job["kind"] == "historical":
                _verify_historical(scientific_plan, [entry["index"]])
            else:
                _verify_interpolation(scientific_plan, script["stage"], [entry["index"]])
            entry["verified"] = True
        retry = _retry_indices(outcomes, attempt, job["max_auto_retry_count"])
        del current["array_job_id"]
        if retry:
            retry_path = path.parent / f"retry-{current['cursor']}-{attempt + 1}.sbatch"
            content = re.sub(r"(?m)^(#SBATCH --array=)[0-9,-]+(%[0-9]+)$", lambda match: match[1] + ",".join(map(str, retry)) + match[2], path.read_text())
            with retry_path.open("x") as handle:
                handle.write(content)
            current.update(retry_script=dict(stage=script["stage"], path=str(retry_path), identity=file_record(retry_path, checksum=True)), array_attempt=attempt + 1)
        else:
            current["cursor"] += 1
            current.pop("retry_script", None)
            current["array_attempt"] = 1
        _save(directory, state)
    if current["cursor"] == len(current["scripts"]):
        return True
    script = current.get("retry_script", current["scripts"][current["cursor"]])
    path = Path(script["path"])
    if file_record(path, checksum=True) != script["identity"]:
        raise ValueError("Rendered array script changed")
    array_id = _submit(path, directory, state)
    current["array_job_id"] = array_id
    _save(directory, state)
    _schedule(plan_path, record, directory, state, name, parents=[array_id])
    _release(array_id, directory, state)
    return False


def _daily(plan_path, record, directory, state, name, work):
    from .cli import main
    from .operational import start_operational_run

    current = state["jobs"][name]
    operation = log_directory(record) / name / "operation"
    current["operation_directory"] = str(operation)
    if not current.get("operation_started"):
        write_new(work / "request.json", record["jobs"][name]["request"])
        if main(["plan", str(work / "request.json"), "--output", str(work / "scientific-plan.json")]):
            raise ValueError("Batch scientific preflight failed")
        current["operation_started"] = True
        _save(directory, state)
        launch = start_operational_run(work / "scientific-plan.json", state_root=record["jobs"][name]["request"]["output"]["root"], output_directory=operation)
        current["observed_coordinator"] = launch.coordinator_job_id
        _schedule(plan_path, record, directory, state, name, parents=[launch.coordinator_job_id])
        return False
    terminal = operation / "operational-result.json"
    if terminal.exists():
        result = json.loads(terminal.read_text())
        if result["status"] != "succeeded":
            raise RuntimeError(f"Batch operation failed: {result['message']}")
        return True
    coordinators = sorted((operation / "waves").glob("*/coordinator-submission.json"))
    if not coordinators:
        raise RuntimeError("Batch coordinator missing; inspect the operational submission ledger")
    latest = json.loads(coordinators[-1].read_text())["job_id"]
    if latest == current["observed_coordinator"]:
        raise RuntimeError("Batch coordinator ended without a terminal result or a successor")
    current["observed_coordinator"] = latest
    _schedule(plan_path, record, directory, state, name, parents=[latest])
    return False


def _single(plan_path, record, directory, state, name, work):
    current = state["jobs"][name]
    job = record["jobs"][name]
    if "worker_job_id" in current:
        outcomes = _accounting(current["worker_job_id"])
        outcome = outcomes[0]
        entry = dict(stage=job["kind"], index=0, attempt=current["worker_attempt"],
                     tiles=sorted(job["request"].get("tiles", record["selection"].get("tiles", []))),
                     logs=[str(log_directory(record) / name / f"worker-{current['worker_job_id']}.out")],
                     verified=False, **outcome)
        current.setdefault("attempts", []).append(entry)
        _save(directory, state)
        retry = _retry_indices(outcomes, current["worker_attempt"], job["max_auto_retry_count"])
        if not retry:
            from .interpolation import digest

            receipt = json.loads((work / "complete.json").read_text())
            if receipt != dict(workflow=record["identity"], job=name, request=digest(job["request"])):
                raise ValueError("Worker completion receipt is missing or mismatched")
            entry["verified"] = True
            return True
    _schedule(plan_path, record, directory, state, name, worker=True)
    return False


def advance(plan_path, name, token):
    if not os.environ.get("SLURM_JOB_ID"):
        raise RuntimeError("Workflow advancement requires a Slurm allocation")
    with _state(plan_path) as (record, directory, state, handle):
        if state is None or name not in state["jobs"]:
            raise ValueError("Workflow has not been submitted")
        current = state["jobs"][name]
        if current.get("token") != token or current["status"] not in {"queued", "waiting"}:
            raise ValueError("Stale or duplicate workflow continuation")
        current["status"] = "running"
        _save(directory, state)
        try:
            if state["status"] != "running":
                raise RuntimeError("Workflow stopped; inspect its failure before recovering")
            if any(state["jobs"][parent]["status"] != "complete" for parent in record["jobs"][name]["depends_on"]):
                raise ValueError("Prerequisite scientific verification has not completed")
            work = Path(record["output"]) / name
            work.mkdir(parents=True, exist_ok=True)
            kind = record["jobs"][name]["kind"]
            if kind in {"historical", "interpolation"}:
                completed = _arrays(plan_path, record, directory, state, name, work)
            elif kind == "daily":
                completed = _daily(plan_path, record, directory, state, name, work)
            else:
                completed = _single(plan_path, record, directory, state, name, work)
            if completed:
                current["status"] = "complete"
                if kind in {"daily", "historical", "interpolation"}:
                    atomic_json(work / "complete.json", dict(workflow=record["identity"], job=name))
                _save(directory, state)
                _ready(plan_path, record, directory, state)
        except Exception as error:
            state.update(status="failed", error=str(error))
            current.update(status="failed", error=str(error))
            for other in state["jobs"].values():
                if other["status"] == "pending":
                    other["status"] = "blocked"
            _save(directory, state)
            raise
        return state
