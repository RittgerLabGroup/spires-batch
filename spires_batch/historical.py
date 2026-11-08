"""JSON-driven historical preparation, daily albedo and receipt assembly.

Uses the same matched cloud/retrieval coordinator as NRT. Planning and rendering
never transfer reflectance, submit jobs, or execute science. Execution belongs
in a compute allocation and stages only one date's explicitly selected sources.
"""

import argparse
from datetime import date, timedelta
import fcntl
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .configuration import PublicConfiguration, require_configuration
from .interpolation import digest, file_record, mapped_fields
from .nrt import day_context, source_identity, source_record
from .workflow import Resources, resolve_paths
from .scheduler_policy import preparation_defaults, resolve_scheduler


class HistoricalScheduler(Resources):
    partition: str = Field(default="blanca-rittger", pattern=r"^[A-Za-z0-9_.-]+$")
    account: str = Field(default="blanca-rittger", pattern=r"^[A-Za-z0-9_.-]+$")
    cluster: Literal["blanca"] = Field(default="blanca", description="Slurm cluster.")
    qos: str = Field(default="blanca-rittger", pattern=r"^[A-Za-z0-9_.-]+$", description="Authorized queue QoS.")
    concurrency: int = Field(default=4, ge=1, strict=True, description="Maximum concurrent date tasks; chunks run in dependency order.")
    policy: str = Field(description="Shared scheduler-policy JSON with exclusions and named job types.")
    job_type: str | None = Field(default=None, description="Named historical resource profile in the policy; resolved before budget validation.")
    queue: str | None = Field(default=None, description="Optional named queue override; sets cluster, partition, account and QoS together.")
    exclusive: bool = Field(default=False, description="Request an exclusive node when true.")
    snapshot: dict | None = Field(default=None, description="Policy snapshot generated during planning; do not author this field.")


class HistoricalRequest(PublicConfiguration):
    model_config = ConfigDict(extra="forbid")
    config_type: Literal["historical"]
    start: date = Field(description="First date to prepare, inclusive.")
    stop: date = Field(description="Last date to prepare, inclusive; a WY ends on September 30.")
    reflectance_root: str = Field(description="Shared archive input directory containing tile/year/VJ109GA.A*.h5.")
    missing_reflectance: Literal["error", "allow"] = Field(default="error", description="error stops planning when any tile/date has no local granule; allow records that planned gap as an unknown tile for interpolation and NRT history. Does not permit corrupt files, duplicate vintages, inaccessible archives or loss of a source after planning.")
    output: str = Field(description="New per-user scratch run directory; never use another operator's outputs.")
    preparation: dict = Field(description="Matched pipeline recipe: science_python, cloud_python, cloud_model, collection, observation_policy, concurrency/memory settings and explicit tiles[tile][WY] retrieval bindings; same interface as NRT cycle preparation.")
    terrain: dict = Field(description="Per-tile mapped dem, slope and aspect fields for daily terrain albedo; DEM metres, angles degrees, matching native grid.")
    albedo_lut: str = Field(description="Existing albedo LUT; daily albedo uses measured grain/dust, without temporal or spatial filling.")
    scheduler: HistoricalScheduler = Field(description="Date-array resources; cpus and memory include the cloud coordinator and concurrent single-worker tile retrievals.")

    @model_validator(mode="after")
    def validate_domain(self):
        if self.stop < self.start:
            raise ValueError("stop must be on or after start")
        required = {"science_python", "cloud_python", "cloud_model", "collection", "observation_policy", "tiles"}
        allowed = required | {"retrieval_concurrency", "retrieval_memory_gib", "preparation_memory_gib"}
        if required - self.preparation.keys() or self.preparation.keys() - allowed:
            raise ValueError("Invalid preparation fields; see the configuration reference")
        tiles = self.preparation["tiles"]
        if not tiles or set(tiles) != set(self.terrain):
            raise ValueError("Preparation and terrain must cover the same nonempty tile domain")
        for tile in tiles:
            if not re.fullmatch(r"h\d{2}v\d{2}", tile):
                raise ValueError("Invalid tile name")
            if set(self.terrain[tile]) != {"dem", "slope", "aspect"}:
                raise ValueError("Each terrain binding requires dem, slope and aspect")
        if not re.fullmatch(r"\d{3}", self.preparation["collection"]):
            raise ValueError("collection must be a three-digit string")
        concurrency = self.preparation.get("retrieval_concurrency", len(tiles))
        per_tile = self.preparation.get("retrieval_memory_gib", 6)
        for value in (concurrency, per_tile):
            if type(value) is not int or value < 1:
                raise ValueError("Preparation concurrency and memory must be positive integers")
        total = self.preparation.get("preparation_memory_gib", min(concurrency, len(tiles)) * per_tile + 2)
        if type(total) is not int or total < 1:
            raise ValueError("Total preparation memory must be a positive integer")
        if "preparation_memory_gib" in self.preparation and "retrieval_memory_gib" in self.preparation:
            raise ValueError("Choose total or per-tile preparation memory, not both")
        memory = re.fullmatch(r"(\d+)([KMGT]?)", self.scheduler.memory)
        units = {"K": 1024, "M": 1024**2, "": 1024**2, "G": 1024**3, "T": 1024**4}
        if self.scheduler.cpus < min(concurrency, len(tiles)) or int(memory[1]) * units[memory[2]] < total * 1024**3:
            raise ValueError("Scheduler CPUs/memory are below the preparation budget")
        for day in days(self.start, self.stop):
            year = str(int(day[:4]) + (int(day[5:7]) >= 10))
            if any(year not in years for years in tiles.values()):
                raise ValueError("Every tile needs explicit retrieval assets for each selected WY")
        return self


