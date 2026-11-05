# Historical acceptance campaign: h08v05, WY2026

The useful training artifact is a real historical run that can continue into
incremental operation. Rebuild R0, matched clouds, inversions and prepared
observations with the centralized candidate. Preserve older scratch results for
comparison; do not overwrite them or count their existence as validation of the
new code. Reuse externally generated ancillary layers and LUTs only after their
grids, units and scientific identities have been checked.

## Inventory and practical first interval

A filename-only inspection on 2026-11-05 found, under
`/pl/active/rittger_ops/vj109ga.002/input/h08v05`:

| Interval | Available dates | Use |
| --- | --- | --- |
| 2025-06-01 through 2025-09-30 | 122/122 | Fresh previous-summer R0 for WY2026 |
| 2025-10-01 through 2026-05-18 | 230/230 | Initial historical campaign |
| 2026-05-19 through 2026-09-30 | 0/135 | Acquire later to finish the WY |

This is not a content-integrity check and must be refreshed before execution.
The existing assets directory `/pl/active/rittger_ops/spipy` was empty. Therefore
no full processing run is claimed or submitted by this documentation update.

Candidate assets also exist outside that new layout: prior retrieval requests
point to `/scratch/alpine/ropa5718/spipy/input/viirs/noaa20/ancillary` and
`/scratch/alpine/ropa5718/lut`, and the cloud package has a selected model
manifest. These are leads to validate, not proof of complete interpolation
bindings or permission to reuse old inversion products. The empty new directory
does not mean those external assets need to be recreated blindly.

Start with the available 230-day interval rather than downloading a full year
solely for a demonstration. Set `history_start` and `start` to `2025-10-01`,
`stop` to `2026-05-19` (exclusive), and `available_through` to `2026-05-18`
(inclusive). First output can cover May 18; final output can only cover May 11
with the seven-day context requirement. Extending this same run is preferable
to making a separate toy NRT dataset.

h08v05 is the requested focus tile. The checked-in example explicitly uses a
single-tile domain; its reduced donor context is **not** equivalent to the
evaluated five-tile domain. Decide and fix the production donor domain before
acceptance processing. If neighbors are included, inventory and prepare them
for every date and bind their assets too. Do not change the domain at NRT handoff.

## Prepare the real inputs

1. Copy `examples/operations/h08v05-wy2026/` into a versioned run directory.
   Complete the asset bindings in `tile_initialization.json`. Initialization
   creates/registers paths; it does not manufacture the missing files.
2. Register reflectance/albedo LUTs, the matched cloud model bundle, native
   interpolation libraries, tile axes/DEM/slope/aspect/canopy/ice/water context,
   agriculture/coastal masks, annual bright-soil mask, and all 12 monthly snow
   frequencies. Use appropriate target-WY-excluded climatology for historical
   evaluation and retain that same registered context for incremental operation.
3. Inventory shared reflectance first. Treat a missing local download differently
   from an actually unavailable observation. Do not fill an incomplete download
   inventory with scientific `null` observations to make a plan pass.
4. Use the private `RittgerLabGroup/earthdata-downloads` helper for missing
   historical standard granules. The example download request covers the missing
   May–September interval. Metadata discovery/script generation is separate from
   file transfer; it never submits jobs. Existing shared archive files remain
   outside managed assets. NRT uses spires-batch's own acquisition commands.
5. Stage required inputs to a distinct scratch campaign directory, keeping a
   durable inventory of their authoritative sources. Preview sizes; do not rsync
   all of `/pl` or the entire old inversion history.

## Execute in dependency order

### Fresh R0

Complete `r0_request.json`, then use the maintained daily planner and standalone
submission flow described in the main README. The request explicitly binds
summer **2025** to processing WY **2026**; its selection dates are summer dates.
Use a new R0 output path. All summer scenes load into RAM before selection.
The example uses sampled metadata preflight; that is not a complete pixel-content
scan or permission to skip scientific execution checks. Its real 122-scene R0
plan passed preflight. This high-memory job uses one exclusive full-memory
blanca-snow node. Other work uses blanca-snow/blanca-rittger, with approved
preemptable overflow retaining the verified operational exclusion list.

