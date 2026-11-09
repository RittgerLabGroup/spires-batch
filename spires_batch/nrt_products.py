"""Immutable, cumulative tile/water-year NetCDF issue products for NRT commits."""

from pathlib import Path

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


def _write_product(
    path, template, dates, records, *, tile, year, issue, identity, ledger
):
    """Stream contiguous source intervals with bounded raster memory/file handles."""
    import netCDF4

    with netCDF4.Dataset(template) as source, netCDF4.Dataset(path, "w") as out:
        source.set_auto_maskandscale(False)
        height, width = (len(source.dimensions[k]) for k in ("y", "x"))
        for name, count in (("time", len(dates)), ("y", height), ("x", width)):
            out.createDimension(name, count)
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
                        max(1, min(8, len(dates))), min(20, height), min(240, width)
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

        begin = 0
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


def publish_issue_products(folder, ledger, store):
    """Build selected products per tile/WY; reuse unchanged immutable products.

    Replay interval files remain the authoritative per-day sources in the ledger.
    This materializes their latest committed values as standalone cumulative files.
    Empty (not yet mature) issues retain the same variable schema. Catalog metadata
    is expanded here, but source rasters are read only in bounded blocks.
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
    for tile in sorted(ledger["template"]["tiles"]):
        for year in range(_water_year(start), _water_year(through) + 1):
            first = max(start, f"{year - 1}-10-01")
            template = checked(history[f"{tile}/{first}/first"]["file"])
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
                else:
                    _write_product(
                        path, template, dates, records,
                        tile=tile, year=year, issue=issue, identity=identity, ledger=ledger,
                    )
                    record = store.put(path, "products")
                outputs[f"product_{tile}_WY{year}_{name}"] = path
                saved[key] = dict(file=record, source_identity=identity)
    return outputs, saved
