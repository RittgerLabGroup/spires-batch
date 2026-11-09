"""Build or check the JSON and Markdown configuration references from live schemas."""

import argparse
import ast
import copy
from dataclasses import asdict
import inspect
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "scripts/config_reference"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "spires-interpolation"))
sys.path.insert(0, str(ROOT.parent / "spires-io"))
from spires_batch.initialization import Initialization
from spires_batch.models import RequestConfig
from spires_batch.workflow import Workflow
from spires_batch.historical import HistoricalRequest
from spires_batch.configuration import HEADER_FIELDS


def documented_descriptions(value):
    descriptions = {}
    if isinstance(value, dict):
        for name, child in value.items():
            if isinstance(child, dict) and isinstance(child.get("description"), str):
                descriptions[name] = child["description"]
            descriptions.update(documented_descriptions(child))
    elif isinstance(value, list):
        for child in value:
            descriptions.update(documented_descriptions(child))
    return descriptions


def annotated_schema(model, descriptions):
    schema = copy.deepcopy(model.model_json_schema())
    for model_name, definition in [(model.__name__, schema), *schema.get("$defs", {}).items()]:
        for name, field in definition.get("properties", {}).items():
            description = field.get("description") or descriptions.get(f"{model_name}.{name}", descriptions.get(name))
            if not description:
                raise ValueError(f"Undocumented schema field: {model_name}.{name}")
            field["description"] = description
    return schema


def build_reference():
    daily = json.loads((SOURCES / "daily_request.json").read_text())
    annotations = json.loads((SOURCES / "annotations.json").read_text())
    interfaces = json.loads((SOURCES / "interfaces.json").read_text())
    from spires_batch.scheduler_policy import PROFILE_FIELDS, QUEUE_FIELDS, SELECTION_FIELDS

    for name, fields in (("scheduler_job_type", PROFILE_FIELDS), ("scheduler_queue", QUEUE_FIELDS), ("scheduler_selection", SELECTION_FIELDS)):
        if set(interfaces[name]["fields"]) != fields:
            raise ValueError(f"Scheduler policy field documentation drifted: {name}")
    descriptions = {**documented_descriptions(daily), **annotations}
    from spires_io.viirs import prepare_viirs_scene_for_inversion
    from spires_interpolation.algorithm import VIIRS_SNOW
    from spires_interpolation.raster_io import DAILY_FIELDS
    from spires_batch.nrt_storage import DEFAULTS
    from spires_batch.interpolation import plan as interpolation_plan

    sets = {}
    for statement in ast.parse(inspect.getsource(interpolation_plan)).body[0].body:
        if isinstance(statement, ast.Assign) and isinstance(statement.targets[0], ast.Name):
            name = statement.targets[0].id
            if name in {"required", "optional"}:
                sets[name] = ast.literal_eval(statement.value)
    documented = interfaces["interpolation"]["fields"]
    if set(documented) != sets["required"] | sets["optional"]:
        raise ValueError("Interpolation request field documentation drifted from the runtime")
    if {name for name, field in documented.items() if field["required"]} != sets["required"]:
        raise ValueError("Interpolation required/optional field documentation drifted")
    if set(interfaces["prepared_daily"]["fields"]) != set(DAILY_FIELDS):
        raise ValueError("Prepared daily field documentation drifted from the runtime")
    if set(interfaces["nrt_storage"]["fields"]) != set(DEFAULTS) | {"archive_root"} | HEADER_FIELDS:
        raise ValueError("Storage documentation drifted from the runtime")
    for name, default in DEFAULTS.items():
        if interfaces["nrt_storage"]["fields"][name]["default"] != default:
            raise ValueError(f"Storage default changed: {name}")
    preparation = {}
    extra_descriptions = {
        "lut_file": "Optional LUT used to infer band selection; the retained worker explicitly fixes its evaluated bands.",
        "cloud_mask_source": "External cloud source; the retained coordinator overrides this with its matched corrected cloud.",
        "water_mask_source": "Optional external water source path.",
        "ice_mask_source": "Optional external ice source path.",
        "playa_mask_source": "Optional external playa source path.",
        "keep_intermediate_reflectance": "Retain intermediate reflectance arrays if true.",
        "keep_r0_masks": "Retain R0 masks; the retained worker forces true.",
    }
    for name, parameter in inspect.signature(prepare_viirs_scene_for_inversion).parameters.items():
        if name in {"source", "logger"}:
            continue
        description = extra_descriptions.get(name, descriptions.get(name))
        if not description:
            raise ValueError(f"Undocumented retrieval preparation field: {name}")
        preparation[name] = {"description": description, "required": False, "default": parameter.default}
    interfaces["retrieval_preparation"] = {
        "source": "spires_io.viirs.prepare_viirs_scene_for_inversion signature",
        "fields": preparation,
        "notes": ["JSON recipes use path strings for external sources, not Python Dataset objects.", "source and logger are API arguments, not recipe fields. Retained worker forces evaluated bands, keep_r0_masks=true and its cloud_mask_source; cloud_mask_application_stage must be post_inversion."],
    }
    return {
        "document_type": "spipy_configuration_reference",
        "runnable_request": False,
        "description": "JSON docstring/reference, NOT a submission file. Each schema lists every declared field, required fields, defaults, enums and constraints. Dictionaries consumed without Pydantic are documented under interfaces. Descriptions are documentation, not accepted config keys. Omitted fields, explicit null, and empty objects are not universally interchangeable.",
        "regenerate": "python scripts/build_config_reference.py",
        "check": "python scripts/build_config_reference.py --check",
        "entry_points": {
            "workflow": "spires-batch workflow plan CONFIG --output PLAN; config_type=workflow, schema_version=1",
            "daily_request": "spires-batch plan CONFIG --output PLAN; config_type=batch_request, schema_version=1",
            "historical": "spires-batch historical plan CONFIG --output PLAN; config_type=historical, schema_version=1 (inclusive start/stop). render-slurm writes date arrays; run requires an allocation; assemble binds real receipts for interpolation.",
            "initialization": "spires-batch tile-init CONFIG --dry-run; config_type=initialization, schema_version=1",
            "interpolation": "spires-batch interpolation plan CONFIG --output PLAN; config_type=interpolation, schema_version=1 (all calendar dates inclusive)",
            "nrt_cycle": "spires-batch nrt cycle CONFIG --through DATE --output DIRECTORY [--execute]; requires an allocation and already registered real history",
            "nrt_storage": "See docs/reference/storage.md for configure/retention commands",
        },
        "schemas": {
            "workflow": annotated_schema(Workflow, descriptions),
            "daily_request": annotated_schema(RequestConfig, descriptions),
            "initialization": annotated_schema(Initialization, descriptions),
            "historical": annotated_schema(HistoricalRequest, descriptions),
        },
        "daily_request_guidance": daily,
        "interfaces": interfaces,
        "fixed_scientific_recipe_not_user_options": {
            key: value for key, value in asdict(VIIRS_SNOW).items()
            if key != "use_snow_climatology"
        },
        "safety": [
            "All public configurations require config_type and schema_version. All kinds use draft version 1 with inclusive calendar bounds. To preserve an earlier draft historical/interpolation range with an exclusive stop, subtract one day from that stop and regenerate the plan; keep version 1. Unsupported/missing headers are rejected; no implicit migration occurs. Saved-plan/checkpoint formats and scientific component versions are separate contracts.",
            "Planning a universal workflow does not download or compute; nrt cycle without --execute DOES acquire, prepare clouds and retrieve before stopping at the revision plan.",
            "JSON has no comments. Do not paste this reference's description/type/default wrappers into a runnable request.",
            "Scientific recipe constants are not user-adjustable JSON options. NOAA-20 viirs_snow supports the Boolean use_snow_climatology option (default true); disabled mode fixes onset frequency weight at one and removes the rare-snow veto. Switching modes requires replay with compatible state.",
            "R0 loads every summer scene for a tile/year into RAM; zarr_path, zarr_mode and chunks are rejected.",
            "Full-history NRT replay stops with status 3 and a transfer-size report instead of silently staging all history.",
            "Use exclusive full-node blanca-snow allocations for high-memory R0; other work uses blanca-snow/blanca-rittger and approved preemptable overflow with the verified exclusion list.",
            "Runtime dictionary parsers do not all reject unknown keys. This reference documents consumed keys, not a promise of complete machine validation.",
        ],
    }


