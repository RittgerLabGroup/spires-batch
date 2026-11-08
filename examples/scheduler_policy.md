# Shared resource and queue defaults

[examples/scheduler_policy.json](scheduler_policy.json)
is the shared source for node exclusions, submission queues and resource defaults
by job type. Update it when measurements change, rather than repeatedly editing
every example. The [resource table](../docs/reference/configuration/resources.md) is generated
from that file, including the evidence behind each budget.

## What a profile records

- `queue`: a named cluster/partition/account/QoS combination from `queues`.
- `cpus`, `memory`, `time`: the reservation for **one task**. Memory is total,
  not per CPU. `concurrency` controls the number of simultaneous array tasks.
- `unit`: what that task processes. A tile/date inversion is not the same task
  as five-tile cloud preparation or spatial interpolation across the whole domain.
- `status` and `evidence`: whether this is measured, an operator baseline, or
  a starting budget; measurement dates, job IDs and any qualifications.
- Optional `preparation`: matched historical/NRT worker-count and memory defaults.
  `max_tiles` prevents accidentally applying a small-domain budget to a larger one.

The tile-inversion baseline is one CPU/worker and 6 GiB. Full daily preparation
needs its own profile because cloud inference and the coordinator also use memory.
The five-tile profile retains the measured 16 GiB total budget rather than
automatically multiplying the standalone inversion reservation by five.

R0 uses `memory: "0"` and `exclusive: true` on `blanca-snow`: reserve the whole
node and all its available memory. It still loads all summer scenes for one
tile/year before selection. Its one requested CPU does not permit other jobs to
share the node, and does not claim that additional algorithm threads would help.

## Single-tile daily concurrency

`daily_single_tile` allows **76 concurrent dates on blanca-rittger**, with
1 CPU and 8 GiB per date. This resource profile supports individual-tile work;
the maintained five-tile training workflow uses `daily_five_tiles`.

The limit assumes 88 usable cores with a four-core allocation already running
and eight cores reserved. Check current CPU and memory
allocations before submitting: a per-array limit does not reserve eight cores
across the partition, enforce per-node placement, or coordinate multiple arrays.
Reduce concurrency when other workloads or unavailable nodes require it.

To select blanca-snow for a **separate single-tile historical run**, use:

```json
"scheduler": {
  "policy": "scheduler_policy.json",
  "job_type": "daily_single_tile",
  "queue": "blanca-snow",
  "concurrency": 62
}
```

At 8 GiB per task, memory limits the blanca-snow node to 62 tasks. Queue selection
does not split one array across both partitions. Concurrent runs need disjoint
date selections and separate output directories; do not submit the same dates
twice across queues.

These budgets cover single-tile daily processing. Five-tile preparation,
interpolation, R0 and serialized NRT cycles retain their own resource profiles.

## Select a profile

Historical configurations select a date-processing profile:

```json
"scheduler": {
  "policy": "scheduler_policy.json",
  "job_type": "daily_five_tiles"
}
```

Interpolation selects profiles per processing stage:

```json
"scheduler": {
  "policy": "scheduler_policy.json",
  "job_type": "interpolation_screen",
  "stages": {
    "spatial": {"job_type": "interpolation_spatial"},
    "properties": {"job_type": "interpolation_properties"},
    "export": {"job_type": "interpolation_export"}
  }
}
```

R0 and advanced batch requests use the same selectors under `execution`, with
batch-specific overrides under `execution.resources`:

```json
"execution": {
  "policy": "scheduler_policy.json",
  "job_type": "r0",
  "resources": {"environment_name": "spipy14"}
}
```

Keep staging and retry settings in that `execution` object as needed. Do not
combine this single-job-type selection with inline tile/stage profile routing.
For an advanced per-tile inversion request, select `tile_inversion` instead.

