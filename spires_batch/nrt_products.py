"""Immutable, cumulative tile/water-year NetCDF issue products for NRT commits."""

from pathlib import Path
import shutil
import time
import json

import numpy as np

from spires_interpolation.issues import (
    ISSUE_NAMES,
    ISSUE_AGES,
    ISSUE_SCHEMA,
    selected_issue_indices,
)
from .interpolation import digest
from .nrt_storage import checked, expand, link_or_copy


def _water_year(day):
    return int(day[:4]) + (day[5:7] >= "10")


def _create_product(
    path, template, dates, records, *, tile, year, issue, identity, ledger
):
    """Stream contiguous source intervals with bounded raster memory/file handles."""
    import netCDF4

    with netCDF4.Dataset(template) as source, netCDF4.Dataset(path, "w") as out:
        source.set_auto_maskandscale(False)
        height, width = (len(source.dimensions[k]) for k in ("y", "x"))
        for name, count in (("time", len(dates)), ("y", height), ("x", width)):
            out.createDimension(name, None if name == "time" else count)
        attributes = {k: source.getncattr(k) for k in source.ncattrs()}
        # A cumulative product can contain many plans and both NRT/standard inputs.
        attributes.pop("plan_identity", None)
        attributes.pop("asset_provenance", None)
        attributes.update(
            tile=tile,
            water_year=year,
            issue=ISSUE_NAMES[issue],
            issue_number=issue + 1,
            future_days=ISSUE_AGES[issue],
            issue_schema=ISSUE_SCHEMA,
            donor_policy="screened_direct_observations",
            use_snow_climatology=int(
                ledger["template"].get("use_snow_climatology", True)
            ),
            source_stream="per_day_inventory",
            available_through=ledger["available_through"],
            output_issues=ledger["template"].get("output_issues", "all_issues"),
            source_catalog_identity=identity,
            provenance=(
                "Generation ledger.json and its monthly catalogs identify each source date and plan"
            ),
        )
        out.setncatts(attributes)
        raster_names = []
        for name, var in source.variables.items():
            attrs = {k: var.getncattr(k) for k in var.ncattrs() if k != "_FillValue"}
            options = {}
            if "_FillValue" in var.ncattrs():
                options["fill_value"] = var.getncattr("_FillValue")
            if var.dimensions == ("time", "y", "x"):
                raster_names.append(name)
                options.update(
                    zlib=True,
                    complevel=2,
                    shuffle=True,
                    chunksizes=(
                        8, min(20, height), min(240, width)
                    ),
                )
            elif var.dimensions not in ((), ("time",), ("y",), ("x",)):
                raise ValueError(f"Unexpected issue variable dimensions: {name}")
            target = out.createVariable(name, var.dtype, var.dimensions, **options)
            target.setncatts(attrs)
            target.set_auto_maskandscale(False)
            if name == "time":
                target[:] = dates.astype("i8")
            elif "time" not in var.dimensions:
                target[...] = var[...]
            else:
                target.set_var_chunk_cache(size=1024**2, nelems=1009, preemption=0.75)
        if not raster_names:
            raise ValueError("An issue product must contain raster variables")

    return raster_names, height, width, attributes


def _copy_dates(path, dates, records, begin_day, *, tile, issue, raster_names, height, width):
    import netCDF4

    with netCDF4.Dataset(path, "r+") as out:
        out.set_auto_maskandscale(False)
        for name in raster_names:
            out[name].set_var_chunk_cache(size=1024**2, nelems=1009, preemption=0.75)
        begin = begin_day
        while begin < len(records):
            record = records[begin]
            end = begin + 1
            while (
                end < len(records)
                and records[end]["file"] == record["file"]
                and records[end]["index"] == record["index"] + end - begin
            ):
                end += 1
            with netCDF4.Dataset(checked(record["file"])) as data:
                data.set_auto_maskandscale(False)
                if (
                    data.getncattr("issue_schema") != ISSUE_SCHEMA
                    or data.getncattr("future_days") != ISSUE_AGES[issue]
                    or data.getncattr("issue") != ISSUE_NAMES[issue]
                    or data.getncattr("tile") != tile
                    or set(data.variables) != set(out.variables)
                    or any(
                        len(data.dimensions[k]) != len(out.dimensions[k])
                        for k in ("y", "x")
                    )
                    or any(
                        not np.array_equal(data[k][:], out[k][:]) for k in ("y", "x")
                    )
                ):
                    raise ValueError("Cumulative source issue/grid/schema mismatch")
                index = record["index"]
                if index < 0 or not np.array_equal(
                    data["time"][index : index + end - begin],
                    dates[begin:end].astype("i8"),
                ):
                    raise ValueError("Cumulative source date/index mismatch")
                for name in raster_names:
                    var = data[name]
                    if var.dimensions != ("time", "y", "x") or var.dtype != out[name].dtype:
                        raise ValueError(f"Cumulative variable schema mismatch: {name}")
                    var.set_var_chunk_cache(size=1024**2, nelems=1009, preemption=0.75)
                    rows = min(20, height)
                    days = min(
                        8, (128 * 1024**2) // (2 * rows * width * var.dtype.itemsize)
                    )
                    if days < 1:
                        raise MemoryError("Cumulative export cannot hold one stripe-day")
                    for first in range(begin, end, days):
                        stop = min(end, first + days)
                        offset = index + first - begin
                        for row in range(0, height, rows):
                            out[name][first:stop, row : row + rows, :] = var[
                                offset : offset + stop - first, row : row + rows, :
                            ]
            begin = end

    with netCDF4.Dataset(path) as out:
        if not np.array_equal(out["time"][:], dates.astype("i8")) or any(
            out[name].shape != (len(dates), height, width) for name in raster_names
        ):
            raise ValueError("Incomplete cumulative issue product")


