"""Reference-aware NRT retention: reviewed plans, tiered recovery, safe cleanup.

Only registered workspaces and owned checkpoint/cache files can be deleted.
Scientific archive payloads and publication provenance are never expired here.
"""

from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
from .interpolation import digest, file_record
from .nrt_storage import (
    settings,
    storage_lock,
    atomic_json,
    utcnow,
    Store,
    checked,
    publication_archive,
    archive_workspace_metadata,
)


def instant(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def files_under(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"Symlink in managed storage: {path}")
    if not path.exists():
        return []
    out = []
    for parent, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            p = Path(parent) / name
            if p.is_symlink():
                raise ValueError(f"Symlink in managed storage: {p}")
        out.extend((Path(parent) / name for name in files))
    return sorted(out)


def snapshot(path):
    rec = file_record(path)
    s = Path(path).stat()
    return dict(rec, device=s.st_dev, inode=s.st_ino)


def refs(value):
    """File references in ledger/cached field maps, excluding historical origins."""
    if isinstance(value, dict):
        if "path" in value:
            yield str(Path(value["path"]).resolve())
        for k, v in value.items():
            if k != "origin":
                yield from refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from refs(v)


def selection(generations, current, policy, now):
    """Deduplicate coincident daily, monthly and September 30 checkpoints."""
    hot = {current}
    cold = set()
    daily_recoveries = {}
    monthly = {}
    annual = {}
    for name, value in generations.items():
        g = value["generation"]
        when = instant(g["created_utc"])
        day = value["ledger"]["available_through"]
        if when > now:
            hot.add(name)
            continue
        key = (day, g["created_utc"])
        if day not in daily_recoveries or key > daily_recoveries[day][0]:
            daily_recoveries[day] = (key, name)
        month = day[:7]
        if month not in monthly or key > monthly[month][0]:
            monthly[month] = (key, name)
        if day[5:] == "09-30":
            year = day[:4]
            if year not in annual or key > annual[year][0]:
                annual[year] = (key, name)
    today = now.date()
    for day, (_, name) in daily_recoveries.items():
        if (today - datetime.fromisoformat(day).date()).days <= policy["daily_days"]:
            hot.add(name)
    month_number = today.year * 12 + today.month
    for month, (_, name) in monthly.items():
        year, m = map(int, month.split("-"))
        if 0 <= month_number - (year * 12 + m) < policy["monthly_months"]:
            cold.add(name)
    cold.update((name for _, name in annual.values()))
    return (hot, cold - hot)


def collect(root, *, as_of=None):
    """Caller holds storage lock. Reads metadata and file stats, never raster data."""
    from .nrt import current
    from spires_interpolation.publication import artifact_paths

    root = Path(root).resolve()
    policy = settings(root)
    archive = Path(policy["archive_root"])
    now = instant(as_of or utcnow())
    g, _, ledger = current(root)
    if ledger.get("schema_version") != 2:
        raise ValueError("Archive and compact the current-science ledger with nrt migrate before retention")
    workspaces = {
        p.name: json.loads(p.read_text()) for p in (root / "workspaces").glob("*.json")
    }
    active = [
        v["directory"]
        for v in workspaces.values()
        if v["state"] in ("planned", "running")
    ]
    generations = {}
    for p in (root / "generations").glob("*/manifest.json"):
        old = json.loads(p.read_text())
        paths = artifact_paths(root, old)
        ld = json.loads(paths["ledger"].read_text())
        if ld.get("schema_version") != 2:
            archived = archive / "publications" / (old["generation"] + ".json")
            if not archived.exists():
                continue
            ld = json.loads(archived.read_text())["ledger"]
        generations[old["generation"]] = dict(generation=old, ledger=ld, hot=True)
    for p in (archive / "recoveries").glob("*.json"):
        value = json.loads(p.read_text())
        generations.setdefault(
            value["generation"]["generation"], dict(value, hot=False)
        )
    hot, cold = selection(generations, g["generation"], policy, now)
    candidates = {}
    evict = []
    drop = []
    workspace_archives = []
    if not active:
        protected = set(refs(ledger))
        for name in hot:
            if name in generations:
                protected.update(refs(generations[name]["ledger"]))
        for name, value in generations.items():
            if name in hot:
                continue
            if value["hot"]:
                evict.append(dict(generation=name, recovery=name in cold))
                for path in files_under(root / "generations" / name):
                    candidates[str(path)] = snapshot(path)
            elif name not in cold:
                drop.append(name)
                path = archive / "recoveries" / (name + ".json")
                candidates[str(path)] = snapshot(path)
        for filename, w in workspaces.items():
            grace = (
                policy["success_grace_days"]
                if w["state"] == "complete"
                else policy["failed_grace_days"]
            )
            if w["state"] not in ("complete", "failed", "abandoned") or now - instant(
                w["updated_utc"]
            ) < timedelta(days=grace):
                continue
            directory = Path(w["directory"])
            paths = files_under(directory)
            if any((str(p.resolve()) in protected for p in paths)):
                continue
            workspace_archives.append(str(directory))
            for path in paths:
                candidates[str(path)] = snapshot(path)
        pinned_inodes = set()
        for name in hot:
            if name not in generations or not generations[name]["hot"]:
                continue
            for path in files_under(root / "generations" / name):
                if path.suffix == ".h5":
                    s = path.stat()
                    pinned_inodes.add((s.st_dev, s.st_ino))
        cold_paths = set()
        for name in cold:
            value = generations[name]
            if not value["hot"]:
                cold_paths.update(refs(value["checkpoints"]))
        for path in files_under(root / "objects" / "checkpoints"):
            s = path.stat()
            if (s.st_dev, s.st_ino) not in pinned_inodes:
                candidates[str(path)] = snapshot(path)
        for path in files_under(archive / "objects" / "checkpoints"):
            if (
                not any((v["recovery"] for v in evict))
                and str(path.resolve()) not in cold_paths
            ):
                candidates[str(path)] = snapshot(path)
        for path in files_under(root / "cache"):
            if str(path.resolve()) not in protected:
                candidates[str(path)] = snapshot(path)
    result = dict(
        schema_version=1,
        root=str(root),
        as_of=now.isoformat(),
        expected_parent=g["generation"],
        policy=policy,
        workspaces=workspaces,
        active_workspaces=active,
        keep_hot=sorted(hot),
        keep_cold=sorted(cold),
        evict_generations=evict,
        drop_recoveries=drop,
        archive_workspaces=workspace_archives,
        files=sorted(candidates.values(), key=lambda v: v["path"]),
        candidate_logical_bytes=sum((v["bytes"] for v in candidates.values())),
    )
    result["identity"] = digest(result)
    return result


def plan(root, destination, *, as_of=None):
    with storage_lock(root, True):
        result = collect(root, as_of=as_of)
        destination = Path(destination).resolve()
        if any(
            (
                destination.is_relative_to(Path(v["directory"]))
                for v in result["workspaces"].values()
            )
        ):
            raise ValueError("Write the retention report outside managed workspaces")
    atomic_json(destination, result)
    return result


def apply(path):
    report = json.loads(Path(path).read_text())
    identity = report.pop("identity")
    if digest(report) != identity:
        raise ValueError("Retention report digest mismatch")
    root = Path(report["root"])
    receipt = root / "maintenance" / (identity + ".json")
    with storage_lock(root, True):
        if receipt.exists():
            result = json.loads(receipt.read_text())
            if result.get("status") == "complete":
                return dict(result, reused=True)
            raise ValueError(
                "Previous cleanup was interrupted; create a fresh retention plan"
            )
        fresh = collect(root, as_of=report["as_of"])
        if fresh["identity"] != identity:
            raise ValueError(
                "Publication, workspace pins, policy or candidate files changed; replan retention"
            )
        if report["active_workspaces"]:
            return dict(
                status="deferred", active_workspaces=report["active_workspaces"]
            )
        store = Store(root)
        archive = store.archive
        for directory in report["archive_workspaces"]:
            archive_workspace_metadata(root, directory)
        for item in report["evict_generations"]:
            folder = root / "generations" / item["generation"]
            g = json.loads((folder / "manifest.json").read_text())
            publication_archive(root, g)
            if item["recovery"]:
                from spires_interpolation.publication import artifact_paths

                paths = artifact_paths(root, g)
                ledger = json.loads(
                    (archive / "publications" / (g["generation"] + ".json")).read_text()
                )["ledger"]
                checkpoints = {
                    k: store.put(p, "checkpoints")
                    for k, p in paths.items()
                    if k.startswith("checkpoint_")
                }
                atomic_json(
                    archive / "recoveries" / (g["generation"] + ".json"),
                    dict(generation=g, ledger=ledger, checkpoints=checkpoints),
                )
        for rec in report["files"]:
            if snapshot(rec["path"]) != rec:
                raise ValueError("Cleanup candidate changed during archival")
        atomic_json(
            receipt, dict(status="deleting", identity=identity, started_utc=utcnow())
        )
        parents = set()
        for rec in report["files"]:
            p = Path(rec["path"])
            p.unlink()
            parents.add(p.parent)
        boundaries = [
            root / "generations",
            root / "objects" / "checkpoints",
            root / "cache",
            archive / "objects" / "checkpoints",
            archive / "recoveries",
        ]
        boundaries.extend((Path(p) for p in report["archive_workspaces"]))
        for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
            while any((parent.is_relative_to(base) for base in boundaries)):
                try:
                    parent.rmdir()
                except OSError:
                    break
                if parent in boundaries:
                    break
                parent = parent.parent
        result = dict(
            status="complete",
            identity=identity,
            completed_utc=utcnow(),
            deleted_files=len(report["files"]),
            unlinked_logical_bytes=report["candidate_logical_bytes"],
            kept_hot=report["keep_hot"],
            kept_cold=report["keep_cold"],
        )
        atomic_json(receipt, result)
        return result


def report_storage(root):
    """Maintenance-only scan: physical blocks deduplicated by device/inode."""
    from .nrt import current

    root = Path(root).resolve()
    policy = settings(root)
    archive = Path(policy["archive_root"])
    seen = set()
    totals = {}
    logical = {}
    with storage_lock(root, True):
        roots = [root]
        if not archive.is_relative_to(root):
            roots.append(archive)
        for p in (root / "workspaces").glob("*.json"):
            path = Path(json.loads(p.read_text())["directory"])
            if not any((path.is_relative_to(r) for r in roots)):
                roots.append(path)
        for base in roots:
            for path in files_under(base):
                category = "metadata"
                if "objects" in path.parts:
                    i = path.parts.index("objects")
                    category = path.parts[i + 1]
                elif "cache" in path.parts:
                    category = "working_cache"
                elif "generations" in path.parts and path.suffix == ".h5":
                    category = "checkpoints"
                elif base not in (root, archive):
                    category = "workspaces"
                stat = path.stat()
                logical[category] = logical.get(category, 0) + stat.st_size
                key = (stat.st_dev, stat.st_ino)
                if key not in seen:
                    totals[category] = totals.get(category, 0) + stat.st_blocks * 512
                    seen.add(key)
        totals["total"] = sum(totals.values())
        alerts = []
        for k, limit in policy["warning_bytes"].items():
            if totals.get(k, 0) > limit:
                alerts.append(f"{k} exceeds {limit} bytes")
        path = root / "storage-report.json"
        previous = json.loads(path.read_text()) if path.exists() else None
        if (
            previous
            and policy["growth_warning_bytes"]
            and (
                totals["total"] - previous["physical_bytes"]["total"]
                > policy["growth_warning_bytes"]
            )
        ):
            alerts.append("Storage growth exceeds configured limit")
        _, _, ledger = current(root)
        cutoff = (
            datetime.now(timezone.utc).date()
            - timedelta(days=policy["unresolved_warning_days"])
        ).isoformat()
        overdue = sorted(
            (
                k
                for k, v in ledger["inventory"].items()
                if k.split("/")[1] < cutoff
                and (v is None or v["source_stream"] == "nrt")
            )
        )
        if overdue:
            alerts.append(f"{len(overdue)} overdue unresolved tile/dates retained")
        result = dict(
            created_utc=utcnow(),
            physical_bytes=totals,
            logical_bytes=logical,
            alerts=alerts,
            overdue_unresolved=overdue,
        )
        atomic_json(path, result)
        return result
