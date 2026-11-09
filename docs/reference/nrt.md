# NRT replacement and historical state reuse

The stream keeps an explicit NOAA-20 source inventory for every tile/date. It
prefers VJ109GA over VJ109GA_NRT, then the latest processing timestamp within the
pinned collection (002). Temporary catalog omissions never remove a committed
observation or downgrade a standard product. Unresolved NRT dates remain on the
acquisition list even after seven days; a delayed standard replacement is not lost.
Missing days are explicit calendar entries, not gaps in the state sequence.

The eight issues describe 0–7 days of temporal context (first through final).
They are independent of whether
reflectance came from NRT or standard processing. A standard replacement rebuilds
the same-day cloud context and retrievals across the configured donor domain. Snow
state and properties replay from seven days before the earliest changed observation
through current availability. This also corrects already-issued final products
whose seven-day look-ahead included the replaced observation. Prior immutable
generations preserve the products as originally issued.

## Register existing history once

Use completed historical output, including export, with the rewindable checkpoint
format. Supply an inventory JSON mapping `tile/YYYY-MM-DD`
to `source_record(path)` records, or null for an explicitly missing observation.
The inventory must match the prepared observations used by the historical plan.
Historical output must extend through `available_through` (`stop` is that same
inclusive date), so the cumulative first issue has complete date coverage at handoff.
The command name `bootstrap` refers only to this one-time stream registration:
it adopts existing products and checkpoints without recomputing or resampling
them. It is not repeated at a water-year boundary. Plans from an older controller
are accepted when their numerical implementation and scientific assets match.

```bash
spires-batch nrt bootstrap historical-plan.json inventory.json --root /products/noaa20-nrt
spires-batch nrt status --root /products/noaa20-nrt
```

The stream publishes one domain-wide current.json pointer containing a ledger and
all tile checkpoints and cumulative tile/WY NetCDFs. Consumers resolve that pointer
once. Artifact names are `product_<tile>_WY<year>_<issue>` and files are
`<tile>_WY<year>_<issue>.nc`. With `output_issues: all_issues` in the registered
interpolation configuration, all eight files contain all variables. With
`first_and_final_only`, only the first and final files are written. The setting is
inherited by subsequent updates. Enabling missing intermediate history in an
existing first/final stream requires a historical replay and new bootstrap.

Each cumulative product begins at October 1 (or the historical output start) and
ends at the earlier of September 30 and current availability minus its issue age.
Thus during a water year, first is one day longer than second and seven days
longer than final. Selected issues with no mature dates have empty time axes.
Replacement observations revise affected dates in the new generation while old
generations retain the as-issued values. Unchanged cumulative files are reused;
changed files are rewritten with bounded memory. This adds full-file I/O and
archive storage for each changed cumulative snapshot, in addition to the interval
products. Select first/final only when intermediate files are not needed.

The ledger's `issue_products` map also records durable archive copies of these
cumulative files. Its `products`
map locates recent tile/date/issue records in durable immutable NetCDF files and
records their time indexes and identities. Older records are indexed in the
ledger's monthly `catalog`; `nrt catalog` resolves both current and as-issued
archives. A per-tile worker export is staging for the domain commit, not the public
NRT commit point. [Storage and retention](storage.md) manage recovery checkpoints
and workspace cleanup separately from the permanent scientific product archive.

## Acquisition, preparation and execution

```bash
spires-batch nrt acquire --root /products/noaa20-nrt --through 2026-09-30 \
  --output /work/cycle/acquisition --download
spires-batch nrt prepare retained-request.json --output prepared.h5 --receipt source.json
spires-batch nrt plan candidates.json --root /products/noaa20-nrt \
  --through 2026-09-30 --output /work/cycle/revision
# Execute inside an appropriately sized compute allocation:
spires-batch nrt run /work/cycle/revision/update.json
```

CMR discovery checks both products. LANCE's collection-version metadata differs
from the filename collection; the adapter filters the exact acquisition day and
collection from granule filenames after a padded temporal query. Catalog/network
errors fail the acquisition instead of masquerading as missing observations.
Downloads use existing Earthdata netrc credentials and temporary cookies, validate
HDF5 structure, and rename atomically. Granule revisions get separate cache paths.
The acquisition receipt is written before scientific processing.

