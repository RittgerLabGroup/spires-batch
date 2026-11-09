"""Versioned NRT inventory, standard-product replacement and domain publication.

The eight temporal issues are independent of input quality. Every unresolved
NRT date is revisited. Replacements replay causal state and cross-tile donors;
prior generations remain available as the as-issued record.
"""

import argparse
import copy
from datetime import date, timedelta
import json
from pathlib import Path
import re
from spires_interpolation.issues import ISSUE_NAMES, selected_issue_indices
from .interpolation import (
    digest,
    file_record,
    plan as make_plan,
    load_plan,
    execute,
    state_context,
    compatible_context,
)

NAME = re.compile(
    "^(VJ109GA(?:_NRT)?)\\.A(\\d{4})(\\d{3})\\.(h\\d{2}v\\d{2})\\.(\\d{3})\\.(\\d+)\\.h5$"
)


def source_record(path, *, prepared=None, prepared_context=None):
    rec = file_record(path)
    m = NAME.fullmatch(Path(path).name)
    if m is None:
        raise ValueError("Expected a NOAA-20 VJ109GA standard/NRT granule")
    product, year, doy, tile, collection, processing = m.groups()
    day = date(int(year), 1, 1) + timedelta(days=int(doy) - 1)
    if day.year != int(year) or int(doy) < 1:
        raise ValueError("Invalid granule day")
    rec.update(
        product=product,
        date=day.isoformat(),
        tile=tile,
        collection=collection,
        processing=processing,
        source_stream="nrt" if product.endswith("_NRT") else "standard",
    )
    if prepared is not None:
        rec.update(prepared=prepared, prepared_context=prepared_context)
    return rec


def source_identity(rec):
    if rec is None:
        return None
    identity = rec.get("origin", rec)
    return {
        k: identity[k]
        for k in (
            "path",
            "bytes",
            "mtime_ns",
            "product",
            "date",
            "tile",
            "collection",
            "processing",
        )
    }


def rank(rec):
    return (
        rec["source_stream"] == "standard",
        int(rec["processing"]),
        rec.get("cmr_updated", ""),
    )


def choose_sources(
    previous,
    candidates,
    *,
    tiles,
    collection,
    through,
    history_start,
    calendar_start=None,
):
    """Prefer standard over NRT, latest processing within the pinned collection.

    The last committed source participates in selection: temporary catalog gaps
    never remove observations or downgrade standard data back to NRT.
    """
    selected = copy.deepcopy(previous)
    for rec in candidates:
        actual = source_record(rec["path"])
        if "origin" in rec and any(
            (
                rec["origin"].get(k) != actual[k]
                for k in (
                    "bytes",
                    "mtime_ns",
                    "product",
                    "date",
                    "tile",
                    "collection",
                    "processing",
                )
            )
        ):
            raise ValueError(
                "Archived source origin disagrees with its physical vintage"
            )
        if any((actual[k] != rec.get(k) for k in actual)):
            raise ValueError(
                "Candidate metadata changed or disagrees with its filename"
            )
        if rec["tile"] not in tiles or rec["collection"] != collection:
            raise ValueError("Candidate outside configured domain/collection")
        if not history_start <= rec["date"] <= through:
            continue
        key = rec["tile"] + "/" + rec["date"]
        old = selected.get(key)
        if old is None or rank(rec) > rank(old):
            selected[key] = copy.deepcopy(rec)
        elif rank(rec) == rank(old):
            if source_identity(rec) == source_identity(old):
                if "prepared" in rec:
                    selected[key] = copy.deepcopy(rec)
            elif rec["path"] == old["path"]:
                selected[key] = copy.deepcopy(rec)
            else:
                raise ValueError(f"Ambiguous equal-ranked source vintages: {key}")
    day = date.fromisoformat(calendar_start or history_start)
    while day <= date.fromisoformat(through):
        for tile in tiles:
            selected.setdefault(tile + "/" + day.isoformat(), None)
        day += timedelta(days=1)
    changed = sorted(
        (
            k
            for k in selected
            if source_identity(selected[k]) != source_identity(previous.get(k))
        )
    )
    changed += sorted(
        (k for k in selected if k not in previous and selected[k] is None)
    )
    return (selected, changed)


