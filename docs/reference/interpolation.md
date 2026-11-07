# Interpolation behavior and saved products

`spires-batch interpolation` operates the selected NOAA-20 interpolation package
from RittgerLabGroup. It is available as an `interpolation` job in the universal
[workflow configuration](../../examples/workflow.md), or through the standalone commands below.

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

`examples/interpolation.json` configures a WY, with empty daily
mappings to be filled from actual prepared receipts. Run `spires-batch historical
assemble` as described in the [example guide](../../examples/README.md), then plan the
resulting request. Do not invent filenames or feed ordinary daily exports into
this interface. The standalone and unified examples use the same five-tile configuration.

Each tile declares:

- `daily`: date -> logical-field mapping or null. A field is `{path,dataset}`.
  Fields are state, canopy_adjusted_fsca, raw_snow_fraction, sensor_zenith,
  screening_conditions, cloud_adjacency, context, grain_radius, dust,
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
`spires_interpolation.observations.prepare_viirs_observations` from retained inversion values,
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
implementation hashes. Workers reject modified plans or source metadata and incompatible scientific
recipes. Code hashes are provenance, not restart requirements.
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

Planning records a separate `state_context` for the recipe, sensor/platform,
component versions, state/observation formats, fixed parameters, grid, static
context and LUT identity, plus append-only annual mask bindings. It excludes
Python source and native binary hashes. New annual masks extend the context;
existing bindings and file identities must still match. Each observation retains
its own retrieval/R0 provenance. Reuse requires compatible scientific context
and verified observation lineage; changed past observations require replay.

New code with unchanged numerical meaning can reuse history. Native libraries
must expose the supported algorithm version. Immutable plans still retain their
source inventory and integrity digest; regenerate a plan for changed input or
binary paths. The current implementation is the only scientific baseline; there
is no development-algorithm selector or fallback. Public interpolation JSON requires
`config_type: "interpolation"` and `schema_version: 1`. This public version is not
the retired development interpolation format. Plan, observation and checkpoint
format identifiers remain independent; their current value of 2 does not expose
an alternative algorithm. Unsupported scientific state is rejected, even when a
`seed_plan` provenance reference is supplied.

Install the coordinated RittgerLabGroup `main` versions of spires-interpolation
and spires-batch. Build both native libraries explicitly with kernels
`snow_fraction` and `spatial`, and supply `snow_library` and `spatial_library`.
The optional `interpolation` dependency installs the new public API.

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