def markdown_value(value):
    if isinstance(value, str):
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|").replace("\n", "<br>")
    return "`" + json.dumps(value, ensure_ascii=False).replace("|", "\\|") + "`"


def markdown_details(value, path="", level=3):
    lines = []
    if isinstance(value, dict):
        if not value:
            return ["`{}` (empty object)", ""]
        if "description" in value:
            lines.extend([markdown_value(value["description"]), ""])
        simple = {
            name: child for name, child in value.items()
            if name != "description" and not isinstance(child, (dict, list))
        }
        if simple:
            lines.extend(["| Property | Value |", "| --- | --- |"])
            lines.extend(f"| `{name}` | {markdown_value(child)} |" for name, child in simple.items())
            lines.append("")
        for name, child in value.items():
            if name == "description" or name in simple:
                continue
            if name == "fields":
                for field_name, field in child.items():
                    field_path = f"{path}.{field_name}" if path else field_name
                    lines.extend([f"{'#' * min(level, 6)} `{field_path}`", ""])
                    lines.extend(markdown_details(field, field_path, level + 1))
            else:
                child_path = f"{path}.{name}" if path else name
                lines.extend([f"{'#' * min(level, 6)} `{child_path}`", ""])
                lines.extend(markdown_details(child, child_path, level + 1))
    elif isinstance(value, list):
        for index, child in enumerate(value, start=1):
            if isinstance(child, (dict, list)):
                lines.extend([f"{'#' * min(level, 6)} {path}: item {index}", ""])
                lines.extend(markdown_details(child, f"{path}[{index - 1}]", level + 1))
            else:
                lines.append(f"- {markdown_value(child)}")
        if not value:
            lines.append("`[]` (empty list)")
        lines.append("")
    else:
        lines.extend([markdown_value(value), ""])
    return lines


