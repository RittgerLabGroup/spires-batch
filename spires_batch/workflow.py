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

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Resources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    partition: str = Field(default="blanca-snow", pattern=r"^[A-Za-z0-9_.-]+$")
    account: str = Field(default="blanca-snow", pattern=r"^[A-Za-z0-9_.-]+$")
    cpus: int = Field(default=1, ge=1, strict=True)
    memory: str = Field(default="8G", pattern=r"^[1-9][0-9]*[KMGT]?$")
    time: str = Field(
        default="04:00:00", pattern=r"^(?:[0-9]+-)?[0-9]+(?::[0-5][0-9]){0,2}$"
    )


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    depends_on: list[str] = Field(default_factory=list)
    request: dict | str
    resources: Resources = Field(default_factory=Resources)

    @model_validator(mode="after")
    def validate_kind(self):
        if self.kind not in {"daily", "prepare", "interpolation", "acquire", "nrt"}:
            raise ValueError(f"Unknown job kind: {self.kind}")
        return self


class Workflow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 2
    mode: str = "historical"
    sensor: str = "viirs"
    platform: str = "noaa20"
    selection: dict = Field(default_factory=dict)
    output: str
    scheduler: dict = Field(default_factory=dict)
    jobs: dict[str, Job]
    selected_jobs: list[str] = Field(default_factory=list)
    build_prerequisites: bool = False

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
        if self.schema_version != 2 or self.mode not in {"historical", "nrt"}:
            raise ValueError("Expected workflow schema 2 and historical or nrt mode")
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
}


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
        or (key == "policy" and parent == "scheduler")
    ):
        path = Path(value).expanduser()
        return str((base / path).resolve() if not path.is_absolute() else path)
    return value


def plan(config_path, destination):
    from .interpolation import digest

    source = Path(config_path).resolve()
    config = Workflow.model_validate_json(source.read_text())
    order = ordered_jobs(config)
    scheduler = resolve_paths(config.scheduler, source.parent, "scheduler")
    allowed_scheduler = set(Resources.model_fields) | {"python", "policy"}
    if set(scheduler) - allowed_scheduler or "policy" not in scheduler:
        raise ValueError(
            "Scheduler requires a policy path and supported resource fields"
        )
    policy = json.loads(Path(scheduler["policy"]).read_text())
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
        resources = Resources.model_validate(
            {
                **{
                    key: value
                    for key, value in scheduler.items()
                    if key in Resources.model_fields
                },
                **job.resources.model_dump(exclude_unset=True),
            }
        ).model_dump()
        if isinstance(job.request, str):
            path = (source.parent / job.request).resolve()
            request = resolve_paths(json.loads(path.read_text()), path.parent)
        else:
            request = resolve_paths(copy.deepcopy(job.request), source.parent)
        if job.kind == "daily":
            from .models import RequestConfig

            request.setdefault("run", {}).update(
                sensor=config.sensor, platform=config.platform
            )
            request["selection"] = {**config.selection, **request.get("selection", {})}
            RequestConfig.model_validate(request)
        elif job.kind == "interpolation":
            from spires_interpolation.algorithm import resolve_algorithm

            resolve_algorithm(
                request.get("algorithm", "viirs_snow"),
                sensor=config.sensor,
                platform=config.platform,
            )
            if request.get("schema_version") != 2:
                raise ValueError("Interpolation requests require schema_version=2")
            if config.selection.get("tiles") and set(request["tiles"]) != set(
                config.selection["tiles"]
            ):
                raise ValueError(
                    "Interpolation must include the configured donor tile domain"
                )
            scientific_scheduler = request["scheduler"]
            requested_cpus = [scientific_scheduler["cpus"]] + [
                stage.get("cpus", scientific_scheduler["cpus"])
                for stage in scientific_scheduler.get("stages", {}).values()
            ]
            if max(requested_cpus) > resources["cpus"]:
                raise ValueError(
                    f"{name}: workflow allocation must cover every interpolation stage's CPU count"
                )
        elif job.kind == "prepare":
            if config.sensor != "viirs" or config.platform != "noaa20":
                raise ValueError(
                    "Retained-observation preparation currently supports NOAA-20 only"
                )
            if "output" not in request:
                raise ValueError("Preparation requires an explicit output file")
        elif job.kind in {"nrt", "acquire"}:
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
        )
    output = Path(config.output).expanduser()
    output = (source.parent / output).resolve() if not output.is_absolute() else output
    record = dict(
        artifact_type="spires_workflow",
        schema_version=2,
        mode=config.mode,
        sensor=config.sensor,
        platform=config.platform,
        selection=config.selection,
        output=str(output),
        order=order,
        jobs=requests,
        scheduler=scheduler,
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
        or record.get("schema_version") != 2
    ):
        raise ValueError("Expected a workflow schema-2 plan")
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
            if json.loads(completed.read_text())["workflow"] != record["identity"]:
                raise ValueError("Output directory belongs to a different workflow")
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
        if kind == "daily":
            from .cli import main

            resolved = directory / "daily-plan.json"
            if not resolved.exists() and main(
                ["plan", str(request), "--output", str(resolved)]
            ):
                raise RuntimeError("Daily planning failed")
            if main(
                ["execute", str(resolved), "--events-dir", str(directory / "events")]
            ):
                raise RuntimeError("Daily processing failed")
        elif kind == "prepare":
            from .inputs import prepare_retained

            prepared = copy.deepcopy(job["request"])
            output = prepared.pop("output")
            atomic_json(directory / "prepared.json", prepare_retained(prepared, output))
        elif kind == "interpolation":
            from .interpolation import (
                plan as scientific_plan,
                load_plan as scientific_load,
                execute,
            )

            resolved = directory / "interpolation-plan.json"
            planned = (
                scientific_load(resolved)
                if resolved.exists()
                else scientific_plan(request, resolved)
            )
            for stage in planned["stage_order"]:
                for index in range(len(planned["tasks"][stage])):
                    execute(resolved, stage, index)
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

            cycle(request, job["request"]["through"], directory / "cycle", run=True)
        atomic_json(
            completed,
            dict(workflow=record["identity"], job=name, request=digest(job["request"])),
        )