```bash
spires-batch plan r0_request.json --output r0-plan.json
spires-batch stage r0-plan.json
```

Inspect the plan/staging preview and allocate sufficient Blanca RAM before the
documented execute/render/submit commands. Do not start R0 on a login node.
After validation and explicit durable archival, downstream retrieval binds the
new R0 as an existing immutable asset; it must not rebuild it for each date.

The first R0 execution completed on 2026-11-05 UTC: Blanca job `29318329`,
exclusive blanca-snow node `blanca-g4-u34`, exit `0:0`, elapsed `00:06:22`.
Slurm reported batch MaxRSS `152938708K` (about 146 GiB). It staged 9.43 GiB
of source scenes and wrote a 501,228,367-byte, 2400-by-2400, seven-band R0.
The executor reopened the outputs and passed its scientific validation;
an independent metadata check confirmed the completion marker and tile.
These are measurements of this R0 run, not historical inversion benchmarks.
The R0 is still a scratch artifact, not a durably registered training product.
Local plans, logs and the status snapshot are under
`/projects/ropa5718/SPIReS-Organization/archive/operations-onboarding-h08v05-wy2026`.

### Matched cloud, inversion and prepared observations

Use the maintained `spires_batch.pipeline.prepare_date` coordinator for each
day of the complete domain. This same coordinator is used by NRT; it calls
matched cloud preparation, the cloud environment, and `retrieval.retrieve`.
It does not require an existing NRT stream. Run it only in a suitably sized
allocation with explicit science/cloud interpreters and tile/WY retrieval assets.

Each day's source inventory must be selected from real files with
`spires_batch.nrt.source_record`; include every domain tile/date and explicit
nulls only for verified missing observations. `prepare_date` returns receipts
with source identity, `prepared_context`, and the prepared field mappings.
Populate `tiles[tile].daily[date]` in the interpolation request from those
receipts, and retain the same inventory for the eventual stream registration.
Do not pass ordinary daily inversion exports directly as prepared observations.

This stage currently needs an explicit inventory/receipt assembly step; the
universal workflow does not automatically expand native historical retrievals
into all interpolation field bindings. The JSON reference documents the real
interfaces instead of pretending an unsupported all-in-one job exists.

### Historical interpolation and publication

Complete the existing `examples/historical_interpolation.json` with the above
dates, actual grid shape, prepared daily mappings, assets, and donor domain.
The generic example's one-day inventory and grid are placeholders, not the
h08v05 campaign. Do not guess native dimensions or scale a grid to save memory.

```bash
spires-batch interpolation plan interpolation.json --output historical-plan.json
spires-batch interpolation render-slurm historical-plan.json --output launch
spires-batch interpolation status historical-plan.json
```

Review `launch/submit.sh`, allocations and node policy before explicitly running
it. Stage order is screen, spatial, properties, export. Preserve the complete
historical plan, inventory, prepared observations, checkpoint bundle and
publication, not only the exported images. Export completion is required before
the [NRT handoff](nrt-runbook.md).

## Acceptance, not just successful jobs

Record elapsed time, peak RAM, input/staging bytes and disk footprint from a
representative pilot before selecting full-campaign resources. Compare fresh
outputs against older inversions as a diagnostic, not as unquestioned truth:
observation selection, cloud vintage, LUTs and R0 must be comparable. Inspect
cloud edges, tile boundaries, snow onset/melt, canopy adjustments, donor/fallback
source codes, and first/final timing. Document any expected differences.

The fresh R0 is complete, but no historical inversion/interpolation products
or end-to-end validation are supplied yet. Once the remaining campaign runs,
its outputs and recorded observations become the shared training resource;
no separate demo dataset needs to be maintained.