NRT cycle configurations can select `nrt_five_tiles` under `scheduler`. This
supplies preparation budgets and saves a resolved configuration for provenance.
It **does not submit or resize a Slurm allocation**. The allocation must also
cover the interpolation settings already registered with the stream. NRT updates
to one stream remain serialized; a profile's concurrency does not bypass that.

Unified workflows accept a shared `scheduler.policy` path or inline policy object.
Each `jobs.NAME` can select `job_type` and `queue`. Historical and interpolation
jobs retain their arrays and concurrency limits; R0 uses managed batch arrays.
Overrides for historical/interpolation/NRT go under `jobs.NAME.resources` or the
child scheduler; batch overrides go under `request.execution.resources`.
Separate `coordinator` resources cover planning/verification, not scientific work.
Initialization, registration and NRT cycles remain individual allocations.

## Preview and override

From a configured environment, these commands only print information:

```bash
spires-batch scheduler-policy examples/scheduler_policy.json
spires-batch scheduler-policy examples/scheduler_policy.json --job-type interpolation_spatial --queue blanca-snow
spires-batch scheduler-policy examples/scheduler_policy.json --job-type interpolation_spatial --queue preemptable
```

To choose another authorized queue for a particular run, add
`"queue": "blanca-snow"` beside its `job_type`. This changes partition, account
and QoS together. The shared exclusions still apply. A queue selection does not
automatically split dates between partitions or submit overflow work.

The supplied `preemptable` queue selects cluster `blanca`, partition `blanca`,
account `blanca-snow` and QoS `preemptable`. This combination was verified against
the operator's Slurm associations and successful prior jobs on November 6, 2026;
colleagues should verify their own account authorization. Select it explicitly
with `"queue": "preemptable"` beside a job type or within an interpolation stage:

```json
"spatial": {
  "job_type": "interpolation_spatial",
  "queue": "preemptable"
}
```

CPU/memory defaults and the usual node exclusions still apply. There is no
automatic fallback when a preferred queue fills. Preemptable work can be
interrupted by node owners; use it for suitable restartable array work and check
restart behavior before scaling. Keep high-memory R0 on `blanca-snow` and
operational coordinators on a non-preemptable queue by default.

Explicit resources override profile defaults. For historical/interpolation,
`scheduler.memory`, for example, overrides the profile's memory. For batch
requests use `execution.resources.memory`, and `execution.max_concurrent_tasks`
for concurrency. Matched-pipeline budgets in `preparation` (or at the NRT cycle's
top level) override profile preparation defaults; allocation checks still apply.

For interpolation, stage profile defaults replace base profile defaults. Explicit
base resource values then apply to all stages; explicit stage values win last.
A base queue override applies to all stages unless a stage specifies another queue.
For named workflows, explicit scheduler resource values override the profile,
then per-job `resources` values win last.

## Keep the measurements

When tuning a job type:

1. Check successful representative jobs, including harder dates, for peak RSS,
   CPU usage, elapsed time and output quality. Slurm memory sampling is not a
   guarantee that future dates have the same peak.
2. Update the relevant `job_types` entry and its `evidence` together. Retain useful
   headroom and distinguish measured allocations from untested estimates.
3. Regenerate the embedded workflow policy and configuration/reference pages with
   `python scripts/build_workflow_example.py` and
   `python scripts/build_config_reference.py` in `spipy14`.
4. Make a **new plan** from the original run configuration to adopt the change.
   Inspect its resolved settings and generated Slurm directives before submission.

Plans freeze resolved settings and policy snapshots. Rendering/executing an
existing plan does not reread current defaults. Live scheduler overrides must
be recorded separately; the November 6 adjustment to array `29341812` is recorded
in the local campaign archive. Do not edit scientific plans or running environments
to resize already-submitted tasks.

Copies of `scheduler_policy.json` are independent. Point run configurations to
one maintained shared copy if you want future plans to pick up team-wide changes;
updating this repository cannot update old copies in other operators' directories.
