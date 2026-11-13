"""Compute-allocation coordinator for acquire -> cloud -> retrieval -> revision.

Configuration names deployed interpreters/model and explicit tile/WY retrieval
assets. No scheduler submission or live cron modification occurs implicitly.
"""

import copy
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, wait, FIRST_COMPLETED
import multiprocessing
import json
import os
from pathlib import Path
import subprocess
import hashlib
import sys
import time
import re
from .configuration import require_configuration
from .nrt import (
    current,
    choose_sources,
    day_context,
    plan_update,
    finish,
    extend_annual_context,
)
from .interpolation import digest, load_plan, execute, implementations, stage_resources


def revision_workers(plan, configuration):
    """Fit stage workers inside the actual allocation, including native threads.

    Scheduler concurrency caps jobs across a campaign; it never authorizes this
    process to exceed its own CPU/memory allocation. Reserve 2 GiB for the parent.
    """
    limits = configuration.get("revision_concurrency", {})
    if not isinstance(limits, dict) or set(limits) - set(plan["stage_order"]):
        raise ValueError("revision_concurrency must map interpolation stages to worker limits")
    if any(type(value) is not int or value < 1 for value in limits.values()):
        raise ValueError("Revision worker limits must be positive integers")
    cpus = int(os.environ.get("SLURM_CPUS_PER_TASK", "1"))
    memory = os.environ.get("SLURM_MEM_PER_NODE")
    if memory is None and "SLURM_MEM_PER_CPU" in os.environ:
        memory = int(os.environ["SLURM_MEM_PER_CPU"]) * cpus
    if memory is None or int(memory) <= 0:
        raise ValueError("Revision execution requires an explicit Slurm memory allocation")
    available = int(memory) * 1024**2 - 2 * 1024**3
    result = {}
    units = {"K": 1024, "M": 1024**2, "": 1024**2, "G": 1024**3, "T": 1024**4}
    for stage in plan["stage_order"]:
        resource = stage_resources(plan["request"]["scheduler"], stage)
        match = re.fullmatch(r"([0-9]+)([KMGT]?)", resource["memory"])
        if match is None or int(match[1]) == 0:
            raise ValueError("Revision tasks require bounded scheduler memory budgets")
        per_worker = int(match[1]) * units[match[2]]
        count = min(cpus // resource["cpus"], available // per_worker,
                    resource["concurrency"], limits.get(stage, resource["concurrency"]))
        if count < 1:
            raise ValueError(f"Allocation cannot fit one {stage} worker plus 2 GiB coordinator headroom")
        result[stage] = min(count, len(plan["tasks"][stage]))
    return result


def _revision_task(path, stage, index):
    # Separate processes isolate NetCDF/HDF5 and serial_task_log stdout redirection.
    # Native spatial threads are explicitly supplied by execute's stage profile.
    execute(path, stage, index)


def _parallel_stage(path, stage, count, workers):
    if workers == 1:
        for index in range(count):
            _revision_task(path, stage, index)
        return
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        pending = set()
        indices = iter(range(count))
        for _ in range(min(count, workers)):
            # Consume the same iterator used to replenish the pool so the
            # initial tasks are not submitted again while still running.
            index = next(indices)
            pending.add(pool.submit(_revision_task, path, stage, index))
        try:
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                # Observe every completed result before launching more work.
                for task in done:
                    task.result()
                for _ in done:
                    index = next(indices, None)
                    if index is not None:
                        pending.add(pool.submit(_revision_task, path, stage, index))
        except BaseException:
            for task in pending:
                task.cancel()
            raise


def invoke(python, code, *args, env=None, log=None):
    from .workflow_logging import task_log

    if log is None:
        subprocess.run([python, "-c", code, *map(str, args)], check=True, env=env)
        return
    with task_log(**log) as handle:
        subprocess.run([python, "-u", "-c", code, *map(str, args)], check=True, env=env,
                       stdout=handle, stderr=subprocess.STDOUT)