`prepare` consumes explicit HDF5/NetCDF field bindings for retained raw inversion,
canopy magnitude, flags, matched QA, geometry and corrected cloud/context rasters.
Fields support `index` for a leading context axis, `band` for a trailing parameter
axis, and `bitwise_and` for an explicit integer flag transform. Trailing parameter
reads share one HDF5 selection. Cached positive-SWIR flags must cover every newly
eligible snow observation. With a `spectral` specification, missing scores are
reconstructed from the original observed spectrum, background, retained parameters
and LUT; an SSE comparison rejects inconsistent provenance.

`nrt retrieve` produces these prepared fields directly from a new standard or NRT
reflectance scene. Its JSON request supplies source metadata, preparation options,
reflectance LUT, native x/y axes, ancillary maps including ice fraction, previous
summer background bindings (`baseline`, optional `screened` plus `index`), and the
background's availability date. It retains original parameters/SSE, selected
background membership and SWIR scores alongside normalized interpolation fields.
The selected inversion settings and `viirs_canopy_weighted_shade` canopy policy
are fixed; retrieval explicitly passes `sensor="viirs"` to postprocessing. Corrected
clouds apply after inversion so newly clear observations retain usable retrievals.
It uses maintained I/O, inversion and canopy APIs; it imports no research runtime.

`prepared_context` is the digest returned by `day_context(inventory, tiles, date)`.
Every nonmissing tile on a changed date must carry that same selected-source
context. This check prevents stale neighboring cloud halos or spatial donors from
entering a revision. The controller conservatively rebuilds the complete domain.

For a complete acquisition/preparation/interpolation cycle, `nrt cycle CONFIG
--through DATE --output DIRECTORY --execute` runs in a compute allocation. Omit
`--execute` to stop after preparing the immutable revision plan. Configuration
provides `stream_root`, `science_python`, `cloud_python`, `cloud_model`, collection,
`observation_policy`, and `tiles[tile][water_year]` retrieval requests. Optional
`annual_bright_soil[tile][water_year]` entries register additional annual masks.
The science
and cloud interpreters must have the coordinated production package versions.
Cloud inference runs in its dedicated environment. This command does not install
cron entries or submit Slurm jobs implicitly. Its enclosing allocation must obey
the site's cloud-resource and node-exclusion policy.

After whole-domain cloud inference, tile retrievals run concurrently, each with
one inversion worker and one numerical-library thread. `retrieval_concurrency`
defaults to the number of nonmissing tiles; set a smaller positive integer only
to deliberately queue tiles. `retrieval_memory_gib` defaults to 6 GiB per worker,
plus 2 GiB coordinator headroom per allocation. Five concurrent tiles therefore
need at least `--cpus-per-task=5 --mem=32G`. Known undersized Slurm allocations
fail before preparation instead of silently falling back to serial retrieval.
These are reservation checks, not per-process memory limits. Cloud inference
and subsequent interpolation may need larger reservations. Each retrieval's
single-worker setting remains fixed until multi-worker scaling is benchmarked.

Alternatively, set `preparation_memory_gib` to a **total** date budget instead
of `retrieval_memory_gib`; specifying both is an error. The measured five-tile
example now uses `preparation_memory_gib: 16` with `--cpus-per-task=5 --mem=16G`.
This total already includes cloud inference and coordinator overhead; nothing
is added or multiplied. JSON validates the allocation but does not resize it.
The concurrent winter pilot peaked at 9.43 GiB; contrasting dates are being
validated before expanding the campaign at this lower reservation.

The observation policy must equal the historical plan's
`asset_provenance.observation_policy` and include the cloud model manifest SHA256.
Models, annual masks, climatology, backgrounds and grid context are versioned
scientific inputs. Missing annual assets or incompatible contexts fail explicitly;
this implementation does not invent a WY2027 mask/R0 or silently reuse a different
climatology when it is enabled.

The stream inherits `use_snow_climatology` from its historical interpolation
request; omission means `true`. Disabled streams retain no frequency/coastal
input bindings and do not require climatology assets. Changing the mode requires
replaying history from `history_start` and registering the resulting compatible
history in a separate stream. Editing a template does not make checkpoints from
the other mode compatible. Cumulative NetCDF products record the resolved mode
in the integer global attribute `use_snow_climatology` (0 or 1).