def day_context(inventory, tiles, day):
    return digest(
        {t: source_identity(inventory.get(t + "/" + day)) for t in sorted(tiles)}
    )


def acquisition_dates(inventory, through, *, lookback_days=7):
    end = date.fromisoformat(through)
    days = {(end - timedelta(days=i)).isoformat() for i in range(lookback_days)}
    days.update(
        (
            k.split("/")[1]
            for k, v in inventory.items()
            if v is None or v["source_stream"] == "nrt"
        )
    )
    return sorted(days)


def current(root):
    from spires_interpolation.publication import current_generation, artifact_paths

    generation = current_generation(root)
    if generation is None:
        raise ValueError("Bootstrap this stream from validated historical output first")
    paths = artifact_paths(root, generation)
    return (generation, paths, json.loads(paths["ledger"].read_text()))


def _bootstrap(historical_plan, inventory, root):
    """Register existing history once, without recomputation or an annual reset."""
    from spires_interpolation.publication import current_generation, artifact_paths

    p = load_plan(historical_plan, compatible_state=True)
    r = p["request"]
    if r["stop"] != r["available_through"]:
        raise ValueError("NRT bootstrap requires historical output through available_through")
    from .interpolation import mapped_fields
    from .nrt_storage import checked

    for path in {
        field["path"]
        for tile in r["tiles"].values()
        for field in mapped_fields(tile["daily"])
    }:
        checked(p["inputs"][path])
    tiles = sorted(r["tiles"])
    expected = {
        t + "/" + d
        for t in tiles
        for d in r["tiles"][t]["daily"]
        if r["history_start"] <= d <= r["available_through"]
    }
    if set(inventory) != expected:
        raise ValueError("Historical inventory must explicitly cover every tile/date")
    for key, rec in inventory.items():
        tile, day = key.split("/")
        if (r["tiles"][tile]["daily"][day] is None) != (rec is None):
            raise ValueError("Inventory/prepared observation disagreement")
        if rec is not None:
            actual = source_record(rec["path"])
            if (
                any((actual[k] != rec.get(k) for k in actual))
                or actual["tile"] != tile
                or actual["date"] != day
                or (rec.get("source_stream") != actual["source_stream"])
                or (actual["collection"] != "002")
            ):
                raise ValueError("Historical source identity mismatch")
    products = {}
    checkpoints = {}
    for tile in tiles:
        folder = Path(r["output"]) / "products" / tile
        generation = current_generation(folder)
        if generation is None or generation["request"] != {
            "plan": p["identity"],
            "tile": tile,
        }:
            raise ValueError("Historical output is not the planned generation")
        paths = artifact_paths(folder, generation)
        checkpoints[tile] = paths["checkpoints"]
        issues = [
            ISSUE_NAMES[i]
            for i in selected_issue_indices(r.get("output_issues", "all_issues"))
        ]
        if not set(issues).issubset(paths):
            raise ValueError("Bootstrap requires the configured issue products; replay older history")
        for issue in issues:
            if issue in paths:
                import netCDF4

                with netCDF4.Dataset(paths[issue]) as ds:
                    import numpy as np

                    for i, day in enumerate(ds["time"][:]):
                        products[f"{tile}/{np.datetime64(int(day), 'D')}/{issue}"] = (
                            dict(file=file_record(paths[issue]), index=i)
                        )
    template = copy.deepcopy(r)
    from .configuration import CONFIG_SCHEMA_VERSION
    from .serialization import write_immutable_json

    template.update(config_type="interpolation", schema_version=CONFIG_SCHEMA_VERSION)
    scheduler_policy = {
        **template["scheduler"]["snapshot"],
        "config_type": "scheduler_policy",
        "schema_version": CONFIG_SCHEMA_VERSION,
    }
    policy_path = Path(root) / "policies" / (digest(scheduler_policy) + ".json")
    if policy_path.exists():
        if json.loads(policy_path.read_text()) != scheduler_policy:
            raise ValueError("Registered scheduler policy changed")
    else:
        write_immutable_json(policy_path, scheduler_policy)
    template["scheduler"]["policy"] = str(policy_path.resolve())
    template.pop("state_identity")
    template.pop("state_context", None)
    template.pop("seed", None)
    from .nrt_storage import Store

    template["seed_plan"] = Store(root).put(historical_plan, "plans")["path"]
    template["scheduler"].pop("snapshot", None)
    from .nrt_storage import archive_provenance

    provenance = archive_provenance(root, historical_plan)
    ledger = dict(
        provenance=provenance,
        schema_version=1,
        available_through=r["available_through"],
        history_start=r["history_start"],
        product_start=r["start"],
        state_identity=r["state_identity"],
        state_context=state_context(r, p["inputs"]),
        template=template,
        inventory=inventory,
        products=products,
        unresolved_nrt=sorted(
            (
                k
                for k, v in inventory.items()
                if v is not None and v["source_stream"] == "nrt"
            )
        ),
    )
    return commit(
        root,
        ledger,
        checkpoints,
        expected_parent=None,
        identity=dict(bootstrap=p["identity"], inventory=digest(inventory)),
    )


