"""One configuration for dependency-ordered retrieval, interpolation and NRT jobs.

Planning freezes configuration and displays the complete dependency graph without
performing acquisition or scientific work. Each job resolves its input inventory
only after its prerequisites succeed, so generated inputs need not exist at plan
time. Existing scientific planners retain their authoritative preflight checks.
"""

from __future__ import annotations

import argparse
import copy
from datetime import date
import json
import os
from pathlib import Path
import re
import shlex
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .configuration import PublicConfiguration, require_configuration


class Resources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    partition: str = Field(default="blanca-snow", pattern=r"^[A-Za-z0-9_.-]+$")
    account: str = Field(default="blanca-snow", pattern=r"^[A-Za-z0-9_.-]+$")
    cpus: int = Field(default=1, ge=1, strict=True)
    memory: str = Field(default="8G", pattern=r"^(?:0|[1-9][0-9]*[KMGT]?)$")
    cluster: str = Field(default="blanca", pattern=r"^[A-Za-z0-9_.-]+$", description="Slurm cluster for this allocation.")
    qos: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]+$", description="Optional authorized Slurm QoS; named policy queues supply this explicitly.")
    exclusive: bool = Field(default=False, description="Reserve the whole node; required when memory is 0 (all node memory).")
    time: str = Field(
        default="04:00:00", pattern=r"^(?:[0-9]+-)?[0-9]+(?::[0-5][0-9]){0,2}$"
    )

    @model_validator(mode="after")
    def validate_memory(self):
        if self.memory == "0" and not self.exclusive:
            raise ValueError("All-node memory requires an exclusive allocation")
        return self


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    depends_on: list[str] = Field(default_factory=list)
    request: dict | str
    resources: Resources = Field(default_factory=Resources)
    job_type: str | None = Field(default=None, description="Optional named resource profile from the workflow scheduler policy.")
    queue: str | None = Field(default=None, description="Optional named queue override for this job's policy profile.")
    historical_job: str | None = Field(default=None, description="For interpolation, assemble observations and inventory from this historical prerequisite automatically.")
    max_auto_retry_count: int | None = Field(default=None, ge=0, strict=True, description="Retries after the first attempt; overrides the workflow default for this job. Only recognized transient scheduler failures are retried.")

    @model_validator(mode="after")
    def validate_kind(self):
        if self.kind not in {
            "initialize",
            "daily",
            "prepare",
            "interpolation",
            "acquire",
            "nrt",
            "historical",
            "register",
        }:
            raise ValueError(f"Unknown job kind: {self.kind}")
        return self


class Workflow(PublicConfiguration):
    model_config = ConfigDict(extra="forbid")
    config_type: Literal["workflow"]
    mode: str = "historical"
    sensor: str = "viirs"
    platform: str = "noaa20"
    selection: dict = Field(default_factory=dict)
    output: str
    log_root: str | None = Field(default=None, description="Base directory for logs and readable summaries. Each workflow gets an identity-named subdirectory. Defaults to output/logs; scientific outputs are not moved.")
    max_auto_retry_count: int = Field(default=3, ge=0, strict=True, description="Maximum additional attempts for transient scheduler failures; 0 disables retries. Per-job overrides take precedence. Explicit daily request.execution.max_auto_retry_count takes precedence over this default.")
    scheduler: dict = Field(default_factory=dict)
    jobs: dict[str, Job]
    selected_jobs: list[str] = Field(default_factory=list)
    build_prerequisites: bool = False
    variables: dict[str, str] = Field(default_factory=dict, description="Literal substitutions for ${name} in configuration values; no shell expansion or recursive substitutions.")
    coordinator: Resources = Field(default_factory=lambda: Resources(memory="4G", time="01:00:00"), description="Non-preemptable resources for planning, verification and automatic submissions, separate from scientific task resources.")

    @model_validator(mode="after")
    def validate_workflow(self):
        from .models import RunConfig, SelectionConfig

        identity = RunConfig(
            name="workflow", sensor=self.sensor, platform=self.platform
        )
        self.sensor, self.platform = identity.sensor, identity.platform
        self.selection = SelectionConfig.model_validate(self.selection).model_dump(
            mode="json", exclude_unset=True
        )
        if self.mode not in {"historical", "nrt"}:
            raise ValueError("Expected historical or nrt mode")
        if not self.jobs:
            raise ValueError("At least one job is required")
        if set(self.selection) - {"tiles", "water_years", "dates"}:
            raise ValueError("Selection supports tiles, water_years and dates")
        for name, job in self.jobs.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError(f"Invalid job name: {name}")
            if set(job.depends_on) - self.jobs.keys():
                raise ValueError(f"Unknown prerequisites for {name}")
            if self.mode == "historical" and job.kind == "nrt":
                raise ValueError("NRT cycle jobs require mode=nrt")
            if job.historical_job is not None:
                if job.kind != "interpolation" or job.historical_job not in job.depends_on:
                    raise ValueError("historical_job requires an interpolation job and an explicit dependency")
                if self.jobs[job.historical_job].kind != "historical":
                    raise ValueError("historical_job must name a historical job")
        if set(self.selected_jobs) - self.jobs.keys():
            raise ValueError("Unknown selected jobs")
        return self


