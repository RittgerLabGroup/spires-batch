"""Build or check the complete JSON configuration documentation from live schemas."""

import argparse
import ast
import copy
from dataclasses import asdict
import inspect
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "spires-interpolation"))
sys.path.insert(0, str(ROOT.parent / "spires-io"))
from spires_batch.initialization import Initialization
from spires_batch.models import RequestConfig
from spires_batch.workflow import Workflow


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
            description = descriptions.get(f"{model_name}.{name}", descriptions.get(name))
            if not description:
                raise ValueError(f"Undocumented schema field: {model_name}.{name}")
            field["description"] = description
    return schema


def build_reference():
    daily = json.loads((ROOT / "examples/reference/daily_request.json").read_text())
    annotations = json.loads((ROOT / "examples/reference/annotations.json").read_text())
    interfaces = json.loads((ROOT / "examples/reference/interfaces.json").read_text())
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
    if set(interfaces["nrt_storage"]["fields"]) != set(DEFAULTS) | {"archive_root"}:
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
            "workflow": "spires-batch workflow plan CONFIG --output PLAN; universal schema 2",
            "daily_request": "spires-batch plan CONFIG --output PLAN; daily schema 1",
            "initialization": "spires-batch tile-init CONFIG --dry-run; initialization schema 1",
            "interpolation": "spires-batch interpolation plan CONFIG --output PLAN; interpolation schema 2",
            "nrt_cycle": "spires-batch nrt cycle CONFIG --through DATE --output DIRECTORY [--execute]; requires an allocation and already registered real history",
            "nrt_storage": "See docs/nrt-storage.md for configure/retention commands",
        },
        "schemas": {
            "workflow": annotated_schema(Workflow, descriptions),
            "daily_request": annotated_schema(RequestConfig, descriptions),
            "initialization": annotated_schema(Initialization, descriptions),
        },
        "daily_request_guidance": daily,
        "interfaces": interfaces,
        "fixed_scientific_recipe_not_user_options": asdict(VIIRS_SNOW),
        "safety": [
            "Planning a universal workflow does not download or compute; nrt cycle without --execute DOES acquire, prepare clouds and retrieve before stopping at the revision plan.",
            "JSON has no comments. Do not paste this reference's description/type/default wrappers into a runnable request.",
            "Scientific recipe constants are not user-adjustable JSON options. Only evaluated NOAA-20 viirs_snow interpolation is enabled.",
            "R0 loads every summer scene for a tile/year into RAM; zarr_path, zarr_mode and chunks are rejected.",
            "Full-history NRT replay stops with status 3 and a transfer-size report instead of silently staging all history.",
            "Use exclusive full-node blanca-snow allocations for high-memory R0; other work uses blanca-snow/blanca-rittger and approved preemptable overflow with the verified exclusion list.",
            "Runtime dictionary parsers do not all reject unknown keys. This reference documents consumed keys, not a promise of complete machine validation.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()
    value = json.dumps(build_reference(), indent=2) + "\n"
    target = ROOT / "examples/config_template.json"
    if args.check:
        if not target.exists() or json.loads(target.read_text()) != json.loads(value):
            parser.exit(1, "Configuration reference is stale; regenerate it.\n")
        print("Configuration reference matches live schemas and authored interfaces.")
    elif args.stdout:
        print(value, end="")
    else:
        target.write_text(value)
        print(target)


if __name__ == "__main__":
    main()