## Continuous history across October 1

A stream continues across water years. For example, October 2026 uses the retained
August/September 2026 history, including values generated with the older R0.
There is no annual state reset, history regeneration, resampling or second stream
registration. New observations use the retrieval recipe for their own water year;
retain prior-year recipes so a September NRT-to-standard replacement still uses
the September R0. Such a replacement replays forward across October 1, and October
observations can finalize the last seven September products in the same update.

Register the new retrieval recipes under `tiles[tile]["2027"]` and the new masks
in the cycle configuration, for every tile in the domain:

```json
{
  "annual_bright_soil": {
    "h09v04": {"2027": {"path": "/assets/h09v04/bright-WY2027.h5", "dataset": "bright"}}
  }
}
```

These entries supplement the ledger's existing masks; replacing an existing
binding or changing its file is rejected. Standalone `nrt plan` accepts the same
tile/year mapping via `--annual-bright-soil FILE`. Cycle mask paths are relative
to the cycle configuration; standalone mappings use the current working directory.
Missing annual assets fail explicitly. When enabled, the registered monthly
climatology stays fixed across the transition; disabled streams remain disabled.

Streams must be bootstrapped from current scientific plans/checkpoints. Their
internal format identifier is 2, independently of the public configuration
baseline (`config_type: "nrt_cycle"`, `schema_version: 1`). It does not select
an older or alternative algorithm.
`seed_plan` records provenance; it does not make obsolete numerical state
compatible. New registrations record their historical reference automatically.

## Replay and publication guarantees

Checkpoints retain 66 observation days: the 45-day magnitude window, seven issue
days, and 14 replacement days. Persistent pre-tail snow presence and cloud-context
state do not expire. Variable-specific valid-observation histories and their
pre-tail ages permit a correct rewind; using only the last onset flag or latest
age would retain information from the superseded input.

A compatible current checkpoint is used first, using coverage metadata rather
than loading its raster arrays during planning. An older hot or archived generation
can supply an earlier restart when its retained windows and source prefix are compatible.
Otherwise planning stops with exit status 3 and a durable full-replay size report;
it never automatically transfers or replays the full scientific history. Changed
input dates are never silently clipped to a seven-day repair window. The selected
policy's within-water-year observation-age diagnostic, zero barriers, direct-only
temporal donors and scientific first/final cutoffs are unchanged; the diagnostic's
October reset does not erase interpolation donors or snow state. State context
includes explicit recipe/component versions, formats, parameters, grids and
scientific assets, not numerical source hashes. Readability-only code changes
therefore do not invalidate history. Execution plans retain code provenance,
source locations and input metadata; each plan itself remains immutable.

Each update carries its expected parent generation. All tile products and new
states must validate before the domain pointer advances; a concurrent stale
update fails. Exact retries reuse the current committed update, and a no-change
poll does not create another generation. A failed preparation, worker or export
leaves the previous public ledger intact. Published payloads are archived before
the pointer advances, so successful workspaces can later be removed by the explicit
[reference-aware retention policy](storage.md). Planned/running workspaces remain pinned.

## Validation scope

Bounded revision checks compare replayed products and all checkpoint arrays to a
full rerun after a standard replacement. They cover standard preference, overdue
NRT rechecks, missing/new calendar days, exact retry and no-op behavior. Real native
input preparation and selected retrievals are checked against the evaluated reference data.
Full-grid reader/cloud/spatial pilots measure actual allocations separately from
these correctness checks. A scheduled live service still needs registered current
annual assets and a one-time registration of existing history; historical validation is not an
as-issued NRT accuracy assessment.

Repeated acquisition cycles reuse unchanged committed native files, including
when the new cycle has a different workspace. Standard replacements and newer
processing timestamps get new downloads. The controller validates its registered
policy and new annual asset coverage before acquisition. Size a single-allocation
`cycle --execute` for the maximum requested resources across stages; stage-specific
Slurm rendering is available for the interpolation plan.

## Coordinated installation

Use the coordinated RittgerLabGroup scientific packages in `spipy14`.
Cloud preparation and inference both run in the separate cloud environment,
which has no SPIReS package dependencies. Set both interpreter paths explicitly.
See [environment setup](../setup.md); do not install cloud inference dependencies
over the science environment.
