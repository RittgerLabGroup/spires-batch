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
during preflight. The Phase E0 controlled serial exit gate passed on
2026-08-01. Phase E1-E5 now provide audited preparation, reservation,
test-only validation, live Slurm submission with durable job identities, and
worker-side ownership enforcement with outcome-derived reservation state.
Operational runs add strict stage gates, scheduler reconciliation, and capped
automatic retries through small `afterany` coordinator jobs.

## Installed capabilities

- Versioned JSON request and resolved-manifest schemas.
- Stable configuration, plan, run, and task identities.
- Explicit-file and configuration-driven CURC discovery.
- Four-layer schema, semantic, inventory, and metadata preflight.
- Optional validated staging from authoritative storage to shared scratch.
- Dry-run and scientific serial execution backends.
- Profile-driven Blanca or Alpine Slurm rendering with explicit cluster,
  partition, account, and QoS selection.
- Immutable submission records, mutable-readiness rechecks, all-or-none output
  reservation acquisition, and audited pre-submission rollback.
- Non-mutating `sbatch --test-only` records and single-use live submission with
  durable group, job, array-element, and reservation identities.
- Worker-side reservation verification immediately before scientific writes,
  failed-state retention, and automatic cleanup after validated completion is
  durably recorded.
- Stage-gated operational waves, scheduler-derived terminal failures,
  audited same-family retry reservation re-arming, and capped automatic retry
  submission.
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

The default installation provides the Pydantic-only planning core. Install the
merged scientific runtime from a source checkout with:

```bash
pip install '.[science]'
```

The `science` extra follows the merged `main` branches until compatible
scientific package releases are published.

### Coordinated scientific stack

On CURC, sibling editable checkouts remain the preferred development and
operational environment. From an environment with `mamba` available, run:

```bash
module load miniforge
mamba run -n spipy14 bash scripts/bootstrap_local_phase_d.sh --merged
```

The bootstrap verifies and installs this merged stack editably, without
resolving dependencies from GitHub:

- `spires-contract/main`
- `spires-io/main`
- `spires-r0/main`
- `spires-inversion/main`
- `spires-postprocess/main`
- `RittgerLabGroup/spires-batch/main`

It never checks out, merges, or rebases a branch. Use `--verify-only` to inspect
an existing environment. On CURC, the bootstrap uses `/usr/bin/gcc` and
`/usr/bin/g++` for the inversion extension to avoid the `spipy14` conda
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

This writes strict dependency array scripts and a `submit.sh` preview using the
profile's explicit Slurm cluster, partition, account, QoS, and environment. The
built-in profiles are `blanca-snow`, `blanca-rittger`, and Alpine's `acpu`
CPU partition; custom profiles must supply both cluster and partition.
Rendering never submits a job. Rendered arrays invoke the same scientific
executor used by serial execution.

## Audited submission and scheduling

Phase E1/E2 prepare the exact scheduler intent and reserve outputs without
calling `sbatch`. Phase E3 adds the non-mutating scheduler gate and the
single-use live transition:

```bash
spires-batch submission prepare resolved-plan.json \
    --state-root /product/root \
    --output-dir submission-preview

spires-batch submission reserve \
    submission-preview/submission.json

spires-batch submission test-only \
    submission-preview/reservation-set.json

spires-batch submission submit \
    submission-preview/reservation-set.json
```

Preparation reloads and verifies both manifest digests, rechecks preflight,
validates current external or staged inputs, confirms output policies and
writable parents, diagnoses every intended reservation, renders immutable
Slurm scripts and indices, hashes every artifact, and writes
`submission.json`. It does not mutate reservation state.

Reservation acquisition repeats the mutable readiness checks, verifies that
the manifest and rendered artifacts still match their recorded hashes, and
acquires the complete output set. If any acquisition conflicts, reservations
created earlier by the same operation are removed with an audit event. The
successful set is retained as immutable `reservation-set.json`; submission
lifecycle transitions are appended to `submission-events.jsonl`.

`test-only` rechecks the manifest, rendered hashes, mutable inputs and outputs,
and exact reservation ownership before invoking `sbatch --test-only` for every
array. Its responses are retained in immutable `scheduler-test.json`; no jobs
are created. `submit` requires that matching test record, repeats all checks,
uses an exclusive submission lock, executes the dependency-ordered
`sbatch --parsable` commands, appends each returned job identity immediately,
attaches the base job and array index to every affected reservation, and writes
immutable `scheduler-submission.json`. Each script carries an explicit Slurm
cluster and a recoverable run/group comment.

Submission-rendered workers load the immutable reservation set and match every
task output against its run, plan, submission, group, cluster, base job, and
array-element identities. Ownership is checked at worker startup and again
immediately before a scientific write. The terminal task event is durably
recorded before reservations transition: validated success and
`loaded_existing` remove completed reservations after audit, while failures
remain in audited `failed` state for explicit recovery or a later retry.

If scheduler submission will not follow, roll back the unsubmitted set
explicitly:

```bash
spires-batch submission rollback-reservations \
    submission-preview/reservation-set.json \
    --reason "operator cancelled before submission"
```

Rollback prevalidates the complete set and refuses to remove anything if any
reservation carries a Slurm job ID.

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

## Stage-gated operational execution

E5 wraps the immutable E1-E4 submission lifecycle in strict operational waves:

```bash
module try-load slurm/blanca

spires-batch submission start-operational resolved-plan.json \
    --state-root /product/root \
    --output-dir operational-run
```

This is a live scheduler mutation. The command validates and submits only
dependency-free tasks, then submits a small coordinator with an `afterany`
dependency on that wave. The coordinator:

- trusts exact worker terminal events when present;
- uses `sacct` to terminalize workers that exited without an event;
- treats node failure, boot failure, preemption, timeout, and out-of-memory as
  transient scheduler failures;
- treats cancellation and unclassified scheduler/protocol failures as
  non-retryable;
- re-arms only failed reservations with the same manifest family, task,
  configuration, and output identity;
- writes and submits immutable retry manifests until
  `max_auto_retry_count` is exhausted; and
- releases a downstream wave only after every task in the preceding wave has
  validated success.

On Blanca the coordinator uses `module try-load slurm/blanca`; installations
with the Slurm client already on `PATH` therefore continue safely when that
module name is unavailable.

Successful-subset release is intentionally unsupported. Any deterministic,
cancelled, or retry-exhausted failure durably blocks every unsubmitted
downstream task. Each wave retains its submission, reservation, scheduler,
events, retry lineage, coordinator, and advance artifacts beneath
`operational-run/waves/`.

The coordinator normally invokes the following single-use command itself:

```bash
spires-batch submission advance operational-run/operation.json \
    --wave-dir operational-run/waves/0001-initial
```

An `advance.lock` retained without `advance-result.json` means advancement
failed after scheduler mutation began. Inspect that wave's submission events,
scheduler record, reservations, and coordinator log before any recovery; the
lock prevents an unsafe duplicate submission.

## Output reservations

The reservation store diagnoses and protects shared output paths. E2
automatically acquires the complete submission set, E3 durably attaches
scheduler identities, and E4 verifies those identities in workers before
writes and derives reservation state from terminal task outcomes. Preview
current conflicts with:

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