def extend_annual_context(template, additions):
    """Add explicitly registered annual masks, retaining all original bindings."""
    result = copy.deepcopy(template)
    for tile, years in (additions or {}).items():
        if tile not in result["tiles"]:
            raise ValueError(f"Annual mask outside tile domain: {tile}")
        previous = result["tiles"][tile]["bright_soil"]
        for year, field in years.items():
            if not re.fullmatch("\\d{4}", year):
                raise ValueError("Annual mask keys must be four-digit water years")
            field = copy.deepcopy(field)
            field["path"] = str(Path(field["path"]).resolve())
            if year in previous and previous[year] != field:
                raise ValueError(
                    f"Cannot replace a registered annual mask: {tile}/WY{year}"
                )
            previous[year] = field
    return result


def ledger_context(ledger, seed_plan=None):
    if "state_context" in ledger:
        from spires_interpolation.algorithm import resolve_algorithm

        context = ledger["state_context"]
        algorithm = resolve_algorithm(
            ledger["template"].get("algorithm", "viirs_snow"),
            use_snow_climatology=ledger["template"].get("use_snow_climatology", True),
        )
        if (
            context.get("schema_version") != 2
            or context["fixed"].get("algorithm") != algorithm.compatibility()
        ):
            raise ValueError(
                "Stream scientific recipe or state format is incompatible; bootstrap compatible history"
            )
        return context
    raise ValueError(
        "Stream lacks scientific compatibility metadata; bootstrap from schema-2 historical products"
    )