def days(start, stop):
    """Yield calendar dates including both endpoints; equal dates select one day."""
    current = start
    while current <= stop:
        yield current.isoformat()
        if current == stop:
            break
        current += timedelta(days=1)


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(json.dumps(value, indent=2, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def read_request(path):
    path = Path(path).resolve()
    value = json.loads(path.read_text())
    require_configuration(value, "historical")
    value["scheduler"] = resolve_scheduler(value["scheduler"], path.parent)
    value["preparation"] = preparation_defaults(value["preparation"], value["scheduler"])
    request = HistoricalRequest.model_validate(value).model_dump(mode="json")
    request = resolve_paths(request, path.parent)
    root = Path(request["reflectance_root"]).expanduser()
    request["reflectance_root"] = str((path.parent / root).resolve())
    return request


def check_archive(root):
    """Reject an unavailable archive instead of interpreting it as missing scenes."""
    with os.scandir(root):
        pass


def source_matches(request, tile, day):
    """Find local vintages without hiding directory permission or I/O failures."""
    parsed = date.fromisoformat(day)
    pattern = f"VJ109GA.A{parsed:%Y%j}.{tile}.{request['preparation']['collection']}.*.h5"
    directory = Path(request["reflectance_root"]) / tile / str(parsed.year)
    try:
        matches = sorted(path for path in directory.iterdir() if fnmatchcase(path.name, pattern))
    except FileNotFoundError:
        matches = []
    return matches, str(directory / pattern)


def plan(config, destination):
    request = read_request(config)
    preparation = request["preparation"]
    selected = list(days(date.fromisoformat(request["start"]), date.fromisoformat(request["stop"])))
    inventory = {}
    missing_sources = {}
    check_archive(request["reflectance_root"])
    for tile in sorted(preparation["tiles"]):
        for day in selected:
            matches, pattern = source_matches(request, tile, day)
            key = tile + "/" + day
            if not matches:
                missing_sources[key] = dict(reason="not_found_in_archive", pattern=pattern)
                inventory[key] = None
            elif len(matches) > 1:
                raise ValueError(f"Expected one downloaded standard granule for {key}, found {len(matches)}; resolve duplicate vintages before planning")
            else:
                inventory[key] = source_record(matches[0])
    if missing_sources and request["missing_reflectance"] == "error":
        raise ValueError(
            "Missing local reflectance for " + ", ".join(missing_sources)
            + "; diagnose/download the missing granules or explicitly set missing_reflectance='allow' to process these tile/dates as unknown observations"
        )
    policy = request["scheduler"]["snapshot"]
    require_configuration(policy, "scheduler_policy")
    exclusions = policy.get("exclude_nodes")
    if not isinstance(exclusions, list) or not exclusions or any(not isinstance(node, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", node) for node in exclusions):
        raise ValueError("An explicit nonempty approved node-exclusion list is required")
    paths = {field["path"] for field in mapped_fields(request)}
    paths.update([request["albedo_lut"], preparation["cloud_model"]])
    for years in preparation["tiles"].values():
        for recipe in years.values():
            paths.add(recipe["lut"])
            paths.update(value for key, value in recipe.get("preparation", {}).items() if key.endswith("_source") and isinstance(value, str))
    manifest_path = Path(preparation["cloud_model"])
    manifest = json.loads(manifest_path.read_text())
    for stage in ("stage1", "stage2"):
        paths.add(str((manifest_path.parent / manifest[stage]["path"]).resolve()))
    assets = {path: file_record(path) for path in sorted(paths)}
    manifest_sha = file_record(manifest_path, checksum=True)["sha256"]
    if preparation["observation_policy"].get("cloud_model_sha256") != manifest_sha:
        raise ValueError("Cloud manifest identity differs from observation_policy")
    for name in ("science_python", "cloud_python"):
        if not os.access(preparation[name], os.X_OK):
            raise ValueError(f"Interpreter is not executable: {preparation[name]}")
    record = dict(request=request, days=selected, inventory=inventory, missing_sources=missing_sources, assets=assets, exclude_nodes=exclusions)
    record["identity"] = digest(record)
    write_new(destination, record)
    return dict(dates=len(selected), tiles=len(preparation["tiles"]), source_bytes=sum(source["bytes"] for source in inventory.values() if source is not None), missing_reflectance=request["missing_reflectance"], missing_count=len(missing_sources), missing_sources=missing_sources, history_transferred=False)


def load_plan(path):
    record = json.loads(Path(path).read_text())
    if record["identity"] != digest({key: value for key, value in record.items() if key != "identity"}):
        raise ValueError("Historical plan changed; create a new plan")
    return record


def receipt_inventory(result, tiles, plan=None):
    """Validate same-day lineage and require plan authorization for null observations."""
    day = result["date"]
    receipts = result["receipts"]
    if sorted(item["tile"] for item in receipts) != sorted(tiles):
        raise ValueError("Historical receipt does not cover the full tile domain")
    if plan is not None and (
        result["identity"] != plan["identity"]
        or day not in plan["days"]
        or set(tiles) != set(plan["request"]["preparation"]["tiles"])
    ):
        raise ValueError("Historical completion receipt belongs to a different plan/date/domain")
    inventory = {}
    for item in receipts:
        if item["date"] != day:
            raise ValueError("Historical receipt date differs from its observations")
        key = item["tile"] + "/" + day
        if item["prepared"] is None:
            if (
                plan is None
                or plan["request"].get("missing_reflectance", "error") != "allow"
                or plan["inventory"][key] is not None
                or plan.get("missing_sources", {}).get(key, {}).get("reason") != "not_found_in_archive"
                or item.get("missing_reason") != "not_found_in_archive"
                or item["daily_albedo"] is not None
            ):
                raise ValueError("Missing observation was not explicitly authorized by the historical plan: " + key)
            inventory[key] = None
        else:
            if plan is not None and source_identity(item) != source_identity(plan["inventory"][key]):
                raise ValueError("Historical observation source differs from its plan: " + key)
            inventory[key] = item
    context = day_context(inventory, tiles, day)
    if any(item["prepared_context"] != context for item in receipts):
        raise ValueError("Prepared same-day lineage differs")
    return inventory


def render(plan_path, directory):
    record = load_plan(plan_path)
    request = record["request"]
    scheduler = request["scheduler"]
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    python = shlex.quote(request["preparation"]["science_python"])
    command = f"{python} -m spires_batch.historical run {shlex.quote(str(Path(plan_path).resolve()))} --index \"$SLURM_ARRAY_TASK_ID\""
    submissions = ["#!/bin/bash", "set -euo pipefail", 'cd "$(dirname "$0")"', 'test ! -e submissions.tsv || { echo "Submission ledger already exists" >&2; exit 1; }', ": > submissions.tsv", 'parent=""']
    for start in range(0, len(record["days"]), 400):
        stop = min(start + 400, len(record["days"])) - 1
        name = f"historical-{start:04d}.sbatch"
        directives = dict(clusters=scheduler["cluster"], partition=scheduler["partition"], account=scheduler["account"], qos=scheduler["qos"], nodes=1, ntasks=1)
        directives.update({"cpus-per-task": scheduler["cpus"], "mem": scheduler["memory"], "time": scheduler["time"], "array": f"{start}-{stop}%{scheduler['concurrency']}", "exclude": ",".join(record["exclude_nodes"]), "output": "historical-%A_%a.out", "error": "historical-%A_%a.err"})
        if scheduler.get("exclusive"):
            directives["exclusive"] = None
        lines = ["#!/bin/bash", *[f"#SBATCH --{key}" if value is None else f"#SBATCH --{key}={value}" for key, value in directives.items()], "set -euo pipefail", "unset PYTHONPATH", "export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 PYTHONUNBUFFERED=1", command]
        (directory / name).write_text("\n".join(lines) + "\n")
        submissions.extend(['dependency=()', 'if [[ -n "$parent" ]]; then dependency=("--dependency=afterok:$parent"); fi', f'job=$(sbatch --parsable "${{dependency[@]}}" {name})', 'parent=${job%%;*}', f'printf "%s\\t%s\\n" "$parent" {name} >> submissions.tsv'])
    (directory / "submit.sh").write_text("\n".join(submissions) + "\n")


def daily_albedo(receipt, terrain, lookup, destination, identity):
    import h5py
    import numpy as np
    from spires_interpolation.albedo import FLAG_MASKS, FLAG_MEANINGS, prepare_geometry
    from spires_interpolation.raster_io import atomic_raster, read_mapped_fields

    source = receipt["prepared"]["state"]["path"]
    with h5py.File(source) as prepared, atomic_raster(destination, identity) as output:
        if output is None:
            return
        if not prepared.attrs.get("complete"):
            raise ValueError("Prepared observations are incomplete")
        shape = prepared["state"].shape
        names = ("albedo", "albedo_terrain", "albedo_flags", "albedo_terrain_flags")
        for name in names:
            output.create_dataset(name, shape, dtype="u2" if name.endswith("flags") else "f4", chunks=(min(20, shape[0]), min(240, shape[1])), compression="gzip", compression_opts=2, shuffle=True)
            if name.endswith("flags"):
                output[name].attrs.update(flag_masks=FLAG_MASKS, flag_meanings=FLAG_MEANINGS)
            else:
                output[name].attrs["units"] = "1"
        for start in range(0, shape[0], 20):
            window = (slice(start, min(start + 20, shape[0])), slice(0, shape[1]))
            context = read_mapped_fields(terrain, window)
            geometry = prepare_geometry(prepared["solar_zenith"][window], prepared["solar_azimuth"][window], context["slope"], context["aspect"], context["dem"])
            eligible = (prepared["state"][window] == 2) & (prepared["screening_flags"][window] == 0)
            grain = np.where(eligible, prepared["grain_radius"][window], np.nan)
            dust = np.where(eligible, prepared["dust"][window], np.nan)
            for name, values in zip(names, lookup.calculate_issues(grain[..., None], dust[..., None], geometry)):
                output[name][window] = values[..., 0]
        for axis in ("x", "y"):
            output.create_dataset(axis, data=prepared[axis][:])
        output.attrs.update(tile=receipt["tile"], date=receipt["date"], prepared_source=source, product="observed_snow_albedo_without_gap_filling")


def run(plan_path, index):
    if not os.environ.get("SLURM_JOB_ID"):
        raise RuntimeError("Historical processing requires a compute allocation")
    from .models import StagingVerification
    from .pipeline import prepare_date, retrieval_concurrency
    from .staging import StagingAction, execute_actions
    from spires_interpolation.albedo import AlbedoLookup

    record = load_plan(plan_path)
    if not 0 <= index < len(record["days"]):
        raise ValueError("Date index outside plan")
    request = record["request"]
    day = record["days"][index]
    tiles = sorted(request["preparation"]["tiles"])
    available_tiles = [tile for tile in tiles if record["inventory"][tile + "/" + day] is not None]
    retrieval_concurrency(request["preparation"], len(available_tiles))
    for path, expected in record["assets"].items():
        if file_record(path) != expected:
            raise ValueError("Asset changed since planning: " + path)
    directory = Path(request["output"]) / "daily" / day
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "prepared-receipts.json"
    with (directory / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if destination.exists():
            from .workflow_execution import _verify_historical

            _verify_historical(plan_path, [index])
            print(f"Verified completed date {day}; reusing its observations", flush=True)
            return
        check_archive(request["reflectance_root"])
        actions = []
        for tile in tiles:
            original = record["inventory"][tile + "/" + day]
            if original is None:
                if request.get("missing_reflectance", "error") != "allow":
                    raise ValueError("Missing source requires explicit permission")
                if source_matches(request, tile, day)[0]:
                    raise ValueError(f"Source appeared since planning: {tile}/{day}; create a new plan")
                continue
            if source_record(original["path"]) != original:
                raise ValueError("Source changed since planning: " + original["path"])
            target = Path(request["output"]) / "input-cache" / day / Path(original["path"]).name
            actions.append(StagingAction(Path(original["path"]), target, original["bytes"], StagingVerification.STAT, True, original["mtime_ns"]))
        missing_tiles = sorted(set(tiles) - set(available_tiles))
        print(json.dumps(dict(date=day, transfer_bytes=sum(action.size_bytes for action in actions), missing_tiles=missing_tiles, missing_reflectance=request.get("missing_reflectance", "error"), history_transferred=False)), flush=True)
        inventory = {tile + "/" + day: None for tile in tiles}
        for tile, result in zip(available_tiles, execute_actions(tuple(actions))):
            key = tile + "/" + day
            inventory[key] = dict(source_record(result.action.destination_path), origin=record["inventory"][key])
        receipts = prepare_date(request["preparation"], inventory, day, directory) if available_tiles else []
        if sorted(receipt["tile"] for receipt in receipts) != available_tiles:
            raise ValueError("Incomplete prepared domain")
        lookup = AlbedoLookup.from_netcdf(request["albedo_lut"]) if receipts else None
        for receipt in receipts:
            from .workflow_logging import serial_task_log

            albedo = directory / (receipt["tile"] + "_albedo.h5")
            with serial_task_log(directory, "albedo", tile=receipt["tile"], day=day):
                daily_albedo(receipt, request["terrain"][receipt["tile"]], lookup, albedo, record["identity"])
            receipt["daily_albedo"] = file_record(albedo)
        context = day_context(inventory, tiles, day)
        receipts.extend(dict(tile=tile, date=day, prepared=None, daily_albedo=None, prepared_context=context, missing_reason="not_found_in_archive") for tile in missing_tiles)
        result = dict(date=day, identity=record["identity"], historical_plan=str(Path(plan_path).resolve()), receipts=sorted(receipts, key=lambda receipt: receipt["tile"]))
        receipt_inventory(result, tiles, record)
        write_new(destination, result)


def assemble(template_path, receipt_root, directory):
    import h5py
    from spires_interpolation.raster_io import DAILY_FIELDS

    template_path = Path(template_path).resolve()
    request = resolve_paths(json.loads(template_path.read_text()), template_path.parent)
    require_configuration(request, "interpolation")
    selected = list(days(date.fromisoformat(request["history_start"]), date.fromisoformat(request["available_through"])))
    tiles = sorted(request["tiles"])
    inventory = {}
    plans = {}
    for tile in tiles:
        request["tiles"][tile]["daily"] = {}
    for day in selected:
        path = Path(receipt_root) / "daily" / day / "prepared-receipts.json"
        result = json.loads(path.read_text())
        receipts = result["receipts"]
        if result["date"] != day:
            raise ValueError("Receipt date or domain differs from interpolation request")
        plan_path = result.get("historical_plan")
        if plan_path is not None and plan_path not in plans:
            plans[plan_path] = load_plan(plan_path)
        inventory.update(receipt_inventory(result, tiles, plans.get(plan_path)))
        for receipt in receipts:
            tile = receipt["tile"]
            if receipt["prepared"] is None:
                request["tiles"][tile]["daily"][day] = None
                continue
            if set(receipt["prepared"]) != set(DAILY_FIELDS):
                raise ValueError("Incorrect prepared field mappings")
            with h5py.File(receipt["prepared"]["state"]["path"]) as prepared:
                if not prepared.attrs.get("complete") or prepared.attrs.get("tile") != tile or prepared.attrs.get("date") != day:
                    raise ValueError("Incomplete or incorrect prepared file")
                if prepared.attrs.get("prepared_context") != receipt["prepared_context"]:
                    raise ValueError("Prepared file and receipt lineage differ")
                for name, binding in receipt["prepared"].items():
                    if binding["path"] != receipt["prepared"]["state"]["path"] or binding["dataset"] != name:
                        raise ValueError("Expected native coordinator field bindings")
                    if tuple(prepared[name].shape) != tuple(request["shape"]):
                        raise ValueError("Prepared grid shape differs from interpolation request")
            request["tiles"][tile]["daily"][day] = receipt["prepared"]
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    write_new(directory / "interpolation.json", request)
    write_new(directory / "inventory.json", inventory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "render-slurm", "run"):
        command = commands.add_parser(name)
        command.add_argument("source")
        if name == "run":
            command.add_argument("--index", type=int, required=True)
        else:
            command.add_argument("--output", required=True)
    command = commands.add_parser("assemble")
    command.add_argument("template")
    command.add_argument("--receipts", required=True)
    command.add_argument("--output", required=True)
    arguments = parser.parse_args(argv)
    if arguments.command == "plan":
        print(json.dumps(plan(arguments.source, arguments.output), indent=2))
    elif arguments.command == "render-slurm":
        render(arguments.source, arguments.output)
    elif arguments.command == "run":
        run(arguments.source, arguments.index)
    else:
        assemble(arguments.template, arguments.receipts, arguments.output)


if __name__ == "__main__":
    main()