def ordered_jobs(config):
    """Select an explicit dependency closure and reject cycles before any writes."""
    selected = set(config.selected_jobs or config.jobs)
    if config.build_prerequisites:
        pending = list(selected)
        while pending:
            for dependency in config.jobs[pending.pop()].depends_on:
                if dependency not in selected:
                    selected.add(dependency)
                    pending.append(dependency)
    ordered = []
    visiting = set()

    def visit(name):
        if name in visiting:
            raise ValueError(f"Dependency cycle at {name}")
        if name in ordered:
            return
        visiting.add(name)
        for dependency in config.jobs[name].depends_on:
            if dependency not in selected:
                raise ValueError(
                    f"{name} requires {dependency}; select it or enable build_prerequisites. For existing inputs, remove that dependency explicitly."
                )
            visit(dependency)
        visiting.remove(name)
        ordered.append(name)

    for name in config.jobs:
        if name in selected:
            visit(name)
    return ordered


PATH_KEYS = {
    "path",
    "output",
    "root",
    "stream_root",
    "science_python",
    "cloud_python",
    "cloud_model",
    "snow_library",
    "spatial_library",
    "albedo_lut",
    "lut",
    "seed_plan",
    "receipt",
    "inventory",
    "historical_plan",
    "python",
    "cloud",
    "context",
    "destination",
    "archive_root",
    "reflectance_root",
    "durable_root",
    "scratch_root",
    "log_root",
}


def expand_variables(value, variables):
    if isinstance(value, dict):
        return {name: expand_variables(child, variables) for name, child in value.items()}
    if isinstance(value, list):
        return [expand_variables(child, variables) for child in value]
    if isinstance(value, str):
        def replacement(match):
            name = match.group(1)
            if name not in variables:
                raise ValueError(f"Unknown workflow variable: {name}")
            result = variables[name]
            if not isinstance(result, str) or "${" in result:
                raise ValueError("Workflow variables must be literal strings")
            return result
        return re.sub(r"\$\{([^}]+)\}", replacement, value)
    return value


def resolve_paths(value, base, key="", parent=""):
    if isinstance(value, dict):
        return {
            name: resolve_paths(child, base, name, key) for name, child in value.items()
        }
    if isinstance(value, list):
        return [resolve_paths(child, base, key, parent) for child in value]
    if isinstance(value, str) and (
        key in PATH_KEYS
        or key.endswith("_source")
        or (key == "policy" and parent in {"scheduler", "execution"})
    ):
        path = Path(value).expanduser()
        return str((base / path).resolve() if not path.is_absolute() else path)
    return value


