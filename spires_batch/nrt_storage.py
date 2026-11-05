"""Durable NRT artifacts, monthly catalogs and bounded working metadata.

Numerical state is unchanged. Archives are immutable; the domain publication
pointer remains the commit point. Files on the same filesystem are hard-linked,
not read/copied again. Cross-filesystem transfers are verified before use.
"""

from contextlib import contextmanager
from datetime import date, timedelta, datetime, timezone
import copy
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from .interpolation import digest, file_record, mapped_fields

DEFAULTS = dict(
    working_days=90,
    daily_days=7,
    monthly_months=12,
    success_grace_days=7,
    failed_grace_days=14,
    unresolved_warning_days=30,
    warning_bytes={},
    growth_warning_bytes=0,
)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def mkdir_synced(directory):
    from spires_interpolation.publication import _sync_directory

    directory = Path(directory)
    missing = []
    parent = directory
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for path in reversed(missing):
        path.mkdir(exist_ok=True)
        _sync_directory(path.parent)


def atomic_json(path, value):
    from spires_interpolation.publication import _atomic_json

    path = Path(path)
    mkdir_synced(path.parent)
    _atomic_json(path, value)


def immutable_json(path, value):
    """Create once, preserving stat identities when concurrent writers agree."""
    path = Path(path)
    mkdir_synced(path.parent)
    fd, name = tempfile.mkstemp(prefix=".json-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(name, path)
            from spires_interpolation.publication import _sync_directory

            _sync_directory(path.parent)
        except FileExistsError:
            if json.loads(path.read_text()) != value:
                raise ValueError("Conflicting immutable JSON artifact")
    finally:
        Path(name).unlink(missing_ok=True)


def settings(root):
    root = Path(root).resolve()
    path = root / "storage.json"
    supplied = json.loads(path.read_text()) if path.exists() else {}
    if set(supplied) - set(DEFAULTS) - {"archive_root"}:
        raise ValueError("Unknown NRT storage setting")
    result = {**DEFAULTS, **supplied}
    for name in (
        "working_days",
        "daily_days",
        "monthly_months",
        "success_grace_days",
        "failed_grace_days",
        "unresolved_warning_days",
    ):
        if not isinstance(result[name], int) or result[name] < 1:
            raise ValueError("Retention intervals must be positive integers")
    if result["working_days"] < 66:
        raise ValueError("Working inventory must cover at least 66 days")
    result["archive_root"] = str(
        Path(result.get("archive_root", root / "archive")).resolve()
    )
    return result


@contextmanager
def storage_lock(root, exclusive=False):
    root = Path(root).resolve()
    mkdir_synced(root)
    with (root / ".storage.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


def configure(root, config):
    with storage_lock(root, True):
        root = Path(root)
        old = settings(root)
        candidate = {**old, **config}
        archive = Path(candidate["archive_root"]).resolve()
        home = root.resolve()
        if (
            archive == home
            or archive in home.parents
            or any(
                (
                    archive.is_relative_to(home / name)
                    for name in ("generations", "objects", "cache", "workspaces")
                )
            )
        ):
            raise ValueError("Archive root overlaps mutable managed storage")
        for lease in (home / "workspaces").glob("*.json"):
            workspace = Path(json.loads(lease.read_text())["directory"])
            if archive.is_relative_to(workspace) or workspace.is_relative_to(archive):
                raise ValueError("Archive root overlaps a managed workspace")
        candidate["archive_root"] = str(archive)
        if (root / "current.json").exists() and Path(
            candidate["archive_root"]
        ).resolve() != Path(old["archive_root"]):
            raise ValueError("Cannot change archive_root after stream publication")
        if set(candidate) - set(DEFAULTS) - {"archive_root"}:
            raise ValueError("Unknown NRT storage setting")
        for k in DEFAULTS:
            if k.endswith(("days", "months")) and (
                not isinstance(candidate[k], int) or candidate[k] < 1
            ):
                raise ValueError("Invalid retention interval")
        if candidate["working_days"] < 66:
            raise ValueError("working_days must be at least 66")
        if not isinstance(candidate["warning_bytes"], dict) or any(
            (
                not isinstance(v, int) or v < 0
                for v in candidate["warning_bytes"].values()
            )
        ):
            raise ValueError("Invalid storage warning limits")
        if (
            not isinstance(candidate["growth_warning_bytes"], int)
            or candidate["growth_warning_bytes"] < 0
        ):
            raise ValueError("Invalid growth warning limit")
        atomic_json(root / "storage.json", candidate)
        return settings(root)


def checked(record):
    got = file_record(record["path"])
    if any((got[k] != record[k] for k in ("bytes", "mtime_ns"))):
        raise ValueError(f"Archived file changed: {record['path']}")
    return Path(record["path"])


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def link_or_copy(source, destination, *, snapshot=False):
    """Publish a separate immutable directory entry; no payload read on one FS."""
    source = Path(source)
    destination = Path(destination)
    mkdir_synced(destination.parent)
    before = file_record(source)
    fd, name = tempfile.mkstemp(prefix=".store-", dir=destination.parent)
    os.close(fd)
    os.unlink(name)
    temporary = Path(name)
    try:
        try:
            if snapshot:
                raise OSError(errno.EXDEV, "Snapshot mutable source")
            os.link(source, temporary)
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP):
                raise
            shutil.copy2(source, temporary)
            if sha256(source) != sha256(temporary):
                raise ValueError("Archive transfer checksum mismatch")
        if file_record(source) != before:
            raise ValueError("Source changed during archive transfer")
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
            from spires_interpolation.publication import _sync_directory

            _sync_directory(destination.parent)
        except FileExistsError:
            existing = file_record(destination)
            if (existing["bytes"], existing["mtime_ns"]) != (
                before["bytes"],
                before["mtime_ns"],
            ):
                raise ValueError("Conflicting immutable archive object")
    finally:
        temporary.unlink(missing_ok=True)
    return file_record(destination)


class Store:

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.policy = settings(root)
        self.archive = Path(self.policy["archive_root"])
        self.cache = {}
        self.originals = {}

    def put(self, path, category, *, hot=False):
        path = Path(path).resolve()
        key = (str(path), category, hot)
        if key in self.cache:
            return self.cache[key]
        target_base = self.root if hot else self.archive
        for base in (
            self.root / "cache",
            self.root / "objects",
            self.archive / "objects",
        ):
            if path.is_relative_to(base):
                dest = target_base / "objects" / path.relative_to(base)
                if path == dest:
                    result = file_record(path)
                elif dest.exists():
                    result = file_record(dest)
                    origin = file_record(path)
                    if (result["bytes"], result["mtime_ns"]) != (
                        origin["bytes"],
                        origin["mtime_ns"],
                    ):
                        raise ValueError("Cached artifact differs from archive")
                else:
                    result = link_or_copy(path, dest)
                self.originals[result["path"]] = path
                self.cache[key] = result
                return result
        origin = file_record(path)
        identity = digest(origin)
        dest = target_base / "objects" / category / identity / path.name
        if dest.exists():
            result = file_record(dest)
            if (result["bytes"], result["mtime_ns"]) != (
                origin["bytes"],
                origin["mtime_ns"],
            ):
                raise ValueError("Stored artifact metadata mismatch")
        else:
            mutable = category in ("code", "provenance", "plans") or path.suffix in (
                ".json",
                ".py",
                ".cpp",
                ".toml",
                ".yaml",
                ".yml",
                ".sh",
                ".md",
                ".txt",
                ".ini",
            )
            result = link_or_copy(path, dest, snapshot=mutable)
        self.originals[result["path"]] = path
        self.cache[key] = result
        return result

    def warm(self, path):
        path = Path(path).resolve()
        base = self.archive / "objects"
        if not path.is_relative_to(base):
            raise ValueError("Only archived artifacts may enter the working cache")
        dest = self.root / "cache" / path.relative_to(base)
        key = ("warm", str(path))
        if key not in self.cache:
            original = file_record(path)
            source = self.originals.get(str(path), path)
            value = (
                link_or_copy(source, dest) if not dest.exists() else file_record(dest)
            )
            if (value["bytes"], value["mtime_ns"]) != (
                original["bytes"],
                original["mtime_ns"],
            ):
                raise ValueError("Working cache differs from archive")
            self.cache[key] = value
        return self.cache[key]

    def json(self, value, category="catalogs"):
        path = self.archive / category / (digest(value) + ".json")
        if not path.exists():
            immutable_json(path, value)
        return file_record(path)

    def fields(self, fields):
        fields = copy.deepcopy(fields)
        if fields is not None:
            for field in mapped_fields(fields):
                if "archive_file" in field:
                    checked(field["archive_file"])
                field.setdefault("origin_path", field["path"])
                record = self.put(field["path"], "prepared")
                field.update(path=record["path"], archive_file=record)
        return fields

    def native(self, record):
        if record is None:
            return None
        from .nrt import source_identity

        checked(record)
        result = copy.deepcopy(record)
        origin = source_identity(record)
        result.update(self.put(record["path"], "native"))
        result["origin"] = origin
        if "prepared" in result:
            result["prepared"] = self.fields(result["prepared"])
        return result


def read_catalog(record):
    path = checked(record)
    value = json.loads(path.read_text())
    if path.stem != digest(value):
        raise ValueError("Archive catalog digest mismatch")
    return value


def expand(ledger, *, start=None, stop=None, days=None):
    """Load only requested monthly metadata. Raster payloads are not opened."""
    result = copy.deepcopy(ledger)
    wanted = None if days is None else set(days)

    def use(day):
        return (
            (start is None or day >= start)
            and (stop is None or day <= stop)
            and (wanted is None or day in wanted)
        )

    for month, rec in ledger.get("catalog", {}).items():
        if start and month < start[:7] or (stop and month > stop[:7]):
            continue
        if wanted is not None and (not any((d.startswith(month) for d in wanted))):
            continue
        part = read_catalog(rec)
        for key, value in part["inventory"].items():
            if use(key.split("/")[1]):
                result["inventory"].setdefault(key, value)
        for tile, records in part["daily"].items():
            for day, fields in records.items():
                if use(day):
                    result["template"]["tiles"][tile]["daily"].setdefault(day, fields)
        for key, value in part["products"].items():
            if use(key.split("/")[1]):
                result["products"].setdefault(key, value)
    return result


class FullReplayRequired(RuntimeError):
    """An unattended update cannot continue without replaying its full history."""


def stop_full_replay(root, ledger, through, directory, *, reason):
    """Report history sizes from catalogs/stat calls, then stop before payload IO."""
    history = expand(ledger, start=ledger["history_start"], stop=through)
    store = Store(root)
    files = {}
    for tile in history["template"]["tiles"].values():
        for day, observation in tile["daily"].items():
            if not ledger["history_start"] <= day <= through:
                continue
            for field in mapped_fields(observation):
                archived = field.get("archive_file", {})
                source = Path(archived.get("path", field["path"])).resolve()
                key = str(source)
                if key in files:
                    continue
                try:
                    size = source.stat().st_size
                except OSError:
                    size = archived.get("bytes")
                cached = False
                archive_objects = store.archive / "objects"
                if source.is_relative_to(archive_objects):
                    cache = store.root / "cache" / source.relative_to(archive_objects)
                    try:
                        source_stat, cache_stat = source.stat(), cache.stat()
                        cached = (source_stat.st_size, source_stat.st_mtime_ns) == (
                            cache_stat.st_size,
                            cache_stat.st_mtime_ns,
                        )
                    except OSError:
                        pass
                files[key] = dict(path=key, bytes=size, cached=cached)
    records = sorted(
        files.values(), key=lambda record: (-(record["bytes"] or 0), record["path"])
    )
    report = dict(
        schema_version=1,
        status="full_replay_required",
        reason=reason,
        stream_root=str(Path(root).resolve()),
        tiles=sorted(history["template"]["tiles"]),
        history_start=ledger["history_start"],
        through=through,
        history_input_bytes=sum(record["bytes"] or 0 for record in records),
        uncached_history_bytes=sum(
            record["bytes"] or 0 for record in records if not record["cached"]
        ),
        unknown_size_files=sum(record["bytes"] is None for record in records),
        files=records,
        note="Logical prepared-history file sizes, not a transfer authorization. Uncached bytes are an estimate before staging, excluding static assets. Shared files are counted once; partial-file reads may require the whole file. No full-history payload was copied or replayed by this check. Review recovery manually; no automatic retry or approval interface is provided.",
    )
    durable = store.archive / "stops" / (digest(report) + ".json")
    atomic_json(durable, report)
    atomic_json(Path(directory) / "full-replay-required.json", report)
    largest = records[0] if records else None
    detail = (
        f" Largest history file: {largest['path']} ({largest['bytes']} bytes)."
        if largest
        else ""
    )
    raise FullReplayRequired(
        f"NRT stopped: {reason}. Full replay would cover {ledger['history_start']} through {through}; "
        f"{len(records)} prepared-history files, {report['history_input_bytes']} known bytes "
        f"({report['history_input_bytes'] / 1024**3:.2f} GiB), "
        f"{report['uncached_history_bytes']} estimated uncached bytes, "
        f"{report['unknown_size_files']} unknown-size files.{detail} "
        f"No full-history transfer or replay was started. Report: {durable}"
    )


def archive_ledger(root, ledger):
    """COW monthly indexes preserve all vintages; only 90-day+unresolved maps stay hot."""
    store = Store(root)
    result = copy.deepcopy(ledger)
    parts = {}
    catalog = dict(ledger.get("catalog", {}))

    def part(month):
        if month not in parts:
            parts[month] = (
                read_catalog(catalog[month])
                if month in catalog
                else dict(inventory={}, daily={}, products={})
            )
        return parts[month]

    for key, rec in ledger["inventory"].items():
        part(key.split("/")[1][:7])["inventory"][key] = store.native(rec)
    for tile, fields in ledger["template"]["tiles"].items():
        for day, fields in fields["daily"].items():
            part(day[:7])["daily"].setdefault(tile, {})[day] = store.fields(fields)
    for key, rec in ledger["products"].items():
        value = copy.deepcopy(rec)
        value["file"] = store.put(checked(rec["file"]), "products")
        part(key.split("/")[1][:7])["products"][key] = value
    for month, value in parts.items():
        catalog[month] = store.json(value)
    cutoff = (
        date.fromisoformat(ledger["available_through"])
        - timedelta(days=store.policy["working_days"] - 1)
    ).isoformat()
    unresolved = {
        key.split("/")[1]
        for key, value in ledger["inventory"].items()
        if value is None or value["source_stream"] == "nrt"
    }
    result["inventory"] = {}
    result["products"] = {}
    for tile in result["template"]["tiles"].values():
        tile["daily"] = {}
    for value in parts.values():
        for key, rec in value["inventory"].items():
            if key.split("/")[1] >= cutoff or key.split("/")[1] in unresolved:
                result["inventory"][key] = rec
        for tile, records in value["daily"].items():
            result["template"]["tiles"][tile]["daily"].update(
                {d: f for d, f in records.items() if d >= cutoff or d in unresolved}
            )
        result["products"].update(
            {k: v for k, v in value["products"].items() if k.split("/")[1] >= cutoff}
        )
    for rec in result["inventory"].values():
        if rec is not None:
            rec.update(store.warm(rec["path"]))
            for field in mapped_fields(rec.get("prepared", {})):
                field["path"] = store.warm(field["path"])["path"]
    for fields in result["template"]["tiles"].values():
        for field in mapped_fields(fields["daily"]):
            field["path"] = store.warm(field["path"])["path"]
    result.update(
        schema_version=2,
        catalog=catalog,
        working_start=max(cutoff, ledger["history_start"]),
    )
    result["unresolved_nrt"] = sorted(
        (
            k
            for k, v in result["inventory"].items()
            if v is not None and v["source_stream"] == "nrt"
        )
    )
    return result


def checkpoint_coverage(path):
    """Read tiny date vectors/headers only, never raster checkpoint arrays."""
    import h5py

    with h5py.File(path) as h:
        if not h.attrs.get("complete"):
            raise ValueError("Incomplete checkpoint")
        if "restart_coverage" in h.attrs:
            return json.loads(h.attrs["restart_coverage"])
        stripes = []
        for group in h.values():
            row = {}
            for name in ("checkpoint", "age_history", "property_history_checkpoint"):
                g = group[name]
                d = g["dates"][:]
                if not len(d) or any(
                    (int(b) - int(a) != 1 for a, b in zip(d[:-1], d[1:]))
                ):
                    raise ValueError("Nonconsecutive checkpoint dates")
                row[name] = [int(d[0]), int(d[-1])]
            if group["checkpoint"].attrs.get("identity") != h.attrs["state_identity"]:
                raise ValueError("Mixed checkpoint identities")
            stripes.append(row)
        if not stripes:
            raise ValueError("Empty checkpoint")
        return dict(schema_version=1, stripes=stripes)


def can_restart(coverage, first, through, history_start):
    import numpy as np

    day = lambda s: int(np.datetime64(s, "D").astype("i8"))
    first, through, origin = map(day, (first, through, history_start))
    anchor = first - 1
    needed = max(origin, min(first - 21, through - 65))
    for stripe in coverage["stripes"]:
        a, b = stripe["checkpoint"]
        if not a <= anchor <= b or (a > first - 45 and a != origin):
            return False
        a, b = stripe["age_history"]
        if not a <= anchor <= b:
            return False
        a, b = stripe["property_history_checkpoint"]
        if a > needed or b < anchor:
            return False
    return True


def durable_ledger(root, ledger):
    store = Store(root)
    result = copy.deepcopy(ledger)
    result["inventory"] = {k: store.native(v) for k, v in ledger["inventory"].items()}
    for tile, fields in result["template"]["tiles"].items():
        fields["daily"] = {d: store.fields(v) for d, v in fields["daily"].items()}
    return result


def publication_archive(root, generation):
    """Preserve scientific provenance/products independently of recovery payloads."""
    from spires_interpolation.publication import artifact_paths

    generation = {k: v for k, v in generation.items() if k != "reused"}
    store = Store(root)
    archived = store.archive / "publications" / (generation["generation"] + ".json")
    if archived.exists():
        saved = json.loads(archived.read_text())
        if {
            k: v for k, v in saved["generation"].items() if k != "reused"
        } != generation:
            raise ValueError("Publication archive manifest conflict")
        return archived
    paths = artifact_paths(root, generation)
    ledger = json.loads(paths["ledger"].read_text())
    if ledger.get("schema_version") != 2:
        raise ValueError("Migrate legacy stream before retention")
    record = dict(generation=generation, ledger=durable_ledger(root, ledger))
    path = store.archive / "publications" / (generation["generation"] + ".json")
    if path.exists():
        if json.loads(path.read_text()) != record:
            raise ValueError("Publication archive conflict")
    else:
        immutable_json(path, record)
    return path


def recoveries(root):
    """Yield hot and cold recovery metadata without inspecting raster payloads."""
    from spires_interpolation.publication import artifact_paths

    root = Path(root)
    seen = set()
    for path in (root / "generations").glob("*/manifest.json"):
        g = json.loads(path.read_text())
        paths = artifact_paths(root, g)
        ledger = json.loads(paths["ledger"].read_text())
        prior = (
            Path(settings(root)["archive_root"])
            / "publications"
            / (g["generation"] + ".json")
        )
        if ledger.get("schema_version") != 2 and prior.exists():
            ledger = json.loads(prior.read_text())["ledger"]
        seen.add(g["generation"])
        yield (g, paths, ledger)
    for path in (Path(settings(root)["archive_root"]) / "recoveries").glob("*.json"):
        value = json.loads(path.read_text())
        g = value["generation"]
        if g["generation"] in seen:
            continue
        paths = {k: checked(v) for k, v in value["checkpoints"].items()}
        yield (g, paths, value["ledger"])


def archive_provenance(root, plan_path):
    """Archive exact plans, code and static assets without pinning restart arrays."""
    store = Store(root)
    p = json.loads(Path(plan_path).read_text())
    r = p["request"]
    excluded = set(r.get("seed", {}).values())
    assets = {}
    observation_paths = {
        v["path"]
        for fields in r["tiles"].values()
        for v in mapped_fields(fields["daily"])
    }
    for path in p["inputs"]:
        if path not in excluded and path not in observation_paths:
            assets[path] = store.put(path, "assets")
    code = {}
    for path, expected in p["implementations"].items():
        if Path(path).is_file() and sha256(path) == expected:
            code[path] = dict(file=store.put(path, "code"), sha256=expected)
        else:
            code[path] = dict(sha256=expected, unavailable=True)
    return store.json(dict(plan=p, assets=assets, code=code), "provenance")


def lease_path(root, directory):
    directory = Path(directory).resolve()
    root = Path(root).resolve()
    if (
        directory == root
        or directory in root.parents
        or directory.is_relative_to(root / "generations")
        or directory.is_relative_to(root / "objects")
        or directory.is_relative_to(root / "cache")
        or directory.is_relative_to(Path(settings(root)["archive_root"]))
    ):
        raise ValueError("Unsafe NRT workspace location")
    for p in (root / "workspaces").glob("*.json"):
        value = json.loads(p.read_text())
        old = Path(value["directory"])
        if directory.is_relative_to(old):
            return p
        if old.is_relative_to(directory):
            raise ValueError("Workspace would enclose an existing registered workspace")
    return root / "workspaces" / (digest(str(directory)) + ".json")


def register_workspace(root, directory, state="planned"):
    p = lease_path(root, directory)
    old = json.loads(p.read_text()) if p.exists() else {}
    owned = Path(old.get("directory", str(Path(directory).resolve())))
    marker = owned / ".nrt-workspace.json"
    owner = dict(root=str(Path(root).resolve()), directory=str(owned))
    if marker.exists():
        if json.loads(marker.read_text()) != owner:
            raise ValueError("Workspace belongs to another stream")
    else:
        if owned.exists() and any(owned.iterdir()):
            raise ValueError("Refusing to manage a nonempty unregistered workspace")
        owned.mkdir(parents=True, exist_ok=True)
        atomic_json(marker, owner)
    value = dict(
        old,
        directory=old.get("directory", str(Path(directory).resolve())),
        state=state,
        updated_utc=utcnow(),
    )
    atomic_json(p, value)
    return p


def workspace_state(root, directory, state):
    register_workspace(root, directory, state)


def archive_workspace_metadata(root, directory):
    """Small requests/receipts survive cleanup, including dated R0 bindings."""
    store = Store(root)
    saved = {}
    assets = {}
    for path in Path(directory).rglob("*.json"):
        if path.is_symlink():
            raise ValueError("Symlink in managed workspace")
        saved[str(path)] = store.put(path, "provenance")
        try:
            value = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if path.name.endswith("_retrieval.json"):
            for field in mapped_fields(value.get("background", {})):
                assets[field["path"]] = store.put(field["path"], "assets")
            for name in ("lut",):
                if name in value:
                    assets[value[name]] = store.put(value[name], "assets")
    return store.json(dict(files=saved, assets=assets), "workspace-records")


def archive_configuration(root, path):
    """Pin dated R0 recipes, masks, model release and configuration outside workspaces."""
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    store = Store(root)
    sources = set()

    def visit(value, key=""):
        if isinstance(value, dict):
            for k, v in value.items():
                visit(v, k)
        elif isinstance(value, list):
            for item in value:
                visit(item, key)
        elif isinstance(value, str) and (
            key
            in (
                "path",
                "lut",
                "cloud_model",
                "snow_library",
                "spatial_library",
                "albedo_lut",
            )
            or key.endswith("_source")
        ):
            candidate = Path(value)
            candidate = (
                (path.parent / candidate).resolve()
                if not candidate.is_absolute()
                else candidate
            )
            if candidate.is_file():
                sources.add(str(candidate))

    visit(config)
    if "cloud_model" in config:
        model = Path(config["cloud_model"])
        if not model.is_absolute():
            model = path.parent / model

        def model_paths(value):
            if isinstance(value, dict):
                if "path" in value:
                    dependency = (model.parent / value["path"]).resolve()
                    if not dependency.is_file():
                        raise FileNotFoundError(dependency)
                    sources.add(str(dependency))
                for child in value.values():
                    model_paths(child)
            elif isinstance(value, list):
                for child in value:
                    model_paths(child)

        model_paths(json.loads(model.read_text()))
    assets = {source: store.put(source, "assets") for source in sorted(sources)}
    return store.json(
        dict(config=store.put(path, "provenance"), assets=assets), "configurations"
    )