def retrieval_concurrency(config, tile_count):
    """Check CPU and total preparation-memory reservations for one date.

    Default to all available tiles, with 6 GiB each and 2 GiB coordinator
    headroom, unless preparation_memory_gib supplies a direct total budget.
    Fail before staging/preparation rather than silently serialize an
    undersized Slurm allocation. Memory budgets are reservations, not RSS limits.
    """
    concurrency = config.get("retrieval_concurrency", max(1, tile_count))
    memory_gib = config.get("retrieval_memory_gib", 6)
    if "preparation_memory_gib" in config and "retrieval_memory_gib" in config:
        raise ValueError(
            "Choose preparation_memory_gib (total) or retrieval_memory_gib "
            "(per tile), not both"
        )
    for name, value in (
        ("retrieval_concurrency", concurrency),
        ("retrieval_memory_gib", memory_gib),
    ):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if "preparation_memory_gib" in config:
        total_gib = config["preparation_memory_gib"]
        if type(total_gib) is not int or total_gib < 1:
            raise ValueError("preparation_memory_gib must be a positive integer")
    if tile_count == 0:
        return 1
    concurrency = min(concurrency, max(1, tile_count))
    cpus = os.environ.get("SLURM_CPUS_PER_TASK")
    if cpus is not None and int(cpus) < concurrency:
        raise ValueError(
            f"Retrieval requires --cpus-per-task={concurrency} or greater; "
            "reduce retrieval_concurrency explicitly to run fewer tiles at once"
        )
    memory_mb = os.environ.get("SLURM_MEM_PER_NODE")
    if memory_mb is None and cpus is not None:
        per_cpu = os.environ.get("SLURM_MEM_PER_CPU")
        if per_cpu is not None:
            memory_mb = int(per_cpu) * int(cpus)
    required_gib = config.get("preparation_memory_gib", concurrency * memory_gib + 2)
    budget_description = (
        "total date-preparation budget, including coordinator and cloud inference"
        if "preparation_memory_gib" in config
        else f"{concurrency} tiles at {memory_gib} GiB plus 2 GiB headroom"
    )
    if memory_mb is not None and 0 < int(memory_mb) < required_gib * 1024:
        raise ValueError(
            f"Preparation requires --mem={required_gib}G or greater "
            f"({budget_description})"
        )
    return concurrency


