# NRT replacement and historical state reuse

The stream keeps an explicit NOAA-20 source inventory for every tile/date. It
prefers VJ109GA over VJ109GA_NRT, then the latest processing timestamp within the
pinned collection (002). Temporary catalog omissions never remove a committed
observation or downgrade a standard product. Unresolved NRT dates remain on the
acquisition list even after seven days; a delayed standard replacement is not lost.
Missing days are explicit calendar entries, not gaps in the state sequence.

First/final describe 0/7 days of temporal context. They are independent of whether
reflectance came from NRT or standard processing. A standard replacement rebuilds
the same-day cloud context and retrievals across the configured donor domain. Snow
state and properties replay from seven days before the earliest changed observation
through current availability. This also corrects already-issued final products
whose seven-day look-ahead included the replaced observation. Prior immutable
generations preserve the products as originally issued.

## Historical bootstrap

Run the historical interpolation stages, including export, with the current
rewindable checkpoint format. Supply an inventory JSON mapping `tile/YYYY-MM-DD`
to `source_record(path)` records, or null for an explicitly missing observation.
The inventory must match the prepared observations used by the historical plan.

```bash
spires-batch nrt bootstrap historical-plan.json inventory.json --root /products/noaa20-nrt
spires-batch nrt status --root /products/noaa20-nrt
```

The stream publishes one domain-wide current.json pointer containing a ledger and
all tile checkpoints. Consumers resolve that pointer once. The ledger's `products`
map locates each tile/date/issue in immutable NetCDF files and records its time
index and file identity. Keep referenced historical and revision product files.
A per-tile worker export is staging for the domain commit; it is not the public
NRT commit point. No earlier generations are deleted automatically.

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
The selected algorithm-6 settings and viirs_s2_083 canopy policy are fixed. Corrected
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
`observation_policy`, and `tiles[tile][water_year]` retrieval requests. The science
and cloud interpreters must have the coordinated production package versions.
Cloud inference runs in its dedicated environment. This command does not install
cron entries or submit Slurm jobs implicitly. Its enclosing allocation must obey
the site's cloud-resource and node-exclusion policy.

The observation policy must equal the historical plan's
`asset_provenance.observation_policy` and include the cloud model manifest SHA256.
Models, annual masks, climatology, backgrounds and grid context are versioned
scientific inputs. Missing annual assets or incompatible contexts fail explicitly;
this implementation does not invent a WY2027 mask/R0 or silently reuse a different
climatology. A stream currently covers one target water year. Register and
bootstrap the next target-year context before crossing October 1.

## Replay and publication guarantees

Checkpoints retain 66 observation days: the 45-day magnitude window, seven issue
days, and 14 replacement days. Persistent pre-tail snow presence and cloud-context
state do not expire. Variable-specific valid-observation histories and their
pre-tail ages permit a correct rewind; using only the last onset flag or latest
age would retain information from the superseded input.

A compatible current checkpoint is used first. An older committed generation can
supply the restart when its retained windows and source prefix are compatible.
Otherwise the plan explicitly falls back to the full scientific history. Changed
input dates are never silently clipped to a seven-day repair window. Water-year
age resets, zero barriers, direct-only temporal donors and scientific first/final
cutoffs are unchanged. The state identity includes scientific code content using
package-relative names, so identical installations can share state. Execution
plans still pin exact deployed source locations and input metadata.

Each update carries its expected parent generation. All tile products and new
states must validate before the domain pointer advances; a concurrent stale
update fails. Exact retries reuse the current committed update, and a no-change
poll does not create another generation. A failed preparation, worker or export
leaves the previous public ledger intact. Keep cycle workspaces referenced by the
ledger; cleanup requires a separate reachability policy.

## Validation scope

Bounded revision checks compare replayed products and all checkpoint arrays to a
full rerun after a standard replacement. They cover standard preference, overdue
NRT rechecks, missing/new calendar days, exact retry and no-op behavior. Real native
input preparation and selected retrievals are checked against frozen run012 data.
Full-grid reader/cloud/spatial pilots measure actual allocations separately from
these correctness checks. A scheduled live service still needs registered current
water-year assets and an operational bootstrap; historical validation is not an
as-issued NRT accuracy assessment.

Repeated acquisition cycles reuse unchanged committed native files, including
when the new cycle has a different workspace. Standard replacements and newer
processing timestamps get new downloads. The controller validates its registered
policy and water-year boundary before acquisition. Size a single-allocation
`cycle --execute` for the maximum requested resources across stages; stage-specific
Slurm rendering is available for the interpolation plan.

## Coordinated installation

Use the matching RittgerLabGroup `spires-interpolation`, `spires-batch`, and
`viirs-cloudmask-batch` versions. Native standard/NRT matching also requires
SPIReS-Organization/spires-io commit
`d2d246304e3b861523c541150fd1dee5ccec0682` (currently on
`feat/viirs-nrt-input`) or a descendant containing that change. The legacy
`spires-batch[science]` extra follows spires-io/main and does not itself guarantee
this reader version; explicitly pin the coordinated reader when building the
science environment.

Install the cloud package's matched preparation module in the science environment
without replacing that environment's scientific dependencies, and use the cloud
package's declared dependencies in a separate inference environment. Set both
interpreter paths explicitly in the cycle configuration. Do not install the
cloud inference dependency pins over the production inversion environment.
