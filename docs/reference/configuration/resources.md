# Standard resource requests

[Reference index](README.md) · [Policy guide](../../../examples/scheduler_policy.md)

Generated from [scheduler_policy.json](../../../examples/scheduler_policy.json).
Edit that policy and regenerate this page; do not maintain a separate table.
Reservations apply to one task, not an entire array. Memory is total per task.

| Job type | Queue | CPUs | Memory | Walltime | Array concurrency | Evidence status |
| --- | --- | --- | --- | --- | --- | --- |
| `r0` | blanca-snow | 1 | 0 | 08:00:00 | 1 | measured |
| `tile_inversion` | blanca-rittger | 1 | 6G | 04:00:00 | 20 | operator_baseline |
| `daily_single_tile` | blanca-rittger | 1 | 8G | 04:00:00 | 4 | starting_budget |
| `daily_five_tiles` | blanca-rittger | 5 | 16G | 04:00:00 | 4 | measured |
| `nrt_five_tiles` | blanca-rittger | 5 | 16G | 12:00:00 | 1 | starting_budget |
| `interpolation_screen` | blanca-rittger | 1 | 6G | 08:00:00 | 16 | measured |
| `interpolation_spatial` | blanca-rittger | 4 | 8G | 12:00:00 | 8 | measured |
| `interpolation_properties` | blanca-rittger | 1 | 8G | 12:00:00 | 12 | starting_budget |
| `interpolation_export` | blanca-rittger | 1 | 8G | 12:00:00 | 2 | starting_budget |

R0 memory `0` means all node memory with exclusive access. Concurrency is not
consumed by individual NRT allocations; unified historical/interpolation and batch jobs retain their arrays.

## Available queues

Select a queue explicitly; no automatic overflow is implied. Shared node exclusions apply to all queues.

| Queue | Cluster | Partition | Account | QoS |
| --- | --- | --- | --- | --- |
| `blanca-snow` | blanca | blanca-snow | blanca-snow | blanca-snow |
| `blanca-rittger` | blanca | blanca-rittger | blanca-rittger | blanca-rittger |
| `preemptable` | blanca | blanca | blanca-snow | preemptable |

## `r0`

One tile / summer; exclusive full node

- WY2026 five-tile campaign: 122 summer scenes per tile, maintained in-memory construction.
- Request all node memory and exclusive access; one CPU requested does not mean the rest of the node is shared. Additional compute threads have not been benchmarked.

## `tile_inversion`

One tile / date spectral inversion worker

- Operator baseline: one worker and 6 GiB per tile/date.
- This is not the budget for a complete multi-tile cloud/preparation job.

## `daily_single_tile`

One date with one tile, clouds, retrieval and daily albedo

- 6 GiB worker baseline plus 2 GiB coordinator headroom; validate a pilot with the complete runner.

## `daily_five_tiles`

One date across up to five tiles, one retrieval worker per tile

- Five-tile 16 GiB trial and historical preparation completed; observed preparation peak was below 10 GiB.
- The current runner adds daily albedo; validate the complete runner before treating that measurement as its guarantee.

## `nrt_five_tiles`

One NRT cycle over up to five tiles; serialized updates to one stream

- Preparation uses the five-tile measured budget; complete cycle time/memory still needs validation.
- The allocation must also cover the interpolation settings registered with the stream. No cron or Slurm job is created by selecting this profile.

## `interpolation_screen`

One tile / row stripe over the historical interval

- 600 screening tasks completed in the five-tile WY2026 campaign with this per-task allocation.

## `interpolation_spatial`

One date across the full five-tile donor domain

- 2026-11-06: 76 completed spatial tasks had a maximum sampled MaxRSS of 1.30 GiB, median 0.75 GiB.
- Four CPUs support the multithreaded spatial kernel; 8 GiB retains headroom. Full winter/spring coverage remains to be checked.
- Campaign array 29341812 was changed from 24 GiB / concurrency 4 to 8 GiB / concurrency 8, split manually across blanca-snow and blanca-rittger. This profile selects one queue; it does not reproduce that split automatically.

## `interpolation_properties`

One tile / row stripe over the historical interval

- Retain until the property-completion pilot has measured CPU, peak memory and runtime.

## `interpolation_export`

One tile product export

- Retain until export has measured CPU, peak memory and runtime.