def _plan_update(
    root,
    candidates,
    through,
    directory,
    *,
    collection="002",
    annual_bright_soil=None,
    seed_plan=None,
):
    """Freeze a revision plan after upstream preparation of changed date domains.

    Prepared observations on each changed date must identify the complete selected
    same-day source context. This prevents mixing new cloud halos with stale donors.
    """
    import numpy as np

    generation, paths, ledger = current(root)
    from .nrt_storage import expand, checkpoint_coverage, can_restart, recoveries

    ledger = expand(ledger, days={rec["date"] for rec in candidates})
    r = extend_annual_context(ledger["template"], annual_bright_soil)
    tiles = sorted(r["tiles"])
    if seed_plan is not None:
        r["seed_plan"] = str(Path(seed_plan).resolve())
    previous_context = ledger_context(ledger, seed_plan)
    through = str(np.datetime64(through, "D"))
    if through < ledger["available_through"]:
        raise ValueError("Availability may not move backwards")
    inventory, changed = choose_sources(
        ledger["inventory"],
        candidates,
        tiles=tiles,
        collection=collection,
        through=through,
        history_start=ledger["history_start"],
        calendar_start=str(np.datetime64(ledger["available_through"]) + 1),
    )
    if not changed and through == ledger["available_through"]:
        return dict(status="unchanged", generation=generation["generation"])
    changed_days = sorted({k.split("/")[1] for k in changed})
    if through > ledger["available_through"]:
        changed_days += list(
            map(
                str,
                np.arange(
                    np.datetime64(ledger["available_through"]) + 1,
                    np.datetime64(through) + 1,
                ),
            )
        )
    changed_days = sorted(set(changed_days))
    for day in changed_days:
        context = day_context(inventory, tiles, day)
        for tile in tiles:
            rec = inventory[tile + "/" + day]
            if rec is None:
                r["tiles"][tile]["daily"][day] = None
                continue
            if "prepared" not in rec or rec.get("prepared_context") != context:
                raise ValueError(
                    f"Prepare the selected same-day cloud/inversion context for every tile: {tile}/{day}; context={context}"
                )
            r["tiles"][tile]["daily"][day] = rec["prepared"]
    beginning = np.datetime64(r["start"])
    first = max(beginning, np.datetime64(changed_days[0]) - 7)
    r.update(
        start=str(first),
        stop=through,
        available_through=through,
        source_stream="mixed",
        seed={t: str(paths["checkpoint_" + t]) for t in tiles},
    )
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    r["output"] = str(directory / "work")

    def coverage(candidate):
        return {t: checkpoint_coverage(path) for t, path in candidate.items()}

    def usable(meta, begin):
        return all(
            (
                can_restart(v, str(begin), through, ledger["history_start"])
                for v in meta.values()
            )
        )

    meta = coverage(r["seed"])
    if not usable(meta, first):
        choices = []
        for old, old_paths, old_ledger in recoveries(root):
            try:
                compatible_context(
                    ledger_context(old_ledger, seed_plan), previous_context
                )
            except ValueError:
                continue
            seed = {t: str(old_paths["checkpoint_" + t]) for t in tiles}
            candidate_meta = coverage(seed)
            end = min(
                (
                    row["checkpoint"][1]
                    for v in candidate_meta.values()
                    for row in v["stripes"]
                )
            )
            begin = min(first, np.datetime64(end + 1, "D"))
            if begin < beginning or not usable(candidate_meta, begin):
                continue
            old_prefix = expand(old_ledger, stop=str(begin - 1))["inventory"]
            new_prefix = expand(ledger, stop=str(begin - 1))["inventory"]
            new_prefix.update(inventory)
            superseded = [
                k.split("/")[1]
                for k, v in old_prefix.items()
                if k.split("/")[1] < str(begin)
                and source_identity(v) != source_identity(new_prefix.get(k))
            ]
            if superseded:
                begin = min(begin, np.datetime64(min(superseded)) - 7)
                if begin < beginning or not usable(candidate_meta, begin):
                    continue
            choices.append((str(begin), old["created_utc"], seed))
        if choices:
            begin, _, r["seed"] = max(choices, key=lambda x: x[:2])
            r["start"] = begin
        else:
            from .nrt_storage import stop_full_replay

            stop_full_replay(
                root,
                dict(ledger, template=r),
                through,
                directory,
                reason="No retained compatible checkpoint covers the required replay",
            )
    replay_from = r["start"] if "seed" in r else ledger["history_start"]
    restored = expand(ledger, start=replay_from, stop=through)
    for tile in tiles:
        daily_observations = restored["template"]["tiles"][tile]["daily"]
        daily_observations.update(r["tiles"][tile]["daily"])
        r["tiles"][tile]["daily"] = {
            d: v for d, v in daily_observations.items() if replay_from <= d <= through
        }
    r["stream_root"] = str(Path(root).resolve())
    request = directory / "request.json"
    if request.exists() and json.loads(request.read_text()) != r:
        raise FileExistsError("Revision directory already contains a different request")
    if not request.exists():
        with request.open("x") as stream:
            stream.write(json.dumps(r, indent=2) + "\n")
    scientific = make_plan(request, directory / "plan.json")
    compatible_context(previous_context, scientific["request"]["state_context"])
    update = dict(
        schema_version=1,
        root=str(Path(root).resolve()),
        expected_parent=generation["generation"],
        plan=str(directory / "plan.json"),
        inventory=inventory,
        changed=changed,
        changed_dates=changed_days,
        replay_start=r["start"],
        through=through,
        replay_mode="checkpoint" if "seed" in r else "full_history",
        state_identity=scientific["request"]["state_identity"],
    )
    update["identity"] = digest(update)
    path = directory / "update.json"
    if path.exists() and json.loads(path.read_text()) != update:
        raise FileExistsError("Revision directory already contains a different update")
    path.write_text(json.dumps(update, indent=2) + "\n")
    return update


