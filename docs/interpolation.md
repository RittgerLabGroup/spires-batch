# Historical interpolation control plane

`spires-batch interpolation` operates the selected NOAA-20 interpolation package
from RittgerLabGroup. It is separate from existing inversion requests so existing
manifest digests, scientific APIs and operational submissions remain unchanged.

```bash
spires-batch interpolation plan request.json --output plan.json
spires-batch interpolation render-slurm plan.json --output launch
spires-batch interpolation status plan.json
# After resource pilots and maintenance, explicitly submit the rendered chain:
bash launch/submit.sh
```

The module form is `python -m spires_batch.interpolation`. No plan/render/status
command submits jobs. Individual workers use `execute-task PLAN STAGE INDEX`.
Stage order is screening -> spatial -> properties -> export. Rendered Slurm arrays
use at most 400 elements per submission; batches are chained with afterok so the
configured concurrency remains a campaign bound. Node exclusions are snapshotted
from the supplied scheduler policy. Accepted job IDs are recorded in
`submissions.tsv`. A failed stage prevents downstream execution; rerender/retry
uses verified completed stripe/date outputs. The scientific workers lock outputs.

## Request schema

`examples/historical_interpolation.json` illustrates a one-day request. Expand its
explicit daily mapping to every calendar day from history_start through
available_through, using null for an actually missing scene. Never omit dates.
Start is inclusive, stop exclusive, availability inclusive. Initial historical
targets start October 1; checkpoint-backed updates can start on any date and
continue across October 1. First/final issues follow the same 0/7-day policy as
research. A request uses a fixed climatology context and selects annual masks by
observation date.

Each tile declares:

- `daily`: date -> logical-field mapping or null. A field is `{path,dataset}`.
  Fields are state, direct, raw, zenith, flags, adj, context, grain_radius, dust,
  screening_flags, solar_zenith and solar_azimuth. Different fields can point to
  different files; the worker opens each file once per read group.
- `terrain`: dem, slope, aspect, canopy_fraction and native projected x/y.
  Trailing parameter axes use `band`; integer flags can use `bitwise_and`.
  One-dimensional axes use `axis: "x"` or `axis: "y"`.
- `bright_soil`: water-year -> field, with optional `index` for a stacked source.
- `agriculture` and `coastal`: Boolean fields.
- `frequency`: 12 monthly fields, optionally indexed in one file. These are the
  target-WY-excluded frequencies for historical evaluation, fixed throughout the
  entire onset prefix. An operational stream retains its registered climatology
  across October 1; changing that science context is a separate revision.

The daily observation boundary is produced by
`spires_interpolation.observations.prepare_day` from retained inversion values,
matched QA, corrected neighboring cloud masks and SWIR residual scores. Canopy
correction must be the selected VIIRS policy. This driver consumes prepared
observations; it does not launch acquisition, annual R0/mask building, inversion,
or cloud preparation. Those inputs can be produced with the corresponding
scientific/cloud packages and registered explicitly.

Always configure the complete donor domain when reproducing the evaluated
five-tile workflow. Split WY2023-2026 into target-year requests with their own
climatology context and output roots. All tiles require equivalent annual masks;
missing context fails preflight. R0 builders retain the maintained all-summer
in-memory policy. Models, LUTs and ancillary assets are externally versioned data,
not files to commit in this repository.

Planning checks source dataset/grid headers, freezes size/mtime identities for
large inputs and checksums the LUT/native libraries/scheduler policy, and records
implementation hashes. Workers reject modified plans, source metadata or code.
This is not a full pixel-content scan of every large input. Scientific asset
provenance and source stream (`standard`, `nrt`, `mixed`) are explicit.

## Outputs and restart

Screen stripe files retain selected observation states/validity and both fSCA
issues. Spatial tile/date files retain both property estimates and donor support.
Property stripes retain fSCA, grain/dust, source codes, ages, flat/terrain albedo,
QA flags and context provenance. Export reads each property stripe once into
first/final NetCDF files, with appropriate issue lengths and scientific metadata.
Outputs and checkpoint bundles commit through one atomic current.json manifest.
A final issue with no eligible dates is omitted rather than emitted prematurely.

The checkpoint bundle includes bounded snow state, direct property history and
variable-specific ages. It supports the [NRT revision controller](nrt.md); this command
implements historical processing. Scheduled NRT deployment is not enabled implicitly. Historical maps computed with standard
inputs do not establish as-issued NRT performance.

Planning records a separate `state_context` for the numerical implementation,
grid, static context and native/LUT identities, plus append-only annual mask
bindings. It does not depend on target dates, output roots, controller code or the
standard/NRT stream label. New annual masks extend the context without invalidating
state; existing bindings and file identities must still match. Each prepared
observation retains the retrieval and R0 used for its own date. The publication's
plan identity still pins all deployed code and its exact source inventory.
Reuse requires both compatible state context and verified observation lineage;
changed past observations require replay. A seed inherits its existing
`state_identity`, including when reading a legacy checkpoint.

Older rewindable bundles without `state_context` can be used with `seed_plan`
pointing to their original historical plan. The planner verifies that plan's
digest, numerical code, assets and checkpoint identity. This adds provenance
metadata to the next published bundle without rebuilding the old state.

For review, install the coordinated feature branches of spires-interpolation and
spires-batch (or set PYTHONPATH to both checkouts). The optional dependency pointing
to interpolation's main branch becomes sufficient only after that branch is merged.

## Development evidence

A bounded synthetic two-stripe case with the frozen real albedo LUT passed all
four stages, first/final NetCDF export, checkpoint publication and exact retries.
Numerical migration checks compare the production policy/native kernels against
frozen research code. Full-grid memory/throughput pilots are required before bulk
submission. No historical or cloud production jobs were submitted by migration.

Scheduler resources can be overridden per stage with `scheduler.stages`, keyed by
`screen`, `spatial`, `properties`, or `export`. Each override accepts `cpus`,
`memory`, `time`, and `concurrency`; the top-level settings remain defaults.
The spatial native kernel uses that stage's CPU count. This lets full-domain
spatial jobs use several cores without allocating idle cores to serial stripes.

Seeded revision plans validate and pin the observations actually replayed from
`start` onward, together with their checkpoint and all scientific context assets.
They retain the full lineage in the request without reopening the entire original
observation archive on every NRT cycle.
