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
- `agriculture`: required Boolean field.
- `coastal`: Boolean field required when `use_snow_climatology` is enabled.
- `frequency`: 12 monthly fields, optionally indexed in one file, required when
  `use_snow_climatology` is enabled. These are the
  target-WY-excluded frequencies for historical evaluation, fixed throughout the
  entire onset prefix. An operational stream retains its registered climatology
  across October 1; changing that science context is a separate revision.

Set `use_snow_climatology` in the interpolation request (or the interpolation
job's `request` in a unified workflow):

```json
"use_snow_climatology": false
```

The default is `true`; only JSON Booleans are accepted, not strings, numbers or
`null`. Disabled mode fixes the onset frequency weight at one and removes the
rare-snow veto. Canopy handling, playa/bright-soil and agriculture screening
thresholds and onset floors, and the other cloud/SWIR screening remain unchanged.
For valid canopy fraction `C`, onset uses `max(existing_floor, 0.2 * (1 - C))`.

When disabled, `frequency` and `coastal` can be omitted. If supplied, planning
removes those bindings from the effective request before resolving paths,
inventorying files or checking dataset headers. They are not part of the saved
scientific context or archived input assets; the source JSON is unchanged. This
allows the switch to be flipped without deleting existing monthly mappings.
Annual bright-soil and agricultural masks are still required. Initialization
uses its own asset list: omit climatology assets from that job as well when
configuring a workflow that must operate without the climatology product.

The daily observation boundary is produced by
`spires_interpolation.observations.prepare_viirs_observations` from retained inversion values,
matched QA, corrected neighboring cloud masks and SWIR residual scores. Canopy
correction must be the selected VIIRS policy. This driver consumes prepared
observations; it does not launch acquisition, annual R0/mask building, inversion,
or cloud preparation. Those inputs can be produced with the corresponding
scientific/cloud packages and registered explicitly.

Always configure the complete donor domain when reproducing the evaluated
five-tile workflow. Split WY2023-2026 into target-year requests with their own
climatology context (when enabled) and output roots. All tiles require equivalent annual masks;
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

The resolved Boolean is recorded in the plan and inherited by NRT templates.
Exported NetCDFs, including cumulative NRT products, record the global attribute
`use_snow_climatology` as integer 0 or 1. The mode applies to every issue and is
independent of `output_issues`.

Changing modes requires replay from `history_start`; checkpoints from the other
mode are rejected. Use separate output roots and registered streams for enabled
and disabled comparisons. Omitted and explicit `true` preserve the current
enabled scientific identity; older compatible enabled contexts represent it by
the absence of the field. No checkpoint-layout change is required.

All calendar date bounds are inclusive. `start == stop` selects one output day.
For WY2026, use `start: "2025-10-01"`, `stop: "2026-09-30"`, and
`available_through: "2026-10-07"` to write all eight issues through September 30.
Daily preparation must also run through October 7, including the next WY's
retrieval assets and annual interpolation mask. The workflow does not add those
seven context days automatically. See [date conventions](configuration/reading.md#inclusive-calendar-dates).

Screen stripe files retain selected observation states/validity and all eight fSCA
issues. Spatial tile/date files retain both property estimates and donor support.
Property stripes retain fSCA, grain/dust, source codes, ages, flat/terrain albedo,
QA flags and context provenance. Export reads each property stripe once into
the selected NetCDF files, with appropriate issue lengths and scientific metadata.
Outputs and checkpoint bundles commit through one atomic current.json manifest.

Set `output_issues` in the interpolation configuration (or the interpolation job's
`request` in a unified workflow):

```json
"output_issues": "all_issues"
```

`all_issues` (the default) writes `first.nc`, `second.nc`, `third.nc`, `fourth.nc`,
`fifth.nc`, `sixth.nc`, `seventh.nc`, and `final.nc`. Use
`"output_issues": "first_and_final_only"` to write only `first.nc` and `final.nc`,
or `"output_issues": "final_only"` to write only `final.nc`.
Restart checkpoints are retained in every mode.
This controls output files, not the calculations or donor eligibility.
Every selected file contains the full set of variables: fSCA and viewable fSCA,
grain radius, dust, both albedos, source codes, observation ages, donor diagnostics,
QA flags and cloud-context provenance.

With input availability through D, issue ages 0–7 end at D through D−7,
clipped to the requested interval. They share the requested start date. A selected
issue with no eligible dates is an empty NetCDF with the complete variable schema;
it never contains partially matured dates. Metadata records `issue`, `issue_number`
(1–8), `future_days` (0–7), and the fixed direct-observation donor policy.
The registered NRT stream inherits `output_issues` and publishes cumulative
tile/WY files as described in [NRT publication](nrt.md).

The exporter uses full-width storage chunks (up to 8 days × 20 rows × tile width),
a 4 MiB cache per variable/file, and transfers up to 223 days at a time within a
512 MiB array budget. Longer intervals are split automatically. Lossless zlib
level 2 and shuffle remain enabled. On the h10v04 benchmark this combination
reduced median eight-file export time by about 67%, with about 10% larger files;
shared-filesystem load affects timings. These settings apply to historical and
NRT replay-interval exports. Cumulative NRT snapshots retain their separate
incremental copy/update layout.

Rebuild the snow native library for ABI version 2 before running this code.
The eight-issue layout has issue schema 2. Old two-issue plans/checkpoints require
a historical replay and a new NRT bootstrap; missing intermediate history cannot
be recovered by relabeling the first/final files. First/final interpolation rules
and screened direct-observation donor eligibility are unchanged.

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
`config_type: "interpolation"` and draft `schema_version: 1`, with inclusive dates.
For an earlier draft request with an exclusive `stop`, subtract one day from that
`stop` and regenerate the plan to preserve its output interval; keep version 1. Plan, observation and checkpoint
format identifiers remain independent; their current value of 2 does not expose
an alternative algorithm. Unsupported scientific state is rejected, even when a
`seed_plan` provenance reference is supplied.

Install the coordinated RittgerLabGroup `main` versions of spires-interpolation
and spires-batch. Build both native libraries explicitly with kernels
`snow_fraction` and `spatial`, and supply `snow_library` and `spatial_library`.
The optional `interpolation` dependency installs the new public API.

## Development evidence

A bounded synthetic two-stripe case with the frozen real albedo LUT passed all
four stages, NetCDF issue export, checkpoint publication and exact retries.
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