def _compressed_prefix(source_path, destination, days, variables):
    """Convert legacy fixed-time history without decoding complete prefix chunks.

    NetCDF creates the dimensions/scales/metadata; HDF5 transfers only raw chunks
    between identical dtype/filter/chunk layouts. Partial time chunks are handled
    by the ordinary NetCDF writer. Source files are always opened read-only.
    """
    import h5py

    def filters(dataset):
        properties = dataset.id.get_create_plist()
        return [properties.get_filter(i) for i in range(properties.get_nfilters())]

    with h5py.File(source_path, "r") as source, h5py.File(destination, "r+") as out:
        for name in variables:
            a, b = source[name], out[name]
            if (a.dtype != b.dtype or a.chunks != b.chunks or a.chunks is None
                    or filters(a) != filters(b)
                    or not np.array_equal(a.fillvalue, b.fillvalue, equal_nan=True)):
                return 0, 0
        time_chunks = {source[name].chunks[0] for name in variables}
        if len(time_chunks) != 1:
            return 0, 0
        copied = days // next(iter(time_chunks)) * next(iter(time_chunks))
        byte_count = 0
        for name in variables:
            a, b = source[name], out[name]
            b.resize((len(out["time"]), *a.shape[1:]))
            nt, ny, nx = a.chunks
            for t in range(0, copied, nt):
                for y in range(0, a.shape[1], ny):
                    for x in range(0, a.shape[2], nx):
                        offset = (t, y, x)
                        if a.id.get_chunk_info_by_coord(offset).size == 0:
                            continue  # Equal fill values; preserve unallocated chunks.
                        mask, data = a.id.read_direct_chunk(offset)
                        b.id.write_direct_chunk(offset, data, filter_mask=mask)
                        byte_count += len(data)
    return copied, byte_count


def _write_product(path, template, dates, records, *, tile, year, issue, identity, ledger,
                   prior=None, reuse_days=0):
    """Copy immutable history once and write only the changed/appended suffix."""
    import netCDF4

    began = time.perf_counter()
    names, height, width, attributes = _create_product(
        path, template, dates, records, tile=tile, year=year, issue=issue,
        identity=identity, ledger=ledger)
    metrics = dict(mode="rebuild", unchanged_days=reuse_days, reused_days=0, raw_chunk_bytes=0)
    begin = 0
    if prior is not None and reuse_days:
        previous = checked(prior["file"])
        with netCDF4.Dataset(previous) as old:
            old.set_auto_maskandscale(False)
            if (old.getncattr("source_catalog_identity") != prior["source_identity"]
                    or old.getncattr("tile") != tile or old.getncattr("issue") != ISSUE_NAMES[issue]
                    or old.getncattr("issue_schema") != ISSUE_SCHEMA
                    or set(old.variables) != set(names) | {"time", "x", "y", "crs"}
                    or not 0 <= reuse_days <= len(old.dimensions["time"])
                    or len(old.dimensions["time"]) > len(dates)
                    or not np.array_equal(old["time"][:], dates[:len(old.dimensions["time"])].astype("i8"))):
                raise ValueError("Previous cumulative product differs from its committed catalog")
            with netCDF4.Dataset(path) as target:
                if any(not np.array_equal(old[k][:], target[k][:]) for k in ("x", "y")):
                    raise ValueError("Previous cumulative grid changed")
                for name in names:
                    if old[name].dtype != target[name].dtype or old[name].dimensions != target[name].dimensions:
                        raise ValueError("Previous cumulative variable schema changed")
            extendable = old.dimensions["time"].isunlimited()
        copied = time.perf_counter()
        if extendable:
            # Never link a prior generation that will be modified in place.
            shutil.copyfile(previous, path)
            begin = reuse_days
            metrics["mode"] = "copy_and_update"
        else:
            begin, metrics["raw_chunk_bytes"] = _compressed_prefix(previous, path, reuse_days, names)
            metrics["mode"] = "convert_fixed_time" if begin else "rebuild"
        metrics["history_copy_seconds"] = time.perf_counter() - copied
        with netCDF4.Dataset(path, "r+") as out:
            out.setncatts(attributes)
            out["time"][:] = dates.astype("i8")
    metrics["reused_days"] = begin
    updated = time.perf_counter()
    _copy_dates(path, dates, records, begin, tile=tile, issue=issue,
                raster_names=names, height=height, width=width)
    metrics.update(updated_days=len(dates) - begin, update_seconds=time.perf_counter() - updated,
                   seconds=time.perf_counter() - began)
    return metrics


