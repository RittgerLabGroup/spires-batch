# Process historical observations

Use [historical.json](historical.json) for a short interval or full WY without
interpolation. Each date includes matched cloud preparation, spectral fitting,
canopy adjustment, daily albedo and prepared observations for later interpolation.

## 1. Check prerequisites and edit the interval

Complete [initialization](initialization.md) and [R0 construction](r0.md), or
bind equivalent validated existing assets. Check the science/cloud interpreters,
cloud model, LUTs, R0, terrain and tile/WY ancillary bindings. Keep the five-tile
domain for this example.

`start` is inclusive and `stop` exclusive. The example spans October 1,
2025 through May 11, 2026. A full WY2026 uses `stop: "2026-10-01"`, but only
after downloading the remaining reflectance and verifying annual assets.
Missing downloads are not equivalent to genuinely missing observations.
The optional private earthdata-downloads utility can help populate the archive.

### Choose what to do with missing tile/dates

`missing_reflectance` defaults to `"error"`: planning stops and lists the absent
tile/dates so you can diagnose or download the missing files. To explicitly allow
local gaps for this job, set:

```json
"missing_reflectance": "allow"
```

The planner records each absent tile/date, search pattern and reason in
`missing_sources`, with `null` in the source inventory. Inspect the returned
`missing_count` and list before submission. This checks the local archive only;
it does not establish whether NASA has an observation available elsewhere.
An inaccessible/missing archive root, duplicate vintages, or unreadable existing
files remain errors. A source lost after planning is not silently replaced;
a source arriving for a planned gap also requires replanning.

The worker records a missing-observation receipt instead of running cloud
inference or retrieval for that tile. Other tiles on the date still run with
incomplete neighboring context. An entirely missing date is also permitted.
Assembly writes `daily[date]: null` for each allowed gap: interpolation expands
that compact record into an all-unknown tile with NaN scientific fields, **not
zero snow**. No fake reflectance file or large empty raster is created, and no
observed daily-albedo product is claimed for the gap. Interpolated products can
use the available history/donors; filling every pixel is not guaranteed.

Keep the historical plan with its receipts: assembly verifies that every gap
was authorized by that exact plan. NRT registration preserves these inventory
entries as missing, so later acquisition can revisit them. This option controls
historical observation processing; it does not change R0 summer-coverage rules,
ancillary requirements or the full-history replay stop.

Choose fresh output paths. Keep `scheduler_policy.json` beside the request.
The five-tile profile requests five CPUs and 16 GiB per date; pilot your current
runtime because earlier memory measurements predate the daily-albedo sidecar.

## 2. Plan and render

```bash
spires-batch historical plan historical.json --output historical-plan.json
spires-batch historical render-slurm historical-plan.json --output historical-launch
```

These inspect inputs and write scripts; they do not submit. Check date coverage,
per-date tile workers, array concurrency, paths and queue in the saved plan and
scripts. For an initial pilot, shorten a copied request's date interval and use
a separate output directory rather than editing a frozen plan.

## 3. Submit

```bash
module try-load slurm/blanca
bash historical-launch/submit.sh
```

This submits date-array work. Follow the recorded Slurm IDs and logs.
Check completed date receipts, prepared products and daily albedo, not just
scheduler exit status. Do not overwrite completed dates to rerun a failed index.

## 4. Continue if interpolation is wanted

Keep the complete historical output and source inventory. The
[interpolation walkthrough](interpolation.md) assembles its request from those
receipts. Historical processing alone does not publish an interpolated NRT stream.