def commit(root, ledger, checkpoints, *, expected_parent, identity):
    from spires_interpolation.publication import (
        publish_generation,
        current_generation,
        artifact_paths,
    )
    from .nrt_storage import Store, archive_ledger, link_or_copy, publication_archive

    previous = current_generation(root)
    if previous is not None and previous["request"] == identity:
        artifact_paths(root, previous)
        publication_archive(root, previous)
        return dict(previous, reused=True)
    ledger = archive_ledger(root, ledger)
    store = Store(root)

    def build(folder):
        from .nrt_products import publish_issue_products

        outputs = {}
        for tile, path in checkpoints.items():
            stored = store.put(path, "checkpoints", hot=True)
            dest = folder / (tile + "_checkpoints.h5")
            link_or_copy(stored["path"], dest)
            outputs["checkpoint_" + tile] = dest
        products, records = publish_issue_products(folder, ledger, store)
        outputs.update(products)
        ledger["issue_products"] = records
        p = folder / "ledger.json"
        p.write_text(json.dumps(ledger, indent=2) + "\n")
        outputs["ledger"] = p
        return outputs

    def validate(paths):
        import h5py

        for name, path in paths.items():
            if name.startswith("checkpoint_"):
                with h5py.File(path) as h:
                    if (
                        not h.attrs.get("complete")
                        or h.attrs.get("state_identity") != ledger["state_identity"]
                    ):
                        raise ValueError("Checkpoint context mismatch")
        checked = {}
        for item in ledger["products"].values():
            rec = item["file"]
            if rec["path"] not in checked:
                checked[rec["path"]] = file_record(rec["path"])
            if checked[rec["path"]] != rec:
                raise ValueError("Product changed before domain commit")

    result = publish_generation(
        root,
        request=identity,
        expected_parent=expected_parent,
        build=build,
        validate=validate,
    )
    publication_archive(root, result)
    return result


def _finish(update_path):
    import numpy as np, netCDF4
    from spires_interpolation.publication import (
        current_generation,
        artifact_paths,
        StaleGeneration,
    )

    u = json.loads(Path(update_path).read_text())
    identity = u.pop("identity")
    if digest(u) != identity:
        raise ValueError("Revision plan digest mismatch")
    generation, _, ledger = current(u["root"])
    if generation["request"] == dict(update=identity):
        from .nrt_storage import publication_archive

        publication_archive(u["root"], generation)
        return dict(generation, reused=True)
    if generation["generation"] != u["expected_parent"]:
        raise StaleGeneration("Another revision committed first")
    p = load_plan(u["plan"])
    r = p["request"]
    checkpoints = {}
    for tile in r["tiles"]:
        folder = Path(r["output"]) / "products" / tile
        g = current_generation(folder)
        if g is None or g["request"] != {"plan": p["identity"], "tile": tile}:
            raise ValueError("Every donor-domain tile must finish before publication")
        paths = artifact_paths(folder, g)
        checkpoints[tile] = paths["checkpoints"]
        issues = [
            ISSUE_NAMES[i]
            for i in selected_issue_indices(r.get("output_issues", "all_issues"))
        ]
        if not set(issues).issubset(paths):
            raise ValueError("Revision requires the configured issue products")
        for issue in issues:
            with netCDF4.Dataset(paths[issue]) as ds:
                for index, day in enumerate(ds["time"][:]):
                    ledger["products"][
                        f"{tile}/{np.datetime64(int(day), 'D')}/{issue}"
                    ] = dict(file=file_record(paths[issue]), index=index)
    ledger.update(
        inventory=u["inventory"],
        available_through=u["through"],
        last_changed=u["changed"],
        last_replay_start=u["replay_start"],
        state_identity=r["state_identity"],
        state_context=r["state_context"],
        unresolved_nrt=sorted(
            (
                k
                for k, v in u["inventory"].items()
                if v is not None and v["source_stream"] == "nrt"
            )
        ),
    )
    for tile, fields in r["tiles"].items():
        ledger["template"]["tiles"][tile]["daily"].update(fields["daily"])
        ledger["template"]["tiles"][tile]["bright_soil"] = fields["bright_soil"]
    from .nrt_storage import archive_provenance

    ledger["provenance"] = archive_provenance(u["root"], u["plan"])
    if "seed_plan" in r:
        ledger["template"]["seed_plan"] = r["seed_plan"]
    return commit(
        u["root"],
        ledger,
        checkpoints,
        expected_parent=u["expected_parent"],
        identity=dict(update=identity),
    )