def render(plan_path, destination):
    """Render one dependency-linked Slurm allocation per configured job."""
    record = load_plan(plan_path)
    directory = Path(destination).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    scheduler = record["scheduler"]
    policy = scheduler["snapshot"]
    exclusions = policy["exclude_nodes"]
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", node) for node in exclusions):
        raise ValueError("Invalid excluded node")
    scripts = {}
    submit = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "module try-load slurm/blanca",
    ]
    for index, name in enumerate(record["order"]):
        resources = {**scheduler, **record["jobs"][name]["resources"]}
        directives = {
            "clusters": "blanca",
            "partition": resources.get("partition", "blanca-snow"),
            "account": resources.get("account", "blanca-snow"),
            "cpus-per-task": str(resources.get("cpus", 1)),
            "mem": resources.get("memory", "8G"),
            "time": resources.get("time", "04:00:00"),
        }
        if any(
            not re.fullmatch(r"[A-Za-z0-9_.:-]+", str(value))
            for value in directives.values()
        ):
            raise ValueError("Invalid scheduler resource value")
        lines = ["#!/usr/bin/env bash"] + [
            f"#SBATCH --{key}={value}" for key, value in directives.items()
        ]
        if exclusions:
            lines.append("#SBATCH --exclude=" + ",".join(exclusions))
        python = scheduler.get("python", sys.executable)
        lines += [
            "set -euo pipefail",
            "export OMP_NUM_THREADS=${SLURM_CPUS_PER_TASK:-1}",
            "export OPENBLAS_NUM_THREADS=1",
            "export MKL_NUM_THREADS=1",
            shlex.join(
                [
                    python,
                    "-m",
                    "spires_batch.workflow",
                    "execute-job",
                    str(Path(plan_path).resolve()),
                    name,
                ]
            ),
        ]
        script = directory / f"{index:03d}-{name}.sbatch"
        script.write_text("\n".join(lines) + "\n")
        dependencies = [
            scripts[dependency] for dependency in record["jobs"][name]["depends_on"]
        ]
        dependency = (
            ' --dependency="afterok:'
            + ":".join("${" + variable + "%%;*}" for variable in dependencies)
            + '"'
            if dependencies
            else ""
        )
        variable = f"job_{index}"
        submit.append(
            f"{variable}=$(sbatch --parsable{dependency} {shlex.quote(str(script))})"
        )
        submit.append(
            f'printf "%s\\t%s\\n" {shlex.quote(name)} "${{{variable}}}" >> {shlex.quote(str(directory / "submissions.tsv"))}'
        )
        scripts[name] = variable
    (directory / "scheduler-policy.json").write_text(
        json.dumps(policy, indent=2) + "\n"
    )
    (directory / "submit.sh").write_text("\n".join(submit) + "\n")
    return directory


def main(argv=None):
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
    else:
        execute_job(arguments.plan, arguments.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