def section_title(name):
    return name.replace("_", " ").capitalize().replace("Nrt", "NRT")


def markdown_reference(title, sections, introduction=""):
    lines = [
        f"# {title}", "",
        "[Reference index](README.md) · [CLI guide](../cli.md)", "",
        "This is field documentation, not a runnable configuration. Field names are",
        "shown exactly as used in configurations. Dotted headings show nested fields;",
        "the section name identifies the interface, not an extra JSON wrapper.", "",
        "Generated by `scripts/build_config_reference.py`; see the index for maintenance.", "",
    ]
    if introduction:
        lines.extend([introduction, ""])
    lines.extend(["## Contents", ""])
    for name in sections:
        lines.append(f"- [{section_title(name)}](#{name.replace('_', '-')})")
    lines.append("")
    for name, value in sections.items():
        lines.extend([f"## {section_title(name)}", ""])
        lines.extend(markdown_details(value, name))
    return "\n".join(lines).rstrip() + "\n"


def build_documents():
    from spires_batch.scheduler_policy import load_policy

    reference = build_reference()
    _, policy = load_policy(ROOT / "examples/scheduler_policy.json")
    resource_lines = [
        "# Standard resource requests", "",
        "[Reference index](README.md) · [Policy guide](../../../examples/scheduler_policy.md)", "",
        "Generated from [scheduler_policy.json](../../../examples/scheduler_policy.json).",
        "Edit that policy and regenerate this page; do not maintain a separate table.",
        "Reservations apply to one task, not an entire array. Memory is total per task.", "",
        "| Job type | Queue | CPUs | Memory | Walltime | Array concurrency | Evidence status |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, profile in policy["job_types"].items():
        resource_lines.append(
            f"| `{name}` | {profile['queue']} | {profile['cpus']} | {profile['memory']} | {profile['time']} | {profile['concurrency']} | {profile.get('status', 'unspecified')} |"
        )
    resource_lines.extend(["", "R0 memory `0` means all node memory with exclusive access. Concurrency is not",
                           "consumed by individual NRT allocations; unified historical/interpolation and batch jobs retain their arrays.", ""])
    resource_lines.extend(["## Available queues", "",
                           "Select a queue explicitly; no automatic overflow is implied. Shared node exclusions apply to all queues.", "",
                           "| Queue | Cluster | Partition | Account | QoS |",
                           "| --- | --- | --- | --- | --- |"])
    for name, queue in policy["queues"].items():
        resource_lines.append(f"| `{name}` | {queue['cluster']} | {queue['partition']} | {queue['account']} | {queue['qos']} |")
    resource_lines.append("")
    for name, profile in policy["job_types"].items():
        resource_lines.extend([f"## `{name}`", "", profile.get("unit", ""), ""])
        resource_lines.extend(f"- {markdown_value(note)}" for note in profile.get("evidence", []))
        resource_lines.append("")
    daily = reference["daily_request_guidance"]
    daily_sections = {
        name: value for name, value in daily.items()
        if name not in {"document_type", "documented_schema_version", "runnable_request", "description"}
    }
    annotations = json.loads((SOURCES / "annotations.json").read_text())
    annotation_lines = [
        "# Configuration field glossary", "",
        "[Reference index](README.md) · [CLI guide](../cli.md)", "",
        "Short descriptions used when documenting the typed configuration schemas.",
        "A model-qualified name distinguishes fields with the same name in different",
        "contexts. This glossary is not a list of top-level configuration keys; see",
        "the field guides for nesting and the complete JSON reference for constraints.", "",
        "Generated by `scripts/build_config_reference.py`; see the index for maintenance.", "",
        "| Field | Meaning |", "| --- | --- |",
    ]
    annotation_lines.extend(
        f"| `{name}` | {markdown_value(description)} |"
        for name, description in sorted(annotations.items())
    )
    return {
        ROOT / "docs/reference/configuration/resources.md": "\n".join(resource_lines).rstrip() + "\n",
        ROOT / "docs/reference/config_reference.json": json.dumps(reference, indent=2) + "\n",
        ROOT / "docs/reference/configuration/annotations.md": "\n".join(annotation_lines) + "\n",
        ROOT / "docs/reference/configuration/daily_request.md": markdown_reference(
            "Batch-request field guide", daily_sections,
            daily["description"] + f"\n\nConfiguration schema version: **{daily['documented_schema_version']}**."
        ),
        ROOT / "docs/reference/configuration/interfaces.md": markdown_reference(
            "Processing interface field guide", reference["interfaces"]
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()
    documents = build_documents()
    if args.check:
        stale = [str(path.relative_to(ROOT)) for path, value in documents.items()
                 if not path.exists() or path.read_text() != value]
        if stale:
            parser.exit(1, "Configuration references are stale; regenerate: " + ", ".join(stale) + "\n")
        print("JSON and Markdown references match live schemas and authored interfaces.")
    elif args.stdout:
        print(documents[ROOT / "docs/reference/config_reference.json"], end="")
    else:
        for target, value in documents.items():
            target.write_text(value)
            print(target)


if __name__ == "__main__":
    main()
