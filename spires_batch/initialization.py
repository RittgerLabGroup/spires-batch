"""Initialize tile storage and register externally produced, reusable assets."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import R0Config, RunConfig, SelectionConfig, StagingVerification
from .staging import StagingAction, execute_actions


class Storage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    durable_root: Path = Path("/pl/active/rittger_ops/spipy")
    scratch_root: Path = Path("/scratch/alpine/ropa5718/spipy")
    reflectance_roots: dict[str, Path] = Field(
        default_factory=lambda: {
            "VJ109GA.002": Path("/pl/active/rittger_ops/vj109ga.002"),
            "VNP09GA.002": Path("/pl/active/rittger_ops/vnp09ga.002"),
        }
    )

    @model_validator(mode="after")
    def validate_roots(self):
        roots = [self.durable_root, self.scratch_root, *self.reflectance_roots.values()]
        if any(not path.expanduser().is_absolute() for path in roots):
            raise ValueError("Storage and shared reflectance roots must be absolute")
        self.durable_root = self.durable_root.expanduser().resolve()
        self.scratch_root = self.scratch_root.expanduser().resolve()
        self.reflectance_roots = {
            name: path.expanduser().resolve()
            for name, path in self.reflectance_roots.items()
        }
        pairs = [(self.durable_root, self.scratch_root)] + [
            (home, archive)
            for home in (self.durable_root, self.scratch_root)
            for archive in self.reflectance_roots.values()
        ]
        for home, other in pairs:
            if home.is_relative_to(other) or other.is_relative_to(home):
                raise ValueError(
                    "Managed roots and shared reflectance archives must not overlap"
                )
        return self


class Asset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    kind: Literal["lut", "cloud_model", "ancillary", "climatology", "r0"]
    relative_path: Path
    tile: str | None = None
    water_year: int | None = Field(default=None, ge=1900, le=2200)
    required: bool = True

    @model_validator(mode="after")
    def validate_binding(self):
        path = self.relative_path
        if path.is_absolute() or ".." in path.parts or not path.name:
            raise ValueError(
                "Asset paths must name individual files relative to assets/"
            )
        folders = {
            "lut": "luts",
            "cloud_model": "cloud-models",
            "ancillary": "ancillary",
            "climatology": "climatology",
            "r0": "r0",
        }
        if len(path.parts) < 2 or path.parts[0] != folders[self.kind]:
            raise ValueError(
                f"{self.kind} files must be under assets/{folders[self.kind]}/"
            )
        if self.kind in {"ancillary", "climatology", "r0"} and self.tile is None:
            raise ValueError("Tile assets require an explicit tile")
        if self.kind == "r0" and self.water_year is None:
            raise ValueError("Existing R0 requires its processing water year")
        return self


class Initialization(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    sensor: str = "viirs"
    platform: str = "noaa20"
    domain: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    tiles: list[str] = Field(min_length=1)
    water_years: list[int] = Field(min_length=1)
    asset_version: str = Field(default="v1", pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
    storage: Storage = Field(default_factory=Storage)
    assets: list[Asset] = Field(default_factory=list)
    stage_assets: bool = False

    @model_validator(mode="after")
    def validate_selection(self):
        identity = RunConfig(
            name="tile-initialization", sensor=self.sensor, platform=self.platform
        )
        self.sensor, self.platform = identity.sensor, identity.platform
        selection = SelectionConfig(tiles=self.tiles, water_years=self.water_years)
        self.tiles = list(selection.tiles)
        self.water_years = list(selection.water_years)
        if len({asset.id for asset in self.assets}) != len(self.assets):
            raise ValueError("Asset IDs must be unique")
        if len({asset.relative_path for asset in self.assets}) != len(self.assets):
            raise ValueError("Register each asset file once")
        bindings = set()
        for asset in self.assets:
            if asset.tile is not None and asset.tile not in self.tiles:
                raise ValueError(f"Asset outside selected tile domain: {asset.id}")
            if (
                asset.water_year is not None
                and asset.water_year not in self.water_years
            ):
                raise ValueError(f"Asset outside selected water years: {asset.id}")
            if asset.kind == "r0":
                binding = (asset.tile, asset.water_year)
                if binding in bindings:
                    raise ValueError(
                        "Only one existing R0 may bind each tile/water year"
                    )
                bindings.add(binding)
        return self


def contained(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"Managed path escapes its root: {path}")
    return path


def plan(config):
    """Inspect file metadata only; missing external assets are reported, not built."""
    config = Initialization.model_validate(config)
    storage = config.storage
    durable, scratch = storage.durable_root, storage.scratch_root
    directories = set()

    def directory(root, relative):
        path = contained(root, relative)
        directories.add(str(path))
        return path

    for name in (
        "assets/luts",
        "assets/cloud-models",
        "assets/catalogs",
        "runs/initialization",
    ):
        directory(durable, name)
    for name in ("cache/assets", "work", "streams"):
        directory(scratch, name)
    state_root = directory(durable, f"state/{config.platform}/{config.domain}")
    directory(scratch, f"streams/{config.platform}/{config.domain}")
    for tile in config.tiles:
        directory(durable, f"assets/ancillary/{tile}/{config.asset_version}")
        directory(durable, f"assets/climatology/{tile}/{config.asset_version}")
        directory(durable, f"products/{config.platform}/{tile}")
        for year in config.water_years:
            directory(durable, f"assets/r0/{config.platform}/{tile}/summer-{year - 1}")
            directory(scratch, f"work/{config.platform}/{tile}/WY{year}")
    assets = []
    actions = []
    r0_artifacts = []
    for asset in config.assets:
        source = contained(durable, Path("assets") / asset.relative_path)
        target = contained(scratch, Path("cache/assets") / asset.relative_path)
        directories.update((str(source.parent), str(target.parent)))
        if source.exists() and not source.is_file():
            raise ValueError(
                f"Asset must be an individual file, not a directory: {source}"
            )
        stat = source.stat() if source.is_file() else None
        reusable = bool(
            stat
            and target.is_file()
            and (target.stat().st_size, target.stat().st_mtime_ns)
            == (stat.st_size, stat.st_mtime_ns)
        )
        record = dict(
            asset.model_dump(mode="json"),
            source_path=str(source),
            scratch_path=str(target),
            status="cached" if reusable else "available" if stat else "missing",
            size_bytes=stat.st_size if stat else None,
        )
        assets.append(record)
        if stat:
            actions.append(
                StagingAction(
                    source,
                    target,
                    stat.st_size,
                    StagingVerification.STAT,
                    True,
                    stat.st_mtime_ns,
                )
            )
        if asset.kind == "r0":
            r0_artifacts.append(
                dict(
                    id=asset.id,
                    path=str(source),
                    tile=asset.tile,
                    water_year=asset.water_year,
                )
            )
    existing_r0 = (
        R0Config(mode="existing", artifacts=r0_artifacts).model_dump(
            mode="json", exclude_none=True
        )
        if r0_artifacts
        else None
    )
    report = dict(
        schema_version=1,
        configuration=config.model_dump(mode="json"),
        directories=sorted(directories),
        assets=assets,
        missing_required_assets=[
            asset["id"]
            for asset in assets
            if asset["required"] and asset["status"] == "missing"
        ],
        unregistered_r0=[
            f"{tile}/WY{year}"
            for tile in config.tiles
            for year in config.water_years
            if not any(
                asset.tile == tile and asset.water_year == year and asset.kind == "r0"
                for asset in config.assets
            )
        ],
        reflectance_roots={
            name: dict(path=str(path), exists=path.is_dir())
            for name, path in storage.reflectance_roots.items()
        },
        staging_requested=config.stage_assets,
        uncached_asset_bytes=sum(
            asset["size_bytes"] for asset in assets if asset["status"] == "available"
        ),
        transfer_bytes=(
            sum(
                asset["size_bytes"]
                for asset in assets
                if asset["status"] == "available"
            )
            if config.stage_assets
            else 0
        ),
        existing_r0=existing_r0,
        state_root=str(state_root),
        scratch_stream_root=str(
            contained(scratch, f"streams/{config.platform}/{config.domain}")
        ),
        note="Asset availability only; scientific preflight remains required. No ancillary generation, reflectance download, R0 build or history transfer.",
    )
    return report, tuple(actions)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".initialization-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def initialize(config, *, dry_run=False):
    """Create an idempotent layout; optionally stage explicitly registered assets."""
    config = Initialization.model_validate(config)
    report, actions = plan(config)
    report["dry_run"] = dry_run
    if dry_run:
        return report
    if config.stage_assets and report["missing_required_assets"]:
        raise ValueError(
            "Required assets are missing; nothing staged: "
            + ", ".join(report["missing_required_assets"])
        )
    catalog = contained(
        config.storage.durable_root, f"assets/catalogs/{config.domain}.json"
    )
    catalog.parent.mkdir(parents=True, exist_ok=True)
    with catalog.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if catalog.exists():
            old = json.loads(catalog.read_text())["configuration"]
            for key in ("sensor", "platform", "tiles", "storage"):
                if old[key] != report["configuration"][key]:
                    raise ValueError(
                        f"Existing domain has different {key}; use a separate domain or explicitly migrate it"
                    )
        for directory in report["directories"]:
            Path(directory).mkdir(parents=True, exist_ok=True)
        write_json(catalog, report)
        if config.stage_assets:
            print(
                json.dumps(
                    dict(
                        transfer_bytes=report["transfer_bytes"],
                        files=[
                            asset
                            for asset in report["assets"]
                            if asset["status"] == "available"
                        ],
                    ),
                    indent=2,
                ),
                flush=True,
            )
            report["staging"] = [
                dict(path=str(result.action.destination_path), status=result.status)
                for result in execute_actions(actions)
            ]
        receipt = contained(
            config.storage.durable_root, f"runs/initialization/{config.domain}.json"
        )
        write_json(receipt, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect paths and sizes without creating or copying anything",
    )
    parser.add_argument(
        "--stage-assets",
        action="store_true",
        help="Copy only registered available assets to scratch; never stage reflectance or history",
    )
    arguments = parser.parse_args(argv)
    try:
        config = Initialization.model_validate_json(arguments.config.read_text())
        if arguments.stage_assets:
            config.stage_assets = True
        print(json.dumps(initialize(config, dry_run=arguments.dry_run), indent=2))
        return 0
    except (OSError, ValueError) as error:
        print(f"Tile initialization stopped: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
