# spires-batch

`spires-batch` provides the operational planning layer for running the
[SPIReS](https://github.com/SPIReS-Organization) retrieval over many tiles and
dates. It turns a strict JSON request into an immutable, preflighted task
manifest that can be inspected serially or rendered as direct Slurm arrays.

Phase A implements planning and recovery foundations. Scientific stage
execution is intentionally not connected yet.

## Installed capabilities

- Versioned JSON request and resolved-manifest schemas.
- Stable configuration, plan, run, and task identities.
- Explicit-file and configuration-driven CURC discovery.
- Four-layer schema, semantic, inventory, and metadata preflight.
- Optional validated staging from authoritative storage to shared scratch.
- Dry-run and executor-neutral serial backends.
- Direct Blanca `sbatch` rendering without submission.
- Structured JSON Lines task events, output-derived status, retry manifests,
  tile summaries, and run summaries.
- Persistent duplicate-output reservations and auditable cleanup.

The package does not import the temporary batch-manifest types in `spires-io`.
Scientific packages remain responsible for scientific loading, computation,
and persisted-product validation.

## Installation

```bash
pip install spires-batch
```

The Phase A core depends only on Pydantic. Scientific package dependencies will
be connected directly when the Phase D executor is implemented.

## JSON requests

Only JSON is accepted. Every request declares schema version `1` and exactly
one sensor/platform combination:

```json
{
  "artifact_type": "spires_batch_request",
  "schema_version": 1,
  "run": {
    "name": "vj109ga-h09v04-wy2026",
    "sensor": "viirs",
    "platform": "noaa20"
  },
  "selection": {
    "tiles": ["h09v04"],
    "water_years": [2026],
    "dates": []
  },
  "steps": ["invert", "albedo"],
  "inputs": {
    "files": [],
    "roots": [
      {
        "adapter": "curc",
        "role": "reflectance",
        "path": "/current/authoritative/root",
        "pattern": "**/VJ109GA.A*.h09v04.*.h5"
      }
    ]
  },
  "r0": {
    "mode": "existing",
    "artifacts": [
      {
        "id": "r0_20250601_20250930",
        "path": "/exact/path/r0_20250601_20250930.nc",
        "tile": "h09v04",
        "water_year": 2026
      }
    ]
  },
  "output": {
    "root": "/product/root",
    "existing_file_handling": "write_new_file",
    "existing_output_policy": "error"
  }
}
```

See
[`examples/viirs_raw_request.json`](examples/viirs_raw_request.json)
for a complete staging and resource-profile example. Generate the exact public
schema with:

```bash
spires-batch schema request
spires-batch schema resolved-plan
```

Unknown fields are rejected. Relative paths are resolved relative to the
request file.

## Stages and artifacts

The schema recognizes:

```text
build_r0 -> invert -> albedo -> interpolate
```

Selected stages are explicit; prerequisites are never inserted silently.
`invert` and `albedo` are initially fused into one daily task when both are
selected, producing:

```text
<root>/<sensor>/<platform>/<tile>/
    spires_<product>_<tile>_<YYYYMMDD>_raw.nc
```

For example:

```text
viirs/noaa20/h09v04/spires_vj109ga_h09v04_20260314_raw.nc
```

The `interpolate` noun is reserved in schema version 1, but requests selecting
it are rejected with `Interpolation not yet implemented`. Phase F will define
its temporal windows and dependencies before the planner creates interpolation
tasks or `_interpolate.nc` artifacts.

An existing R0 always has an explicit ID and path. Build requests likewise
provide each desired output path; batch does not infer reference-year or water-
year directory conventions.

## Discovery and scratch staging

Mutable site roots belong in each request. No `/pl` or `/scratch` path is
compiled into the package. The public `curc` adapter understands supported NASA
filename identities, while exact roots and patterns remain operational
configuration.

Optional staging records both the authoritative source and effective scratch
path in every resolved task:

```json
{
  "execution": {
    "staging": {
      "enabled": true,
      "root": "/scratch/alpine/USER/spires-batch/input-cache",
      "verification": "stat",
      "reuse_valid": true
    }
  }
}
```

`stat` verifies file size; `sha256` provides a stricter, more expensive copy
check. Copies use a temporary sibling and atomic promotion.

## Preflight

Preflight always runs before Slurm rendering:

1. JSON schema validation.
2. Cross-section semantic validation.
3. Exact inventory and collision validation.
4. Optional lightweight metadata-header inspection.

Metadata modes are:

- `none`: inventory only.
- `sample`: one deterministic representative per homogeneous role/product/
  format/name group. This is the default.
- `all`: every resolved input header.

Runtime scientific tasks will still validate every input authoritatively.

## Planning and inspection

```bash
spires-batch validate request.json
spires-batch plan request.json --output resolved-plan.json
spires-batch dry-run resolved-plan.json
spires-batch stage resolved-plan.json
spires-batch stage resolved-plan.json --execute
```

Resolved plans are immutable: an existing manifest is never overwritten. The
manifest records:

- `config_digest`: SHA-256 of canonical validated request JSON.
- `plan_digest`: SHA-256 of deterministic resolved tasks and resources.
- A unique run ID.
- Stable semantic task IDs independent of array indices.

Hashes are operational identifiers and do not appear in scientific filenames.

## Slurm rendering

```bash
spires-batch render-slurm resolved-plan.json --output-dir slurm-preview
```

This writes strict dependency array scripts and a `submit.sh` preview using
`slurm/blanca`, the selected partition, and the `spipy14` environment. It never
submits a job. The rendered task entry point remains deliberately unavailable
until Phase D connects the scientific executor.

## Status and retries

Task workers emit append-only JSON Lines start and terminal events. Status is
derived from attempt history plus output validation; file existence alone is
not scientific completion.

```bash
spires-batch summarize resolved-plan.json \
    --events-dir task-logs \
    --output-dir summaries

spires-batch retry-manifest resolved-plan.json \
    --events-dir task-logs \
    --output retry-1.json
```

Summaries are written as JSON, CSV, and concise text at run and tile levels.
Only transient failures below the configured retry cap enter a retry manifest.

## Output reservations

Before actual serial execution or Slurm submission, each output is reserved on
shared storage. A second run targeting the same path fails before execution.
Dry runs only diagnose conflicts:

```bash
spires-batch reservations diagnose resolved-plan.json --state-root /product/root
spires-batch reservations list --state-root /product/root
```

After an output is reopened and validated, completion is recorded in the task
history and the completed reservation is removed automatically. A recovery
command previews completed leftovers and requires `--apply` to remove them:

```bash
spires-batch reservations prune \
    --state-root /product/root \
    --status completed \
    --older-than-days 7
```

Failed or interrupted reservations are never removed because of age alone.
Releasing one requires the exact run ID, task ID, reason, and explicit
`--apply`.
