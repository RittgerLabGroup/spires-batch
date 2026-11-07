# Run an NRT update

Use [cycle.json](cycle.json) only after [registering completed history](storage.md).
For the normal single-JSON route, use `workflow cycle` in the
[complete walkthrough](workflow.md) instead.

## 1. Check the stream and configuration

Use the same domain, scientific context and asset bindings as the registered
history. Set the science/cloud interpreter paths, current annual assets,
`stream_root`, and resource policy. Do not overlap updates to the same stream.
Check credentials, available observations and scratch/archive capacity.

The example history ends May 11, 2026. May 12–18 provide actual held-out dates
for continuation; standard inputs exercise incremental behavior but do not
reconstruct as-issued NRT accuracy.

## 2. Allocate resources

The standalone `nrt cycle` command runs in the current allocation; it does not
submit itself. Inspect the profile first:

```bash
spires-batch scheduler-policy scheduler_policy.json --job-type nrt_five_tiles
```

Obtain a suitable Slurm/Blanca allocation using your site's approved submission
method and the resolved resources. If you want batch to submit that allocation,
use the unified workflow's `workflow cycle` command instead.

## 3. Execute inside that allocation

Use a new directory for this update:

```bash
spires-batch nrt cycle cycle.json --through 2026-05-18 --output cycle-20260518 --execute
```

This acquires observations, prepares clouds, retrieves snow properties,
interpolates revisions and publishes validated results. **Omitting `--execute`
is not a dry run:** it still acquires and processes observations, stopping at
the interpolation revision plan.

## 4. Inspect the publication

```bash
STREAM_ROOT="$(python -c 'import json; print(json.load(open("cycle.json"))["stream_root"])')"
spires-batch nrt status --root "$STREAM_ROOT"
spires-batch nrt storage-report --root "$STREAM_ROOT"
```

Check receipts, generation, maps and first/final cutoffs. After the May 18
continuation, the expected cutoffs are May 18/May 11. Full-history replay exits
with a transfer-size report; stop and diagnose rather than bypassing it.

See [routine NRT operations](../docs/nrt-operations.md). No example creates cron
entries or enables a recurring service.