def publish_issue_products(folder, ledger, store, *, previous_history=None, templates=None):
    """Build selected products per tile/WY; reuse unchanged immutable products.

    Replay interval files remain the authoritative per-day sources in the ledger.
    This materializes their latest committed values as standalone cumulative files.
    Empty (not yet mature) issues retain the same variable schema. Catalog metadata
    is expanded here, but source rasters are read only in bounded blocks.
    Per-tile templates supply that schema when no selected issue has matured yet.
    """
    if "product_start" not in ledger:
        raise ValueError("Eight-issue NRT products require a new historical bootstrap")
    start, through = ledger["product_start"], ledger["available_through"]
    history = expand(ledger, start=start, stop=through)["products"]
    previous = ledger.get("issue_products", {})
    selected = selected_issue_indices(
        ledger["template"].get("output_issues", "all_issues")
    )
    outputs, saved = {}, {}
    metrics = {}
    for tile in sorted(ledger["template"]["tiles"]):
        for year in range(_water_year(start), _water_year(through) + 1):
            first = max(start, f"{year - 1}-10-01")
            for issue in selected:
                name, age = ISSUE_NAMES[issue], ISSUE_AGES[issue]
                stop = min(
                    np.datetime64(f"{year}-10-01"), np.datetime64(through) + 1 - age
                )
                dates = np.arange(np.datetime64(first), stop)
                try:
                    records = [history[f"{tile}/{day}/{name}"] for day in dates]
                except KeyError as exc:
                    raise ValueError(
                        f"Missing cumulative issue date: {exc.args[0]}"
                    ) from exc
                key = f"{tile}/WY{year}/{name}"
                identity = digest(
                    dict(
                        issue_schema=ISSUE_SCHEMA, product=key, start=first,
                        dates=[str(day) for day in dates], records=records,
                    )
                )
                path = Path(folder) / f"{tile}_WY{year}_{name}.nc"
                prior = previous.get(key)
                if prior is not None and prior["source_identity"] == identity:
                    link_or_copy(checked(prior["file"]), path)
                    record = prior["file"]
                    metrics[key] = dict(mode="unchanged", reused_days=len(dates), updated_days=0)
                else:
                    if records:
                        template_record = records[0]["file"]
                    elif templates and tile in templates:
                        template_record = templates[tile]
                    elif prior is not None:
                        template_record = prior["file"]
                    else:
                        # Compatibility for callers with another mature issue
                        # in the catalog, but no explicit empty-file template.
                        entry = next((v for k, v in history.items()
                                      if k.startswith(tile + "/")), None)
                        if entry is None:
                            raise ValueError(f"No cumulative product template for {tile}")
                        template_record = entry["file"]
                    template = checked(template_record)
                    reuse_days = 0
                    if prior is not None and previous_history is not None:
                        for day, rec in zip(dates, records):
                            if previous_history.get(f"{tile}/{day}/{name}") != rec:
                                break
                            reuse_days += 1
                    metrics[key] = _write_product(
                        path, template, dates, records,
                        tile=tile, year=year, issue=issue, identity=identity, ledger=ledger,
                        prior=prior, reuse_days=reuse_days,
                    )
                    record = store.put(path, "products")
                (Path(folder) / "publication-metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
                outputs[f"product_{tile}_WY{year}_{name}"] = path
                saved[key] = dict(file=record, source_identity=identity)
    return outputs, saved