def prepare_date(config, inventory, day, directory):
    """Rebuild the entire same-day domain so cloud halos and donors agree."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    tiles = sorted(config["tiles"])
    concurrency = retrieval_concurrency(
        config, sum(inventory.get(tile + "/" + day) is not None for tile in tiles)
    )
    context = day_context(inventory, tiles, day)
    year = int(day[:4]) + (int(day[5:7]) >= 10)
    for tile in tiles:
        if (
            inventory.get(tile + "/" + day) is not None
            and str(year) not in config["tiles"][tile]
        ):
            raise ValueError(
                f"Missing explicit retrieval assets for {tile}/WY{year}; build/register its annual R0 and context first"
            )
    output = directory / digest(
        dict(context=context, config=config, implementations=implementations())
    )
    output.mkdir(parents=True, exist_ok=True)
    inputs = {}
    for tile in tiles:
        source = inventory.get(tile + "/" + day)
        if source is None:
            continue
        matched = output / (tile + "_matched.h5")
        if not matched.exists():
            invoke(
                config["cloud_python"],
                "import sys; from viirs_cloudmask_batch.matched import prepare_matched; prepare_matched(sys.argv[1],sys.argv[2])",
                source["path"],
                matched,
                log=dict(directory=directory, stage="reflectance", tile=tile, day=day),
            )
        inputs[tile] = str(matched)
    if not inputs:
        return []
    (output / "cloud-inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    invoke(
        config["cloud_python"],
        'import sys,json,torch,os; from viirs_cloudmask_batch.matched import predict_matched_date; torch.set_num_threads(int(os.environ.get("SLURM_CPUS_PER_TASK","1"))); predict_matched_date(json.load(open(sys.argv[1])),sys.argv[2],sys.argv[3])',
        output / "cloud-inputs.json",
        output / "cloud",
        config["cloud_model"],
        log=dict(directory=directory, stage="cloud", day=day),
    )
    clouds = {
        t: dict(
            cloud=str(output / "cloud" / f"{t}_cloud_mask.tif"),
            context=str(output / "cloud" / f"{t}_cloud_context_incomplete.tif"),
        )
        for t in inputs
    }
    retrieval_env = dict(os.environ)
    retrieval_env.update(
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        NUMEXPR_NUM_THREADS="1",
        VECLIB_MAXIMUM_THREADS="1",
    )

    def prepare_tile(tile):
        try:
            r = copy.deepcopy(config["tiles"][tile][str(year)])
        except KeyError:
            raise ValueError(
                f"Missing explicit retrieval assets for {tile}/WY{year}; build/register its annual R0 and context first"
            ) from None
        r.update(
            source=inventory[tile + "/" + day], prepared_context=context, clouds=clouds
        )
        request = output / (tile + "_retrieval.json")
        request.write_text(json.dumps(r, indent=2) + "\n")
        receipt = output / (tile + "_prepared.json")
        invoke(
            config["science_python"],
            'import sys,json; from pathlib import Path; from spires_batch.retrieval import retrieve; r=retrieve(sys.argv[1],sys.argv[2]); Path(sys.argv[3]).write_text(json.dumps(r,indent=2)+"\\n")',
            request,
            output / (tile + "_prepared.h5"),
            receipt,
            env=retrieval_env,
            log=dict(directory=directory, stage="retrieval", tile=tile, day=day),
        )
        return json.loads(receipt.read_text())

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        return list(executor.map(prepare_tile, inputs))


def _cycle(config, through, directory, *, run=False, resolved=None):
    """Prepare one versioned daily update; optionally execute in this allocation."""
    if not os.environ.get("SLURM_JOB_ID"):
        raise RuntimeError("The full native-grid cycle belongs in a compute allocation")
    config_path = Path(config).resolve()
    config = resolved if resolved is not None else json.loads(config_path.read_text())
    require_configuration(config, "nrt_cycle")
    root = config["stream_root"]
    directory = Path(directory).resolve()
    annual = config.get("annual_bright_soil", {})
    for years in annual.values():
        for field in years.values():
            path = Path(field["path"])
            field["path"] = str(
                (config_path.parent / path).resolve()
                if not path.is_absolute()
                else path
            )
    from .acquisition import acquire

    _, _, ledger = current(root)
    tiles = sorted(config["tiles"])
    policy = config.get("observation_policy")
    if not policy or policy != ledger["template"]["asset_provenance"].get(
        "observation_policy"
    ):
        raise ValueError(
            "Cycle observation policy must match the historical bootstrap policy"
        )
    if (
        policy.get("cloud_model_sha256")
        != hashlib.sha256(Path(config["cloud_model"]).read_bytes()).hexdigest()
    ):
        raise ValueError("Cloud model differs from the registered observation policy")
    if tiles != sorted(ledger["template"]["tiles"]):
        raise ValueError("Cycle tile domain differs from historical state")
    if through < ledger["available_through"]:
        raise ValueError("Availability may not move backwards")
    template = extend_annual_context(ledger["template"], annual)
    if run:
        # Check all stage budgets before any download or inversion work.
        stages = ["screen", "spatial", "properties", "export"]
        revision_workers(dict(request=template, stage_order=stages,
                              tasks={stage: [None] for stage in stages}), config)
    from datetime import date, timedelta

    day = date.fromisoformat(ledger["available_through"]) + timedelta(days=1)
    end = date.fromisoformat(through)
    years = set()
    while day <= end:
        years.add(str(day.year + (day.month >= 10)))
        day += timedelta(days=1)
    for tile in tiles:
        for year in sorted(years):
            if year not in template["tiles"][tile]["bright_soil"]:
                raise ValueError(
                    f"Missing annual bright-soil mask: {tile}/WY{year}; register annual_bright_soil without rebuilding history"
                )
            if year not in config["tiles"][tile]:
                raise ValueError(
                    f"Missing explicit retrieval assets for {tile}/WY{year}; retain prior-year recipes for revisions"
                )
    acquired = acquire(
        root,
        through,
        directory / "acquisition",
        collection=config.get("collection", "002"),
        fetch=True,
    )
    from .nrt_storage import expand

    ledger = expand(ledger, days={rec["date"] for rec in acquired["candidates"]})
    inventory, changed = choose_sources(
        ledger["inventory"],
        acquired["candidates"],
        tiles=tiles,
        collection=config.get("collection", "002"),
        through=through,
        history_start=ledger["history_start"],
        calendar_start=(
            date.fromisoformat(ledger["available_through"]) + timedelta(days=1)
        ).isoformat(),
    )
    candidates = []
    for day in sorted({key.split("/")[1] for key in changed}):
        candidates.extend(
            prepare_date(config, inventory, day, directory / "prepared" / day)
        )
    update = plan_update(
        root,
        candidates,
        through,
        directory / "revision",
        collection=config.get("collection", "002"),
        annual_bright_soil=annual,
        seed_plan=config.get("seed_plan"),
    )
    if update.get("status") == "unchanged" or not run:
        return update
    return _execute_revision(directory / "revision/update.json", root, through, config)


def _execute_revision(path, root, through, configuration=None):
    update = json.loads(Path(path).read_text())
    identity = update.pop("identity")
    if digest(update) != identity or update["root"] != str(Path(root).resolve()) or update["through"] != through:
        raise ValueError("Saved NRT revision differs from the requested update")
    generation, _, _ = current(root)
    if generation["request"] == dict(update=identity):
        return finish(path)
    if generation["generation"] != update["expected_parent"]:
        raise ValueError("Another NRT revision committed; automatic retry cannot replace it")
    plan = load_plan(update["plan"])
    workers = revision_workers(plan, configuration or {})
    # Set these before spawning workers so numerical libraries see the limits
    # at import time. The spatial kernel still gets its explicit thread count.
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = "1"
    from .nrt_storage import atomic_json
    metrics_path = Path(path).parent / "execution-metrics.json"
    metrics = dict(status="running", python=sys.executable, workers=workers, stages={})
    for stage in plan["stage_order"]:
        count = len(plan["tasks"][stage])
        began = time.perf_counter()
        metrics["stages"][stage] = dict(status="running", tasks=count, workers=workers[stage])
        atomic_json(metrics_path, metrics)
        try:
            if count:
                _parallel_stage(update["plan"], stage, count, workers[stage])
        except BaseException:
            metrics["status"] = metrics["stages"][stage]["status"] = "failed"
            atomic_json(metrics_path, metrics)
            raise
        metrics["stages"][stage].update(status="complete", seconds=time.perf_counter() - began)
        atomic_json(metrics_path, metrics)
    began = time.perf_counter()
    metrics["status"] = "publishing"
    atomic_json(metrics_path, metrics)
    try:
        result = finish(path)
    except BaseException:
        metrics.update(status="failed", publication_seconds=time.perf_counter() - began)
        atomic_json(metrics_path, metrics)
        raise
    metrics.update(status="complete", publication_seconds=time.perf_counter() - began)
    atomic_json(metrics_path, metrics)
    return result


def cycle(config, through, directory, *, run=False):
    if not os.environ.get("SLURM_JOB_ID"):
        raise RuntimeError("The full native-grid cycle belongs in a compute allocation")
    from .nrt_storage import (
        storage_lock,
        register_workspace,
        workspace_state,
        archive_workspace_metadata,
        Store,
    )

    config_path = Path(config).resolve()
    configuration = json.loads(config_path.read_text())
    require_configuration(configuration, "nrt_cycle")
    from .scheduler_policy import resolve_cycle_policy

    configuration = resolve_cycle_policy(configuration, config_path.parent)
    root = configuration["stream_root"]
    with storage_lock(root):
        register_workspace(root, directory, "running")
        try:
            from .nrt_storage import archive_configuration

            archive_configuration(root, config_path)
            if "scheduler" in configuration:
                from .nrt_storage import atomic_json

                resolved_path = Path(directory) / "resolved-cycle.json"
                if resolved_path.exists() and json.loads(resolved_path.read_text()) != configuration:
                    raise ValueError("NRT workspace configuration changed; cannot retry")
                atomic_json(resolved_path, configuration)
                archive_configuration(root, resolved_path)
            revision = Path(directory) / "revision/update.json"
            result = (_execute_revision(revision, root, through, configuration) if run and revision.exists() else
                      _cycle(config_path, through, directory, run=run, resolved=configuration))
            if run or result.get("status") == "unchanged":
                archive_workspace_metadata(root, directory)
                workspace_state(root, directory, "complete")
            else:
                workspace_state(root, directory, "planned")
            return result
        except Exception:
            workspace_state(root, directory, "failed")
            raise
