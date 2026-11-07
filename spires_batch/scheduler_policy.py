"""Shared, explicitly selected resource budgets for operational job types."""

import copy
import json
from pathlib import Path
import re

from .configuration import HEADER_FIELDS, require_configuration


RESOURCE_FIELDS = {"cluster", "partition", "account", "qos", "cpus", "memory", "time", "concurrency", "exclusive"}
QUEUE_FIELDS = {"cluster", "partition", "account", "qos"}
PREPARATION_FIELDS = {"retrieval_concurrency", "retrieval_memory_gib", "preparation_memory_gib"}
PROFILE_FIELDS = (RESOURCE_FIELDS - QUEUE_FIELDS) | {"queue", "description", "unit", "status", "evidence", "preparation", "max_tiles"}
SELECTION_FIELDS = RESOURCE_FIELDS | {"policy", "job_type", "queue"}


def validate_resources(resources):
    for name in QUEUE_FIELDS:
        if not isinstance(resources.get(name), str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", resources[name]):
            raise ValueError(f"Invalid scheduler {name}; select one named queue")
    for name in ("cpus", "concurrency"):
        if type(resources.get(name)) is not int or resources[name] < 1:
            raise ValueError(f"Scheduler {name} must be a positive integer")
    if type(resources.get("exclusive")) is not bool:
        raise ValueError("Scheduler exclusive must be a boolean")
    memory = resources.get("memory")
    if not isinstance(memory, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*[KMGT]?)", memory):
        raise ValueError("Scheduler memory must be a Slurm memory size")
    if memory == "0" and not resources["exclusive"]:
        raise ValueError("All-node memory requires exclusive=true")
    if not isinstance(resources.get("time"), str) or not re.fullmatch(r"(?:[0-9]+-)?[0-9]+(?::[0-5][0-9]){0,2}", resources["time"]):
        raise ValueError("Invalid scheduler walltime")
    return resources


def profile_resources(policy, job_type, *, queue=None, overrides=None):
    profiles = policy.get("job_types", {})
    if job_type not in profiles:
        raise ValueError(f"Unknown scheduler job_type: {job_type!r}")
    profile = profiles[job_type]
    queue_name = queue if queue is not None else profile["queue"]
    if queue_name not in policy.get("queues", {}):
        raise ValueError(f"Unknown scheduler queue: {queue_name!r}")
    overrides = overrides or {}
    if set(overrides) - RESOURCE_FIELDS:
        raise ValueError("Unknown scheduler resource override")
    result = {
        "exclusive": False,
        **policy["queues"][queue_name],
        **{name: value for name, value in profile.items() if name in RESOURCE_FIELDS},
        **overrides,
    }
    return validate_resources(result)


def load_policy(path, base="."):
    path = (Path(base) / Path(path).expanduser()).resolve()
    policy = json.loads(path.read_text())
    return str(path), validate_policy(policy)


def validate_policy(policy):
    require_configuration(policy, "scheduler_policy")
    if set(policy) - HEADER_FIELDS - {"exclude_nodes", "queues", "job_types"}:
        raise ValueError("Unknown scheduler policy field")
    exclusions = policy.get("exclude_nodes")
    if not isinstance(exclusions, list) or any(not isinstance(node, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", node) for node in exclusions):
        raise ValueError("Scheduler policy requires valid exclude_nodes")
    queues = policy.get("queues", {})
    profiles = policy.get("job_types", {})
    if not isinstance(queues, dict) or not isinstance(profiles, dict):
        raise ValueError("Policy queues and job_types must be objects")
    for name, queue in queues.items():
        if not isinstance(queue, dict) or set(queue) != QUEUE_FIELDS:
            raise ValueError(f"Queue {name!r} requires cluster, partition, account and qos")
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value) for value in queue.values()):
            raise ValueError(f"Invalid queue values in {name!r}")
    for name, profile in profiles.items():
        if not isinstance(profile, dict) or set(profile) - PROFILE_FIELDS:
            raise ValueError(f"Invalid job profile {name!r}")
        if "queue" not in profile:
            raise ValueError(f"Profile {name!r} requires a queue")
        preparation = profile.get("preparation", {})
        if not isinstance(preparation, dict) or set(preparation) - PREPARATION_FIELDS:
            raise ValueError(f"Invalid preparation defaults in {name!r}")
        if any(type(value) is not int or value < 1 for value in preparation.values()):
            raise ValueError("Preparation budgets must be positive integers")
        if "preparation_memory_gib" in preparation and "retrieval_memory_gib" in preparation:
            raise ValueError("Choose total or per-tile memory in a preparation profile")
        if "max_tiles" in profile and (type(profile["max_tiles"]) is not int or profile["max_tiles"] < 1):
            raise ValueError("max_tiles must be a positive integer")
        profile_resources(policy, name)
    return policy


def resolve_scheduler(selection, base="."):
    selection = copy.deepcopy(selection)
    allowed = SELECTION_FIELDS | {"snapshot"}
    if set(selection) - allowed or "policy" not in selection:
        raise ValueError("Scheduler requires a policy and supported profile/resource fields")
    if "snapshot" in selection:
        path = str((Path(base) / Path(selection["policy"]).expanduser()).resolve())
        policy = validate_policy(selection["snapshot"])
    else:
        path, policy = load_policy(selection["policy"], base)
    overrides = {name: value for name, value in selection.items() if name in RESOURCE_FIELDS}
    job_type = selection.get("job_type")
    if job_type is not None:
        resources = profile_resources(policy, job_type, queue=selection.get("queue"), overrides=overrides)
    else:
        if "queue" in selection:
            raise ValueError("queue requires a job_type")
        resources = validate_resources({"cluster": "blanca", "qos": selection.get("account"), "exclusive": False, **overrides})
    return {**selection, **resources, "policy": path, "snapshot": policy}


def resolve_interpolation_scheduler(selection, base="."):
    selection = copy.deepcopy(selection)
    stages = selection.pop("stages", {})
    names = {"screen", "spatial", "properties", "export"}
    if not isinstance(stages, dict) or set(stages) - names:
        raise ValueError("Unknown interpolation scheduler stage")
    resolved = resolve_scheduler(selection, base)
    resolved["stages"] = {}
    explicit = {name: value for name, value in selection.items() if name in RESOURCE_FIELDS}
    for stage in sorted(names):
        override = stages.get(stage, {})
        if not isinstance(override, dict) or set(override) - RESOURCE_FIELDS - {"job_type", "queue"}:
            raise ValueError("Invalid interpolation stage resource override")
        job_type = override.get("job_type", selection.get("job_type"))
        if job_type:
            resources = profile_resources(
                resolved["snapshot"], job_type,
                queue=override.get("queue", selection.get("queue")),
                overrides={**explicit, **{name: value for name, value in override.items() if name in RESOURCE_FIELDS}},
            )
        else:
            if "queue" in override:
                raise ValueError("Stage queue requires a job_type")
            resources = validate_resources({**{name: resolved[name] for name in RESOURCE_FIELDS}, **override})
        if resources["memory"] == "0":
            raise ValueError("Interpolation requires a bounded memory request")
        if resources["cluster"] != resolved["cluster"]:
            raise ValueError("Dependent interpolation stages must use the same cluster")
        resolved["stages"][stage] = {**override, **resources}
    return resolved


def preparation_defaults(preparation, scheduler):
    profile = scheduler["snapshot"].get("job_types", {}).get(scheduler.get("job_type"), {})
    if "max_tiles" in profile and len(preparation["tiles"]) > profile["max_tiles"]:
        raise ValueError("Tile domain exceeds the selected resource profile; choose or define a larger profile")
    defaults = dict(profile.get("preparation", {}))
    if "retrieval_memory_gib" in preparation:
        defaults.pop("preparation_memory_gib", None)
    if "preparation_memory_gib" in preparation:
        defaults.pop("retrieval_memory_gib", None)
    result = {**defaults, **preparation}
    tile_count = len(result["tiles"])
    concurrency = result.get("retrieval_concurrency", tile_count)
    per_tile = result.get("retrieval_memory_gib", 6)
    for value in (concurrency, per_tile):
        if type(value) is not int or value < 1:
            raise ValueError("Preparation concurrency and memory must be positive integers")
    total = result.get("preparation_memory_gib", min(concurrency, tile_count) * per_tile + 2)
    if type(total) is not int or total < 1:
        raise ValueError("Total preparation memory must be a positive integer")
    if "preparation_memory_gib" in result and "retrieval_memory_gib" in result:
        raise ValueError("Choose total or per-tile preparation memory, not both")
    memory = re.fullmatch(r"(\d+)([KMGT]?)", scheduler["memory"])
    units = {"K": 1024, "M": 1024**2, "": 1024**2, "G": 1024**3, "T": 1024**4}
    if scheduler["cpus"] < min(concurrency, tile_count) or (scheduler["memory"] != "0" and int(memory[1]) * units[memory[2]] < total * 1024**3):
        raise ValueError("Scheduler CPUs/memory are below the preparation budget")
    return result


def resolve_cycle_policy(configuration, base="."):
    configuration = copy.deepcopy(configuration)
    if "scheduler" in configuration:
        configuration["scheduler"] = resolve_scheduler(configuration["scheduler"], base)
        configuration = preparation_defaults(configuration, configuration["scheduler"])
    return configuration


def resolve_batch_request(request, base=".", *, policy_snapshot=None):
    from .models import RequestConfig

    execution = request.execution.model_dump(mode="json", exclude_unset=True)
    if execution.get("policy_snapshot") is not None:
        validate_policy(execution["policy_snapshot"])
        return request
    if not execution.get("policy"):
        if execution.get("job_type") or execution.get("queue"):
            raise ValueError("execution.job_type/queue require execution.policy")
        return request
    if execution.get("profiles") or not execution.get("job_type"):
        raise ValueError("A policy-based batch request requires one job_type, not inline profile routing")
    selection = {name: execution[name] for name in ("policy", "job_type", "queue") if execution.get(name) is not None}
    if policy_snapshot is not None:
        selection["snapshot"] = policy_snapshot
    scheduler = resolve_scheduler(selection, base)
    resources = {name: scheduler[name] for name in QUEUE_FIELDS}
    resources.update(cpus_per_task=scheduler["cpus"], memory=scheduler["memory"], time_limit=scheduler["time"])
    overrides = execution.get("resources", {})
    directives = ["--nodes=1"]
    if scheduler["exclusive"]:
        directives.append("--exclusive")
    if scheduler["snapshot"]["exclude_nodes"]:
        directives.append("--exclude=" + ",".join(scheduler["snapshot"]["exclude_nodes"]))
    if any(item.startswith(("--exclude", "--exclusive", "--nodes")) for item in overrides.get("extra_directives", [])):
        raise ValueError("Set exclusions/exclusive allocation in the shared policy, not extra_directives")
    resources.update(overrides)
    resources["extra_directives"] = directives + list(overrides.get("extra_directives", []))
    validate_resources({**scheduler, "memory": resources["memory"], "cpus": resources["cpus_per_task"], "time": resources["time_limit"], **{name: resources[name] for name in QUEUE_FIELDS}})
    execution.update(policy=scheduler["policy"], policy_snapshot=scheduler["snapshot"], resources=resources)
    execution.setdefault("profile", scheduler["partition"] if scheduler["partition"] in {"blanca-snow", "blanca-rittger"} else "blanca-snow")
    execution.setdefault("max_concurrent_tasks", scheduler["concurrency"])
    value = request.model_dump(mode="json")
    value["execution"] = execution
    return RequestConfig.model_validate(value)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Preview shared resource defaults without submitting or processing.")
    parser.add_argument("policy")
    parser.add_argument("--job-type")
    parser.add_argument("--queue")
    arguments = parser.parse_args(argv)
    path, policy = load_policy(arguments.policy)
    names = [arguments.job_type] if arguments.job_type else list(policy.get("job_types", {}))
    resolved = {}
    for name in names:
        resources = profile_resources(policy, name, queue=arguments.queue)
        resolved[name] = {**policy["job_types"][name], "queue": arguments.queue or policy["job_types"][name]["queue"], "resources": resources}
    print(json.dumps(dict(policy=path, exclude_nodes=policy["exclude_nodes"], job_types=resolved), indent=2))
    return 0
