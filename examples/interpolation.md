# Interpolate completed history

Use [interpolation.json](interpolation.json) after [historical processing](historical.md).
The unified workflow performs receipt assembly and submission automatically;
these commands are for an independent interpolation run.

## 1. Match the historical coverage

Set the dates, tile domain, native-library paths, ancillary fields, albedo LUT and
output destination. Keep `scheduler_policy.json` beside the request.
The example explicitly sets `use_snow_climatology: true`. Set it to `false` to
hold the onset frequency weight at one and remove the rare-snow veto while
retaining playa/bright-soil and agricultural thresholds. Disabled runs need no
`frequency` or `coastal` fields; existing bindings are ignored and omitted from
the effective plan. In a unified workflow, this option belongs in the
interpolation job's `request`. Also omit climatology assets from initialization
if those files will be unavailable. Use a separate output root and replay from
`history_start` when changing modes; the other mode's checkpoints cannot be reused.
The example ends May 11, 2026 and excludes May 12–18 even from historical
look-ahead; those dates are reserved for NRT continuation.

The example's empty `daily` mappings are filled from real prepared receipts.
Do not invent observation paths or substitute ordinary exported inversion maps.
If historical processing explicitly allowed missing reflectance, assembly retains
those tile/dates as `null` records. They represent unknown observations, not
snow-free tiles, and do not supply spatial or temporal observation donors.

## 2. Assemble and plan

Replace `HISTORICAL_OUTPUT` with the historical configuration's output directory:

```bash
spires-batch historical assemble interpolation.json --receipts HISTORICAL_OUTPUT --output bound
spires-batch interpolation plan bound/interpolation.json --output interpolation-plan.json
spires-batch interpolation render-slurm interpolation-plan.json --output interpolation-launch
```

These write the bound request, inventory, plan and scripts without submitting.
Retain both files in `bound/`. Review date coverage, donor domain and per-stage
resources. The rendering command captures the science interpreter being used.

## 3. Submit and inspect

```bash
module try-load slurm/blanca
bash interpolation-launch/submit.sh
spires-batch interpolation status interpolation-plan.json
```

The script submits screening, spatial, properties and export stages with
dependencies. Status inspection is not full Slurm accounting or scientific QA.
Verify every stage, the exported generation and checkpoints. Inspect fSCA,
grain radius, dust, albedo, QA and observation ages.

For this historical interval, first/final products can reach May 11/May 4.
All date bounds are inclusive; the example uses `schema_version: 1` and
`stop: "2026-05-11"`. For a full WY with seven context days, set `stop` to
September 30 and `available_through` to October 7 after preparing those dates.
The example sets `output_issues` to `all_issues`, so the six intermediate files
end on May 10 through May 5. Set `output_issues` to `final_only` to export just
`final.nc`, with all variables through May 4 and the same restart checkpoints.
Set it to `first_and_final_only` to export only the
two endpoints. Every written file contains all output variables, and donor
eligibility remains based on the original screened observations.
Keep all history required for registration, not just exported maps.

## 4. Continue into NRT

[Configure storage and register history](storage.md), then
[run an NRT update](cycle.md). See the
[interpolation reference](../docs/reference/interpolation.md) for format,
scientific-context and restart rules.
