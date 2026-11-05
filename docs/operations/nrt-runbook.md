# Continue the historical campaign into NRT

Use the real h08v05 WY2026 historical campaign. Do not generate a fake checkpoint
or a separate demonstration history. There is one necessary bookkeeping step:
the current `nrt bootstrap` command **registers completed historical products and
checkpoints**. It does not rerun the historical science, but registration does
archive existing inputs/products and populate working state. This initial
handoff can involve substantial I/O; inspect the historical inventory and
destinations first. Omitting registration is not supported by the cycle API.

## One-time handoff

After historical export and QA succeed, retain its exact native source inventory
and prepared receipts. Bind the same donor domain, observation policy (including
the cloud-model manifest checksum), climatology, grids and assets. Complete
`cycle.json` and `storage.json`, then configure the durable archive before
registering the historical results:

```bash
STREAM_ROOT="$(python -c 'import json; print(json.load(open("cycle.json"))["stream_root"])')"
spires-batch nrt storage-config storage.json --root "$STREAM_ROOT"
spires-batch nrt bootstrap historical-plan.json inventory.json --root "$STREAM_ROOT"
spires-batch nrt status --root "$STREAM_ROOT"
```

Use a scratch `STREAM_ROOT` for fast working state and a separate `/pl` archive.
Without storage configuration the archive defaults to `STREAM_ROOT/archive`,
which would leave both copies on scratch. Initialization does not automatically
configure, restore or publish a stream. Give each independent training exercise
its own stream and archive. Keep historical prepared inputs at their registered
paths until archival and supported retention/migration are verified; do not
delete them merely because export succeeded. There is no blanket rsync command
in this workflow. See [per-user paths](user-paths.md).

## First real continuation

For the available historical interval, acquire May 19, 2026 and continue through
that date using the same WY2026 recipe. Standard reflectance acquired today is
valid for exercising incremental mechanics, but it does **not** reconstruct
as-issued NRT performance. Do not artificially relabel standard data as NRT.
The reference file describes every cycle configuration field; use real asset
bindings rather than the generic example's missing `deployment-cycle.json`.

Run inside a Blanca allocation with science/cloud environments and model assets
verified. Use a new cycle output directory:

```bash
spires-batch nrt cycle cycle.json --through 2026-05-19 --output CYCLE_DIRECTORY
```

Important: omitting `--execute` stops before interpolation/publication, **not**
before acquisition, cloud inference or retrieval. This command still performs
substantial work. Inspect the revision plan, then use the documented `nrt run`
on its `update.json`; alternatively run a subsequent approved full cycle with
`--execute`. Neither form creates cron entries or silently submits Slurm jobs.

## Routine daily checklist

1. Check the last published date and generation; do not start overlapping cycles.
2. Confirm credentials, expected new dates, archive/scratch capacity, annual
   assets and unchanged scientific context. Use the approved environment pins.
3. Submit the configured cycle allocation explicitly; archive its job ID, config,
   acquisition receipt, log and revision plan.
4. Inspect scheduler exit and scientific receipt. Resolve the domain's
   `current.json` once, then inspect every tile/date product from that generation.
5. Check expected first/final cutoff, explicit missing days, QA and source-code
   distributions, observation ages, and plausible snow/property/albedo maps.
6. Apply only reviewed retention plans. Do not delete the current checkpoint,
   unresolved inputs, leased workspaces or annual assets needed for revisions.

Standard replacements can revise prior dates and final products. On October 1,
add the next annual assets and retrieval bindings for every tile; retain the
previous year for late replacements. Do not reset the stream or register history
again solely because a WY changed.

## Stop and diagnose

| Symptom | Operator action |
| --- | --- |
| Missing local scene | Check archive/acquisition; distinguish a download gap from genuine missing observation |
| Missing asset or grid/unit error | Fix the binding/asset and replan; never disable validation |
| Allocation runs out of RAM | Inspect stage peak, adjust resources, retry supported incomplete work; never reintroduce chunked R0 |
| Failed upstream Slurm dependency | Inspect the upstream stage; do not force downstream publication |
| Reservation or lock conflict | Find its owner/job before retrying; do not blindly remove locks |
| Incompatible scientific history | Compare assets/recipe/domain and escalate; do not edit hashes to force reuse |
| Exit status 3 / full replay required | Read `full-replay-required.json` and durable stop report, including estimated bytes; stop and consult the maintainer |
| Job succeeded but no publication | Inspect scientific receipts and generation pointer; scheduler success alone is insufficient |

The full-replay stop does not wait for interactive approval under cron or Slurm
and does not transfer the complete history in the background. The prior
publication remains current. Normal bounded replay is allowed; exceptional
full recovery is an explicit separate decision, not a training prerequisite.

See [NRT storage](../nrt-storage.md) for retention commands and the
[scientific algorithm reference](../../../spires-interpolation/docs/algorithms.md)
for field units, source codes, screening bits and temporal rules. Grain radius
is micrometres; dust is ppm; snow fractions/albedo are fractions, not percent.
Variable-specific observation age describes eligible direct observations, not
interpolation success. Keep decoded NetCDF fill values distinct from zero snow.