def bootstrap(historical_plan, inventory, root):
    from .nrt_storage import storage_lock

    with storage_lock(root):
        return _bootstrap(historical_plan, inventory, root)


def plan_update(root, candidates, through, directory, **kwargs):
    from .nrt_storage import storage_lock, register_workspace, workspace_state

    with storage_lock(root):
        register_workspace(root, directory)
        try:
            result = _plan_update(root, candidates, through, directory, **kwargs)
            if result.get("status") == "unchanged":
                workspace_state(root, directory, "complete")
            return result
        except Exception:
            workspace_state(root, directory, "failed")
            raise


def finish(update_path):
    from .nrt_storage import storage_lock, workspace_state, archive_workspace_metadata

    u = json.loads(Path(update_path).read_text())
    root = u["root"]
    with storage_lock(root):
        result = _finish(update_path)
        archive_workspace_metadata(root, Path(update_path).parent)
        workspace_state(root, Path(update_path).parent, "complete")
        return result


def _main(argv=None):
    p = argparse.ArgumentParser(prog="spires-batch nrt")
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("bootstrap")
    b.add_argument("plan")
    b.add_argument("inventory")
    b.add_argument("--root", required=True)
    q = sub.add_parser("plan")
    q.add_argument("candidates")
    q.add_argument("--root", required=True)
    q.add_argument("--through", required=True, help="Last processing date, inclusive")
    q.add_argument("--output", required=True)
    q.add_argument(
        "--annual-bright-soil",
        help="JSON tile -> water year -> field additions; existing masks are retained",
    )
    q.add_argument(
        "--seed-plan",
        help="Original historical-plan provenance; never bypasses scientific compatibility checks",
    )
    f = sub.add_parser("finish")
    f.add_argument("update")
    r = sub.add_parser("run")
    r.add_argument("update")
    a = sub.add_parser("acquire")
    a.add_argument("--root", required=True)
    a.add_argument("--through", required=True, help="Last processing date, inclusive")
    a.add_argument("--output", required=True)
    a.add_argument("--download", action="store_true")
    a = sub.add_parser("prepare")
    a.add_argument("request")
    a.add_argument("--output", required=True)
    a.add_argument("--receipt", required=True)
    a = sub.add_parser("retrieve")
    a.add_argument("request")
    a.add_argument("--output", required=True)
    a.add_argument("--receipt", required=True)
    a = sub.add_parser("cycle")
    a.add_argument("config")
    a.add_argument("--through", required=True, help="Last processing date, inclusive")
    a.add_argument("--output", required=True)
    a.add_argument("--execute", action="store_true")
    a = sub.add_parser("storage-config")
    a.add_argument("config")
    a.add_argument("--root", required=True)
    a = sub.add_parser("storage-report")
    a.add_argument("--root", required=True)
    a = sub.add_parser("retention-plan")
    a.add_argument("--root", required=True)
    a.add_argument("--output", required=True)
    a = sub.add_parser("retention-apply")
    a.add_argument("report")
    a = sub.add_parser("migrate")
    a.add_argument("--root", required=True)
    a.add_argument("--seed-plan")
    a = sub.add_parser("rebuild")
    a.add_argument("--root", required=True)
    a.add_argument("--output", required=True)
    a.add_argument("--checkpoint")
    a = sub.add_parser("catalog")
    a.add_argument("--root", required=True)
    a.add_argument("--output", required=True)
    a.add_argument("--start", help="First catalog date, inclusive")
    a.add_argument("--stop", help="Last catalog date, inclusive")
    a.add_argument("--publication")
    a = sub.add_parser("abandon")
    a.add_argument("--root", required=True)
    a.add_argument("directory")
    s = sub.add_parser("status")
    s.add_argument("--root", required=True)
    a = p.parse_args(argv)
    if a.command == "storage-config":
        from .nrt_storage import configure

        result = configure(a.root, json.loads(Path(a.config).read_text()))
    elif a.command == "storage-report":
        from .nrt_retention import report_storage

        result = report_storage(a.root)
    elif a.command == "retention-plan":
        from .nrt_retention import plan

        result = plan(a.root, a.output)
    elif a.command == "retention-apply":
        from .nrt_retention import apply

        result = apply(a.report)
    elif a.command == "migrate":
        from .nrt_maintenance import migrate

        result = migrate(a.root, seed_plan=a.seed_plan)
    elif a.command == "rebuild":
        from .nrt_maintenance import rebuild

        result = rebuild(a.root, a.output, checkpoint=a.checkpoint)
    elif a.command == "catalog":
        from .nrt_maintenance import catalog

        result = catalog(
            a.root, a.output, start=a.start, stop=a.stop, publication=a.publication
        )
    elif a.command == "abandon":
        from .nrt_maintenance import abandon

        result = abandon(a.root, a.directory)
    elif a.command == "cycle":
        from .pipeline import cycle

        result = cycle(a.config, a.through, a.output, run=a.execute)
    elif a.command == "retrieve":
        from .retrieval import retrieve

        result = retrieve(a.request, a.output)
        Path(a.receipt).write_text(json.dumps(result, indent=2) + "\n")
    elif a.command == "acquire":
        from .acquisition import acquire

        result = acquire(a.root, a.through, a.output, fetch=a.download)
    elif a.command == "prepare":
        from .inputs import prepare_retained

        result = prepare_retained(a.request, a.output)
        Path(a.receipt).write_text(json.dumps(result, indent=2) + "\n")
    elif a.command == "bootstrap":
        result = bootstrap(a.plan, json.loads(Path(a.inventory).read_text()), a.root)
    elif a.command == "plan":
        result = plan_update(
            a.root,
            json.loads(Path(a.candidates).read_text()),
            a.through,
            a.output,
            annual_bright_soil=(
                json.loads(Path(a.annual_bright_soil).read_text())
                if a.annual_bright_soil
                else None
            ),
            seed_plan=a.seed_plan,
        )
    elif a.command == "run":
        from .nrt_maintenance import run

        result = run(a.update)
    elif a.command == "finish":
        result = finish(a.update)
    else:
        generation, _, ledger = current(a.root)
        result = dict(
            generation=generation["generation"],
            available_through=ledger["available_through"],
            unresolved_nrt=ledger["unresolved_nrt"],
            acquisition_dates=acquisition_dates(
                ledger["inventory"], ledger["available_through"]
            ),
        )
    summary = {
        k: v
        for k, v in result.items()
        if k not in ("inventory", "entries", "candidates", "files", "workspaces")
    }
    if "inventory" in result:
        summary["inventory_count"] = len(result["inventory"])
    print(json.dumps(summary, indent=2))
    return 0


def main(argv=None):
    from .nrt_storage import FullReplayRequired
    import sys

    try:
        return _main(argv)
    except FullReplayRequired as error:
        print(str(error), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
