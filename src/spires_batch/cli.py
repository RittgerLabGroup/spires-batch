"""Command-line interface for Phase A planning and operations."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from spires_batch.backends import DryRunBackend
from spires_batch.events import read_event_logs
from spires_batch.planner import plan_request
from spires_batch.preflight import PreflightFailedError
from spires_batch.reservations import ReservationStore
from spires_batch.serialization import (
    load_plan,
    load_request,
    write_plan,
)
from spires_batch.slurm import render_slurm
from spires_batch.staging import execute_staging
from spires_batch.status import (
    attempts_from_events,
    build_retry_plan,
    summarize,
    tile_summaries,
    write_summary_files,
)
from spires_batch.models import RequestConfig, ResolvedPlan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spires-batch",
        description="Plan and operate SPIReS batch workflows",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    schema = subparsers.add_parser(
        "schema",
        help="print the versioned JSON Schema for a public artifact",
    )
    schema.add_argument(
        "artifact",
        choices=("request", "resolved-plan"),
    )
    schema.add_argument("--output", "-o", type=Path)

    validate = subparsers.add_parser(
        "validate",
        help="validate configuration, inventory, and configured metadata headers",
    )
    validate.add_argument("config", type=Path)

    plan = subparsers.add_parser(
        "plan",
        help="resolve a JSON request into an immutable manifest",
    )
    plan.add_argument("config", type=Path)
    plan.add_argument("--output", "-o", type=Path, required=True)

    dry_run = subparsers.add_parser(
        "dry-run",
        help="show the task graph without executing or reserving outputs",
    )
    dry_run.add_argument("artifact", type=Path)

    stage = subparsers.add_parser(
        "stage",
        help="preview or execute configured input staging",
    )
    stage.add_argument("manifest", type=Path)
    stage.add_argument(
        "--execute",
        action="store_true",
        help="perform copies; otherwise only preview",
    )

    slurm = subparsers.add_parser(
        "render-slurm",
        help="render direct sbatch arrays without submitting them",
    )
    slurm.add_argument("manifest", type=Path)
    slurm.add_argument("--output-dir", type=Path, required=True)

    summary = subparsers.add_parser(
        "summarize",
        help="derive run and tile summaries from task event logs and outputs",
    )
    summary.add_argument("manifest", type=Path)
    summary.add_argument("--events-dir", type=Path, required=True)
    summary.add_argument("--output-dir", type=Path, required=True)

    retry = subparsers.add_parser(
        "retry-manifest",
        help="create an immutable retry manifest for eligible transient failures",
    )
    retry.add_argument("manifest", type=Path)
    retry.add_argument("--events-dir", type=Path, required=True)
    retry.add_argument("--output", "-o", type=Path, required=True)

    execute_task = subparsers.add_parser(
        "execute-task",
        help="reserved Phase D entry point used by rendered Slurm arrays",
    )
    execute_task.add_argument("--manifest", type=Path, required=True)
    execute_task.add_argument("--task-index", type=Path, required=True)
    execute_task.add_argument("--array-index", type=int, required=True)

    reservations = subparsers.add_parser(
        "reservations",
        help="inspect and clean persistent output reservations",
    )
    reservation_commands = reservations.add_subparsers(
        dest="reservation_command",
        required=True,
    )
    reservation_list = reservation_commands.add_parser("list")
    reservation_list.add_argument("--state-root", type=Path, required=True)

    reservation_diagnose = reservation_commands.add_parser("diagnose")
    reservation_diagnose.add_argument("manifest", type=Path)
    reservation_diagnose.add_argument("--state-root", type=Path, required=True)

    reservation_prune = reservation_commands.add_parser("prune")
    reservation_prune.add_argument("--state-root", type=Path, required=True)
    reservation_prune.add_argument(
        "--status",
        choices=("completed",),
        default="completed",
    )
    reservation_prune.add_argument("--older-than-days", type=float, default=7.0)
    reservation_prune.add_argument("--apply", action="store_true")

    reservation_release = reservation_commands.add_parser("release-stale")
    reservation_release.add_argument("--state-root", type=Path, required=True)
    reservation_release.add_argument("--output", type=Path, required=True)
    reservation_release.add_argument("--run-id", required=True)
    reservation_release.add_argument("--task-id", required=True)
    reservation_release.add_argument("--reason", required=True)
    reservation_release.add_argument("--apply", action="store_true")
    return parser


def _print_preflight(plan) -> None:
    result = plan.preflight
    for issue in result.issues:
        location = f" [{issue.path}]" if issue.path is not None else ""
        print(
            f"{issue.severity.value.upper():7} "
            f"{issue.layer.value}/{issue.code}: {issue.message}{location}"
        )
    print(f"preflight: {'passed' if result.passed else 'failed'}")


def _resolve_artifact(path: Path):
    with path.open("r", encoding="utf-8") as stream:
        artifact_type = json.load(stream).get("artifact_type")
    if artifact_type == "spires_batch_resolved_plan":
        return load_plan(path)
    request = load_request(path)
    return plan_request(request, base_dir=path.parent)


def _event_paths(directory: Path) -> tuple[Path, ...]:
    return tuple(sorted(directory.glob("**/*.jsonl")))


def _command_validate(args: argparse.Namespace) -> int:
    request = load_request(args.config)
    plan = plan_request(request, base_dir=args.config.parent)
    _print_preflight(plan)
    print(f"tasks: {len(plan.tasks)}")
    return 0


def _command_schema(args: argparse.Namespace) -> int:
    model = RequestConfig if args.artifact == "request" else ResolvedPlan
    payload = json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(payload, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(args.output)
    return 0


def _command_plan(args: argparse.Namespace) -> int:
    request = load_request(args.config)
    plan = plan_request(request, base_dir=args.config.parent)
    write_plan(args.output, plan)
    _print_preflight(plan)
    print(f"manifest: {args.output}")
    print(f"run_id: {plan.run_id}")
    print(f"config_digest: {plan.config_digest}")
    print(f"plan_digest: {plan.plan_digest}")
    print(f"tasks: {len(plan.tasks)}")
    return 0


def _command_dry_run(args: argparse.Namespace) -> int:
    plan = _resolve_artifact(args.artifact)
    _print_preflight(plan)
    for item in DryRunBackend().render(plan):
        dependencies = ",".join(item.dependencies) or "-"
        print(
            f"[{item.index:04d}] {item.task_id} "
            f"stages={','.join(item.stages)} depends_on={dependencies}"
        )
        for path in item.inputs:
            print(f"         input  {path}")
        for path in item.outputs:
            print(f"         output {path}")
    print(f"tasks: {len(plan.tasks)}")
    return 0


def _command_stage(args: argparse.Namespace) -> int:
    plan = load_plan(args.manifest)
    results = execute_staging(plan, dry_run=not args.execute)
    for result in results:
        print(
            f"{result.status:8} {result.action.source_path} -> "
            f"{result.action.destination_path}: {result.message}"
        )
    print(f"staging actions: {len(results)}")
    return 0


def _command_render_slurm(args: argparse.Namespace) -> int:
    plan = load_plan(args.manifest)
    result = render_slurm(
        plan,
        manifest_path=args.manifest,
        output_directory=args.output_dir,
    )
    for group in result.groups:
        dependencies = ",".join(group.dependency_group_ids) or "-"
        print(
            f"{group.group_id}: tasks={len(group.tasks)} "
            f"dependencies={dependencies} script={group.script_path}"
        )
    print(f"submission preview: {result.submit_script}")
    print("No Slurm command was submitted.")
    return 0


def _command_summarize(args: argparse.Namespace) -> int:
    plan = load_plan(args.manifest)
    events = read_event_logs(_event_paths(args.events_dir))
    attempts = attempts_from_events(events)
    run_summary = summarize(plan, attempts)
    paths = write_summary_files(run_summary, args.output_dir)
    for tile, tile_summary in tile_summaries(run_summary).items():
        write_summary_files(
            tile_summary,
            args.output_dir / "tiles" / tile,
            basename="tile-summary",
        )
    print("\n".join(str(path) for path in paths))
    return 0


def _command_retry(args: argparse.Namespace) -> int:
    plan = load_plan(args.manifest)
    events = read_event_logs(_event_paths(args.events_dir))
    retry_plan = build_retry_plan(plan, attempts_from_events(events))
    write_plan(args.output, retry_plan)
    print(f"retry manifest: {args.output}")
    print(f"eligible tasks: {len(retry_plan.tasks)}")
    return 0


def _command_execute_task(args: argparse.Namespace) -> int:
    plan = load_plan(args.manifest)
    with args.task_index.open("r", encoding="utf-8") as stream:
        index = json.load(stream)
    if index.get("plan_digest") != plan.plan_digest:
        raise ValueError("Slurm task index does not belong to the supplied manifest")
    task_ids = index.get("task_ids", [])
    if args.array_index < 0 or args.array_index >= len(task_ids):
        raise IndexError(
            f"array index {args.array_index} is outside 0..{len(task_ids) - 1}"
        )
    task_id = task_ids[args.array_index]
    raise RuntimeError(
        f"scientific executor is not connected in Phase A; task {task_id!r} was "
        "resolved correctly but cannot yet execute"
    )


def _command_reservations(args: argparse.Namespace) -> int:
    store = ReservationStore(args.state_root)
    if args.reservation_command == "list":
        reservations = store.list()
        for reservation in reservations:
            print(
                f"{reservation.state.value:10} {reservation.run_id} "
                f"{reservation.task_id} {reservation.output_path}"
            )
        print(f"reservations: {len(reservations)}")
        return 0

    if args.reservation_command == "diagnose":
        plan = load_plan(args.manifest)
        conflicts = 0
        for task in plan.tasks:
            for output in task.outputs:
                reservation = store.load(output.path)
                if reservation is None:
                    print(f"available  {output.path}")
                else:
                    conflicts += 1
                    print(
                        f"conflict   {output.path}: run={reservation.run_id} "
                        f"task={reservation.task_id} state={reservation.state.value}"
                    )
        print(f"conflicts: {conflicts}")
        return 1 if conflicts else 0

    if args.reservation_command == "prune":
        candidates = store.prune_completed(
            older_than=timedelta(days=args.older_than_days),
            apply=args.apply,
        )
        action = "removed" if args.apply else "would remove"
        for reservation in candidates:
            print(f"{action} {reservation.output_path}")
        print(f"completed reservations: {len(candidates)}")
        return 0

    if args.reservation_command == "release-stale":
        reservation = store.release_stale(
            args.output,
            expected_run_id=args.run_id,
            expected_task_id=args.task_id,
            reason=args.reason,
            apply=args.apply,
        )
        action = "released" if args.apply else "would release"
        print(f"{action} {reservation.output_path}")
        return 0

    raise ValueError(f"unsupported reservation command {args.reservation_command!r}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    commands = {
        "schema": _command_schema,
        "validate": _command_validate,
        "plan": _command_plan,
        "dry-run": _command_dry_run,
        "stage": _command_stage,
        "render-slurm": _command_render_slurm,
        "summarize": _command_summarize,
        "retry-manifest": _command_retry,
        "execute-task": _command_execute_task,
        "reservations": _command_reservations,
    }
    try:
        return commands[args.command](args)
    except ValidationError as exc:
        print("configuration validation failed:", file=sys.stderr)
        for error in exc.errors(include_url=False):
            location = ".".join(str(item) for item in error["loc"]) or "<root>"
            print(f"  {location}: {error['msg']}", file=sys.stderr)
        return 2
    except PreflightFailedError as exc:
        print("preflight failed:", file=sys.stderr)
        for issue in exc.result.issues:
            if issue.severity.value == "error":
                location = f" [{issue.path}]" if issue.path is not None else ""
                print(
                    f"  {issue.layer.value}/{issue.code}: "
                    f"{issue.message}{location}",
                    file=sys.stderr,
                )
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