def plan(config_path, destination, *, cycle_job=None, through=None):
    from .interpolation import digest
    from .scheduler_policy import load_policy, profile_resources, resolve_batch_request, resolve_interpolation_scheduler, resolve_cycle_policy

    source = Path(config_path).resolve()
    raw = json.loads(source.read_text())
    variables = raw.pop("variables", {})
    raw = expand_variables(raw, variables)
    if cycle_job is not None:
        if cycle_job not in raw["jobs"] or raw["jobs"][cycle_job]["kind"] != "nrt":
            raise ValueError("cycle --job must select an NRT job")
        date.fromisoformat(through)
        raw.update(mode="nrt", selected_jobs=[cycle_job], build_prerequisites=False,
                   output=str(Path(destination).resolve().with_suffix("")) + "-run")
        raw["jobs"][cycle_job]["depends_on"] = []
    config = Workflow.model_validate({**raw, "variables": variables})
    order = ordered_jobs(config)
    scheduler = resolve_paths(config.scheduler, source.parent, "scheduler")
    scheduler.setdefault("python", sys.executable)
    allowed_scheduler = set(Resources.model_fields) | {"python", "policy", "job_type", "queue"}
    if set(scheduler) - allowed_scheduler or "policy" not in scheduler:
        raise ValueError(
            "Scheduler requires a policy path and supported resource fields"
        )
    if isinstance(scheduler["policy"], dict):
        from .scheduler_policy import validate_policy

        policy = validate_policy(scheduler["policy"])
        scheduler["policy"] = str(source)
    else:
        scheduler["policy"], policy = load_policy(scheduler["policy"])
    if config.coordinator.cluster != "blanca" or config.coordinator.qos == "preemptable" or config.coordinator.partition == "blanca":
        raise ValueError("Workflow coordinators must use a non-preemptable Blanca queue")
    exclusions = policy.get("exclude_nodes")
    if not isinstance(exclusions, list) or any(
        not isinstance(node, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", node)
        for node in exclusions
    ):
        raise ValueError("Scheduler policy requires valid exclude_nodes")
    scheduler["snapshot"] = policy
    requests = {}
    for name in order:
        job = config.jobs[name]
        job_type = job.job_type or scheduler.get("job_type")
        queue = job.queue or scheduler.get("queue")
        if queue and not job_type:
            raise ValueError("Workflow queue selection requires a job_type")
        defaults = profile_resources(policy, job_type, queue=queue) if job_type else {}
        resources = Resources.model_validate(
            {
                **{key: value for key, value in defaults.items() if key in Resources.model_fields},
                **{
                    key: value
                    for key, value in scheduler.items()
                    if key in Resources.model_fields
                },
                **job.resources.model_dump(exclude_unset=True),
            }
        ).model_dump()
        if resources["cluster"] != "blanca":
            raise ValueError("Unified workflows currently require Blanca resources")
        if isinstance(job.request, str):
            path = (source.parent / job.request).resolve()
            request = resolve_paths(expand_variables(json.loads(path.read_text()), variables), path.parent)
        else:
            request = resolve_paths(copy.deepcopy(job.request), source.parent)
        if name == cycle_job:
            request["through"] = through
            if not (Path(request["stream_root"]) / "current.json").is_file():
                raise ValueError("Daily continuation requires an already registered NRT stream")
        if job.kind in {"historical", "interpolation", "nrt"}:
            settings = request.setdefault("scheduler", {})
            settings.setdefault("policy", scheduler["policy"])
            if settings["policy"] == scheduler["policy"]:
                settings["snapshot"] = policy
            if job_type:
                settings["job_type"] = job_type
            if queue:
                settings["queue"] = queue
            settings.update(job.resources.model_dump(exclude_unset=True))
            if job.kind == "nrt" and not settings.get("job_type"):
                for key, value in resources.items():
                    settings.setdefault(key, value)
        if job.kind == "initialize":
            from .initialization import Initialization

            request.update(sensor=config.sensor, platform=config.platform)
            for key in ("tiles", "water_years"):
                if key in config.selection:
                    request.setdefault(key, config.selection[key])
            request = Initialization.model_validate(request).model_dump(mode="json")
        elif job.kind == "daily":
            from .models import RequestConfig

            request.setdefault("run", {}).update(
                sensor=config.sensor, platform=config.platform
            )
            request["selection"] = {**config.selection, **request.get("selection", {})}
            execution = request.setdefault("execution", {})
            execution.setdefault("resources", {}).setdefault("science_python", scheduler["python"])
            for profile in execution.get("profiles", {}).values():
                profile.setdefault("resources", {}).setdefault("science_python", scheduler["python"])
            if job.max_auto_retry_count is not None:
                execution["max_auto_retry_count"] = job.max_auto_retry_count
            else:
                execution.setdefault("max_auto_retry_count", config.max_auto_retry_count)
            if job_type:
                execution.update(policy=scheduler["policy"], job_type=job_type)
                if queue:
                    execution["queue"] = queue
            if job.resources.model_fields_set:
                raise ValueError("Daily task resource overrides belong in request.execution.resources")
            request = resolve_batch_request(RequestConfig.model_validate(request), source.parent, policy_snapshot=policy if execution.get("policy") == scheduler["policy"] else None).model_dump(mode="json")
            from .planner import resolve_resource_profiles

            if any(profile.cluster != "blanca" for profile in resolve_resource_profiles(RequestConfig.model_validate(request))):
                raise ValueError("Unified batch tasks require Blanca resources")
        elif job.kind == "historical":
            from .historical import HistoricalRequest
            from .scheduler_policy import resolve_scheduler, preparation_defaults

            request["scheduler"] = resolve_scheduler(request["scheduler"], source.parent)
            request["preparation"] = preparation_defaults(request["preparation"], request["scheduler"])
            request = HistoricalRequest.model_validate(request).model_dump(mode="json")
            if config.selection.get("tiles") and set(request["preparation"]["tiles"]) != set(config.selection["tiles"]):
                raise ValueError("Historical processing must include the configured tile domain")
        elif job.kind == "interpolation":
            from spires_interpolation.algorithm import resolve_algorithm

            resolve_algorithm(
                request.get("algorithm", "viirs_snow"),
                sensor=config.sensor,
                platform=config.platform,
            )
            require_configuration(request, "interpolation")
            if config.selection.get("tiles") and set(request["tiles"]) != set(
                config.selection["tiles"]
            ):
                raise ValueError(
                    "Interpolation must include the configured donor tile domain"
                )
            scientific_scheduler = resolve_interpolation_scheduler(request["scheduler"], source.parent)
            request["scheduler"] = scientific_scheduler
            if scientific_scheduler["cluster"] != "blanca":
                raise ValueError("Unified interpolation requires Blanca resources")
        elif job.kind == "register":
            if set(request) != {"interpolation_job", "stream_root", "storage"}:
                raise ValueError("Register requires interpolation_job, stream_root and storage")
            parent = request["interpolation_job"]
            if parent not in job.depends_on or config.jobs[parent].kind != "interpolation":
                raise ValueError("Registration requires an explicit interpolation prerequisite")
            if not config.jobs[parent].historical_job:
                raise ValueError("Registration requires interpolation assembled from a historical_job")
            require_configuration(request["storage"], "nrt_storage")
        elif job.kind == "prepare":
            require_configuration(request, "prepared_observation")
            if config.sensor != "viirs" or config.platform != "noaa20":
                raise ValueError(
                    "Retained-observation preparation currently supports NOAA-20 only"
                )
            if "output" not in request:
                raise ValueError("Preparation requires an explicit output file")
        elif job.kind in {"nrt", "acquire"}:
            require_configuration(request, "nrt_cycle" if job.kind == "nrt" else "acquisition")
            if job.kind == "nrt":
                request = resolve_cycle_policy(request, source.parent)
                resources = Resources.model_validate({key: request["scheduler"][key] for key in Resources.model_fields}).model_dump()
                if resources["cluster"] != "blanca":
                    raise ValueError("Unified NRT requires Blanca resources")
            if config.sensor != "viirs" or config.platform != "noaa20":
                raise ValueError("Acquisition/NRT currently supports NOAA-20 only")
            date.fromisoformat(request["through"])
            if "stream_root" not in request:
                raise ValueError("Acquisition/NRT requires a registered stream_root")
        requests[name] = dict(
            kind=job.kind,
            depends_on=job.depends_on,
            request=request,
            resources=resources,
            historical_job=job.historical_job,
            max_auto_retry_count=(request["execution"]["max_auto_retry_count"] if job.kind == "daily" else
                                  job.max_auto_retry_count if job.max_auto_retry_count is not None else config.max_auto_retry_count),
        )
    output = Path(config.output).expanduser()
    output = (source.parent / output).resolve() if not output.is_absolute() else output
    record = dict(
        artifact_type="spires_workflow",
        schema_version=4,
        mode=config.mode,
        sensor=config.sensor,
        platform=config.platform,
        selection=config.selection,
        output=str(output),
        log_root=str((source.parent / Path(config.log_root).expanduser()).resolve()) if config.log_root else str(output / "logs"),
        order=order,
        jobs=requests,
        scheduler=scheduler,
        coordinator=config.coordinator.model_dump(),
    )
    record["identity"] = digest(record)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x") as handle:
        json.dump(record, handle, indent=2)
    return record


def load_plan(path):
    from .interpolation import digest

    record = json.loads(Path(path).read_text())
    if (
        record.get("artifact_type") != "spires_workflow"
        or record.get("schema_version") != 4
    ):
        raise ValueError("Expected a current workflow plan; regenerate from the JSON configuration")
    identity = record.pop("identity")
    if digest(record) != identity:
        raise ValueError("Workflow configuration changed after planning")
    return dict(record, identity=identity)


def execute_job(plan_path, name):
    """Execute one job inside a compute allocation after dependency completion."""
    from .nrt_storage import atomic_json
    from .interpolation import digest
    import fcntl

    if not os.environ.get("SLURM_JOB_ID"):
        raise RuntimeError(
            "Scientific workflow execution requires a compute allocation"
        )
    record = load_plan(plan_path)
    job = record["jobs"][name]
    root = Path(record["output"])
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        completed = directory / "complete.json"
        if completed.exists():
            if json.loads(completed.read_text()) != dict(workflow=record["identity"], job=name, request=digest(job["request"])):
                raise ValueError("Output directory belongs to a different workflow/job/request")
            return
        for dependency in job["depends_on"]:
            receipt = json.loads((root / dependency / "complete.json").read_text())
            if receipt["workflow"] != record["identity"]:
                raise ValueError("Prerequisite belongs to another workflow")
        request = directory / "request.json"
        if request.exists() and json.loads(request.read_text()) != job["request"]:
            raise ValueError("Existing job request differs")
        atomic_json(request, job["request"])
        kind = job["kind"]
        if kind == "initialize":
            from .initialization import initialize

            report = initialize(job["request"])
            if report["missing_required_assets"]:
                raise ValueError("Required initialization assets are missing: " + ", ".join(report["missing_required_assets"]))
        elif kind == "register":
            from .nrt_storage import configure
            from .nrt import bootstrap

            settings = job["request"]
            parent = root / settings["interpolation_job"]
            configure(settings["stream_root"], settings["storage"])
            bootstrap(parent / "scientific-plan.json", parent / "bound/inventory.json", settings["stream_root"])
        elif kind == "prepare":
            from .inputs import prepare_retained

            prepared = copy.deepcopy(job["request"])
            output = prepared.pop("output")
            atomic_json(directory / "prepared.json", prepare_retained(prepared, output))
        elif kind == "acquire":
            from .acquisition import acquire

            settings = job["request"]
            acquire(
                settings["stream_root"],
                settings["through"],
                directory / "acquisition",
                collection=settings.get("collection", "002"),
                fetch=True,
            )
        elif kind == "nrt":
            from .pipeline import cycle

            workspace_record = directory / "cycle-workspace.json"
            previous = json.loads(workspace_record.read_text())["path"] if workspace_record.exists() else None
            workspace = Path(previous) if previous and (Path(previous) / "revision/update.json").exists() else directory / ("cycle-" + os.environ["SLURM_JOB_ID"])
            atomic_json(workspace_record, dict(path=str(workspace)))
            cycle(request, job["request"]["through"], workspace, run=True)
        atomic_json(
            completed,
            dict(workflow=record["identity"], job=name, request=digest(job["request"])),
        )


def render(plan_path, destination):
    """Write a reviewed launcher; scientific arrays are planned after prerequisites."""
    record = load_plan(plan_path)
    directory = Path(destination).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    command = [record["scheduler"].get("python", sys.executable), "-m", "spires_batch.workflow", "start", str(Path(plan_path).resolve())]
    (directory / "submit.sh").write_text("#!/bin/bash\nset -euo pipefail\nmodule try-load slurm/blanca\n" + shlex.join(command) + "\n")
    (directory / "preview.json").write_text(json.dumps(dict(order=record["order"], coordinator=record["coordinator"], jobs=record["jobs"]), indent=2) + "\n")
    return directory




def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help="Print the universal workflow JSON schema")
    prepare = commands.add_parser("plan")
    prepare.add_argument("config")
    prepare.add_argument("--output", required=True)
    render_command = commands.add_parser("render-slurm")
    render_command.add_argument("plan")
    render_command.add_argument("--output", required=True)
    execute = commands.add_parser("execute-job")
    execute.add_argument("plan")
    execute.add_argument("name")
    submit = commands.add_parser("submit", help="Plan and submit a complete workflow from one JSON")
    submit.add_argument("config")
    submit.add_argument("--output", required=True, help="New immutable workflow plan filename")
    start = commands.add_parser("start", help="Submit an already reviewed workflow plan once")
    start.add_argument("plan")
    advance = commands.add_parser("advance", help="Internal event-driven continuation")
    advance.add_argument("plan")
    advance.add_argument("name")
    advance.add_argument("token")
    status = commands.add_parser("status", help="Read durable progress, job identities and failure reasons")
    status.add_argument("plan")
    cycle = commands.add_parser("cycle", help="Submit only an NRT job from the unified JSON against existing history")
    cycle.add_argument("config")
    cycle.add_argument("--job", required=True)
    cycle.add_argument("--through", required=True)
    cycle.add_argument("--output", required=True, help="New immutable cycle workflow plan filename")
    arguments = parser.parse_args(argv)
    if arguments.command == "schema":
        print(json.dumps(Workflow.model_json_schema(), indent=2))
    elif arguments.command == "plan":
        record = plan(arguments.config, arguments.output)
        print(
            json.dumps({"order": record["order"], "output": record["output"]}, indent=2)
        )
    elif arguments.command == "render-slurm":
        print(render(arguments.plan, arguments.output))
    elif arguments.command == "execute-job":
        record = load_plan(arguments.plan)
        if record["jobs"][arguments.name]["kind"] in {"daily", "historical", "interpolation"}:
            raise ValueError("Use workflow submit/start to preserve array scheduling")
        execute_job(arguments.plan, arguments.name)
    else:
        from . import workflow_execution

        if arguments.command == "cycle":
            plan(arguments.config, arguments.output, cycle_job=arguments.job, through=arguments.through)
            result = workflow_execution.start(arguments.output)
        elif arguments.command == "submit":
            plan(arguments.config, arguments.output)
            result = workflow_execution.start(arguments.output)
        elif arguments.command == "start":
            result = workflow_execution.start(arguments.plan)
        elif arguments.command == "status":
            result = workflow_execution.status(arguments.plan)
        else:
            result = workflow_execution.advance(arguments.plan, arguments.name, arguments.token)
        print(json.dumps(result, indent=2))
    return 0


def main(argv=None):
    from .nrt_storage import FullReplayRequired

    try:
        return _main(argv)
    except FullReplayRequired as error:
        print(str(error), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
