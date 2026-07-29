# spires-batch

`spires-batch` provides the operational planning layer for running the
[SPIReS](https://github.com/SPIReS-Organization) retrieval over many tiles and
dates. It turns a strict JSON request into an immutable, preflighted task
manifest that can be inspected serially or rendered as direct Slurm arrays.

Phase A planning and recovery foundations are complete. Phase D connects
manifest-backed existing-R0 inversion, fused inversion/postprocessing,
standalone full-product postprocessing, atomic persistence, and scientific
output validation. R0 construction uses strict sensor-specific summer-composite
recipes, and standalone postprocessing rejects compact `results_subset` inputs
during preflight.

## Installed capabilities

- Versioned JSON request and resolved-manifest schemas.
- Stable configuration, plan, run, and task identities.
- Explicit-file and configuration-driven CURC discovery.
- Four-layer schema, semantic, inventory, and metadata preflight.
- Optional validated staging from authoritative storage to shared scratch.
- Dry-run and scientific serial execution backends.
- Direct Blanca `sbatch` rendering without submission.
- Structured JSON Lines task events, output-derived status, retry manifests,
  tile summaries, and run summaries.
- Persistent duplicate-output reservations and auditable cleanup.
- Typed scene-preparation, clustering, inversion, and postprocessing options.
- Reopened-product validation, explicit output reuse, and retry
  classification.

The executor translates resolved batch tasks directly into the current public
`spires-io`, `spires-inversion`, and `spires-postprocess` APIs. It does not use
or import the older `spires-io` manifest item types. Scientific packages remain
responsible for scientific loading, computation, and persisted-product
validation.

## Installation

```bash
pip install spires-batch
```

The planning core depends only on Pydantic. During coordinated Phase D
development the scientific stack is supplied by sibling editable installs.
Stable scientific dependency references will be added after the component
branches merge; temporary branch references are not added to package metadata.

### Coordinated Phase D development

Phase D is developed against sibling working trees so unmerged scientific
branches do not become package or Git-history dependencies. From an environment
with `mamba` available, run:

```bash
module load miniforge
mamba run -n spipy14 bash scripts/bootstrap_local_phase_d.sh
```

The bootstrap verifies and installs this development stack editably, without
resolving dependencies from GitHub:

- `spires-contract/batch-support`
- `spires-io/batch-support`
- `spires-r0/ross-dev`
- `spires-inversion/main`
- `spires-postprocess/main`
- `RittgerLabGroup/spires-batch/main`

It never checks out, merges, or rebases a branch. Use `--verify-only` to inspect
an existing environment. After the component PRs merge, update each component
checkout to `main` and run the same command with `--merged`. Temporary branch
selection therefore remains local development policy and does not enter
`spires-batch` dependency metadata. On CURC, the bootstrap uses `/usr/bin/gcc`
and `/usr/bin/g++` for the inversion extension to avoid the `spipy14` conda
GCC/glibc conflict; override these with `SPIRES_PHASE_D_CC` and
`SPIRES_PHASE_D_CXX` if needed.

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
    "files": [
      {
        "role": "lut",
        "name": "inversion_lut",
        "path": "/exact/path/reflectance-lut.nc"
      },
      {
        "role": "lut",
        "name": "albedo_lookup",
        "path": "/exact/path/albedo-lut.nc"
      },
      {
        "role": "ancillary",
        "name": "dem",
        "path": "/exact/path/h09v04-dem.tif",
        "tile": "h09v04",
        "metadata": {"units": "m"}
      },
      {
        "role": "ancillary",
        "name": "slope",
        "path": "/exact/path/h09v04-slope.tif",
        "tile": "h09v04"
      },
      {
        "role": "ancillary",
        "name": "aspect",
        "path": "/exact/path/h09v04-aspect.tif",
        "tile": "h09v04"
      }
    ],
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
  "science": {
    "invert": {
      "algorithm": 6,
      "max_eval": 200,
      "n_workers": 1
    },
    "albedo": {
      "calculate_albedo": true
    }
  },
  "output": {
    "root": "/product/root",
    "product_contents": "full",
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

Scientific context files use stable names:

- LUTs: `inversion_lut`, `albedo_lookup`, and `forcing_lookup`.
- Ancillary layers: `dem`, `slope`, `aspect`, `skyview`,
  `canopy_fraction`, and `ice_fraction`.
- Masks: `cloud_mask`, `water_mask`, `ice_mask`, and `playa_mask`.

Names are validated during request loading, and each resolved task must contain
exactly one copy of every context input required by its selected operations.
If `science.invert.preparation.bands` is omitted, execution derives the ordered
band set from a labeled R0 product. Positional GeoTIFF R0 bands require an
explicit band list. The executor then selects those exact bands, in scene
order, from the canonical master reflectance LUT.

Source-reader metadata is normalized only at the persistence boundary: HDF5
dimension-scale bookkeeping is discarded, while a source `_FillValue` is
retained as `source_fill_value`. This prevents source-container internals from
colliding with the canonical grouped NetCDF representation.

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

`output.product_contents` independently selects the stored payload for each raw
product:

- `full` retains the complete grouped `SpiresData` inputs and results.
- `results_subset` retains the self-describing grid, packed QA, and results
  while omitting inputs that can be reopened from their configured sources.

The selected value is copied into every resolved raw-output task. It does not
change whether the product is `inversion_raw` or `postprocessed_raw`; fused
`invert + albedo` tasks can write the latter directly, while standalone albedo
with `update_atomically` transitions an existing raw file.

Standalone albedo requires a `full` input product. Preflight rejects
`results_subset` inputs because they intentionally omit the scene and ancillary
context needed to calculate postprocessing products. Fused `invert + albedo`
tasks may still write `results_subset` directly.

The `interpolate` noun is reserved in schema version 1, but requests selecting
it are rejected with `Interpolation not yet implemented`. Phase F will define
its temporal windows and dependencies before the planner creates interpolation
tasks or `_interpolate.nc` artifacts.

An existing R0 always has an explicit ID and path. Build requests likewise
provide each desired output path; batch does not infer reference-year or water-
year directory conventions.

Build requests use exactly one of two recipes:

- `viirs_summer_composite` for VIIRS runs;
- `modis_summer_composite` for MODIS runs.

Both dispatch to the same summer-composite workflow and typed science options;
the sensor-specific public R0 API supplies the reflectance reader and canonical
band definitions.

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

## Serial scientific execution

Execute the same immutable task manifest used by dry-run and Slurm rendering:

```bash
spires-batch execute resolved-plan.json --events-dir task-events
```

Tasks run in dependency order. Existing outputs are reused only under
`existing_output_policy: reuse_valid` and only after stage-specific scientific
validation. New and updated raw products are reopened and sample-validated
before a success event is written. Deterministic configuration and contract
failures are separated from retryable filesystem, timeout, and resource
failures.

## Slurm rendering

```bash
spires-batch render-slurm resolved-plan.json --output-dir slurm-preview
```

This writes strict dependency array scripts and a `submit.sh` preview using
`slurm/blanca`, the selected partition, and the `spipy14` environment. It never
submits a job. Rendered arrays invoke the same scientific executor used by
serial execution. Operational submission and reservation ownership checks
remain Phase E work.

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

The reservation store can diagnose and protect shared output paths. Automatic
acquisition and ownership checks around submission and execution remain Phase E
work. Preview current conflicts with:

```bash
spires-batch reservations diagnose resolved-plan.json --state-root /product/root
spires-batch reservations list --state-root /product/root
```

The store supports validated completion cleanup once Phase E wraps execution
with reservation ownership. A recovery command previews completed leftovers
and requires `--apply` to remove them:

```bash
spires-batch reservations prune \
    --state-root /product/root \
    --status completed \
    --older-than-days 7
```

Failed or interrupted reservations are never removed because of age alone.
Releasing one requires the exact run ID, task ID, reason, and explicit
`--apply`.
