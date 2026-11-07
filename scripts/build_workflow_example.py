"""Build the self-contained workflow example from the maintained task examples."""

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def read_example(name):
    return json.loads((EXAMPLES / f"{name}.json").read_text())


def build_example():
    requests = {
        name: read_example(name)
        for name in ("initialization", "r0", "historical", "interpolation", "cycle", "storage")
    }
    historical = requests["historical"]
    initialization = requests["initialization"]
    variables = {
        "scratch": initialization["storage"]["scratch_root"],
        "science_python": historical["preparation"]["science_python"],
        "cloud_python": historical["preparation"]["cloud_python"],
    }
    for request in requests.values():
        for section in ("scheduler", "execution"):
            if section in request and "policy" in request[section]:
                if request[section].pop("policy") != "scheduler_policy.json":
                    raise ValueError("Examples must use the shared scheduler_policy.json")
    initialization["stage_assets"] = True
    configuration = {
        "config_type": "workflow",
        "schema_version": 1,
        "mode": "nrt",
        "sensor": initialization["sensor"],
        "platform": initialization["platform"],
        "selection": {"tiles": initialization["tiles"]},
        "output": str(Path(historical["output"]).parent),
        "log_root": str(Path(historical["output"]).parent / "logs"),
        "max_auto_retry_count": 3,
        "scheduler": {
            "policy": read_example("scheduler_policy"),
            "python": variables["science_python"],
        },
        "coordinator": {
            "cluster": "blanca",
            "partition": "blanca-snow",
            "account": "blanca-snow",
            "qos": "blanca-snow",
            "cpus": 1,
            "memory": "4G",
            "time": "01:00:00",
        },
        "jobs": {
            "initialize": {
                "kind": "initialize",
                "resources": {"memory": "8G", "time": "02:00:00"},
                "request": initialization,
            },
            "background": {
                "kind": "daily",
                "depends_on": ["initialize"],
                "job_type": "r0",
                "request": requests["r0"],
            },
            "historical": {
                "kind": "historical",
                "depends_on": ["background"],
                "job_type": "daily_five_tiles",
                "request": historical,
            },
            "interpolate": {
                "kind": "interpolation",
                "depends_on": ["historical"],
                "historical_job": "historical",
                "request": requests["interpolation"],
            },
            "register": {
                "kind": "register",
                "depends_on": ["interpolate"],
                "resources": {"memory": "16G", "time": "12:00:00"},
                "request": {
                    "interpolation_job": "interpolate",
                    "stream_root": requests["cycle"]["stream_root"],
                    "storage": requests["storage"],
                },
            },
            "continue_nrt": {
                "kind": "nrt",
                "depends_on": ["register"],
                "job_type": "nrt_five_tiles",
                "request": requests["cycle"],
            },
        },
    }

    def parameterize(value):
        if isinstance(value, dict):
            return {name: parameterize(child) for name, child in value.items()}
        if isinstance(value, list):
            return [parameterize(child) for child in value]
        if isinstance(value, str):
            for name, path in variables.items():
                if value == path or value.startswith(path.rstrip("/") + "/"):
                    return "${" + name + "}" + value[len(path):]
        return value

    result = parameterize(configuration)
    result["variables"] = variables
    return json.dumps(result, indent=2) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--stdout", action="store_true")
    arguments = parser.parse_args()
    document = build_example()
    destination = EXAMPLES / "workflow.json"
    if arguments.check:
        if not destination.exists() or destination.read_text() != document:
            parser.exit(1, "Workflow example is stale; run scripts/build_workflow_example.py\n")
        print("Unified example matches the task configurations and shared policy.")
    elif arguments.stdout:
        print(document, end="")
    else:
        destination.write_text(document)
        print(destination)


if __name__ == "__main__":
    main()
