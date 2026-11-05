# spipy operations: start here

This guide targets operators familiar with SPIRES_2025_0_1. It describes the
unreleased spipy v1.0.0 candidate, not an approved production deployment.
Use real historical processing as the onboarding exercise, then continue that
same history incrementally. Do not create disposable scientific implementations,
synthetic snow histories, or a second training-only processing pipeline.

## Read in this order

1. This page: package responsibilities and the scientific changes.
2. [Historical campaign](historical.md): h08v05, NOAA-20, WY2026, with fresh R0
   and inversions and the existing shared reflectance archive.
3. [NRT handoff and daily operations](nrt-runbook.md): continue the actual
   historical output, inspect publication, and handle failures.
4. [Configuration reference](../../examples/config_template.json): valid JSON
   documentation, not a runnable request. It covers the universal workflow and
   each request type; scientific constants are not pretend configuration knobs.
5. [Release management](../spipy-release.md): source pins, environment approval,
   outstanding acceptance checks, and updating a candidate without releasing it.

## Repository and data map

| Component | Responsibility | Operator touchpoint |
| --- | --- | --- |
| spires-batch | Plans, submission, staging, historical/NRT orchestration | JSON and CLI |
| spires-contract | Array names, dimensions, units, validation | Read boundary errors; do not bypass |
| spires-io | Native observation matching, reflectance, ancillary loading | Source inventory and asset bindings |
| spires-r0 | All-summer in-memory background selection and reuse | Explicit build or existing R0 |
| spires-inversion | Spectral fit | Approved retrieval recipe/resources |
| spires-postprocess | Sensor-specific canopy correction and radiative products | Correct sensor and policy |
| viirs-cloudmask-batch | Matched cloud inference and neighboring context | Model manifest and cloud interpreter |
| spires-interpolation | Screening, snow state, interpolation, albedo, checkpoints | Prepared inputs, donor domain, native libraries |
| spires-lut | LUT generation | Versioned LUT assets, usually built separately |
| earthdata-downloads | Historical archive acquisition helper | Generate/download only missing reflectance |

`earthdata-downloads` is a separate private utility, not a required runtime
dependency of spires-batch. NRT acquisition already exists in spires-batch.
Do not install the upstream `spires` convenience metapackage to assemble this
candidate. Use the approved coordinated sources and the designated science and
cloud environments. Development uses `spipy14`; the release environment is not
yet locked. Native interpolation libraries are built explicitly at deployment,
not by workers. R0 has no Zarr/chunked execution option.

Keep the shared archive at `/pl/active/rittger_ops/vj109ga.002/input`.
Managed durable assets/products belong under `/pl/active/rittger_ops/spipy`;
fast working copies belong on `/scratch/alpine/ropa5718`. Do not put reflectance
under `spipy/assets`, and do not assume a scratch checkpoint is durably archived.
The root uses `rittger_ops` with an underscore at this site.

## Scientific changes that affect operations

- VIIRS I-band reflectance, M-band reflectance, geometry, and QA must refer to
  the same observation. The selected cloud workflow uses matched native-grid
  observations and neighboring context, not an unrelated 1 km cloud mask.
- Retain raw fit parameters and spectral diagnostics even where corrected
  clouds later reject an observation. A cloud-discarded product cannot supply
  the provenance needed for interpolation or late-data replay.
- The evaluated interpolation recipe is NOAA-20 `viirs_snow`. MODIS retrieval
  and canopy capabilities do not mean MODIS interpolation is validated.
- fSCA uses temporal snow-state/magnitude history. Grain radius and dust use
  distinct direct eligibility, spatial donors, temporal donors, and fallback
  rules. Flat/terrain albedo is calculated from completed properties, not an
  independently interpolated fourth observation series.
- First/final mean zero/seven days of look-ahead, not NRT/standard reflectance.
  A later standard observation can revise previously published final products.
- Missing input is unknown, not observed zero snow. Explicit zero snow resets
  relevant state/barriers; missing dates must remain explicit calendar entries.
- A tile is an output unit, but the configured domain controls cross-tile
  donors and cloud context. Changing that domain changes scientific results.

For exact thresholds, units, source codes, and processing order, read
[`spires-interpolation/docs/algorithms.md`](../../../spires-interpolation/docs/algorithms.md)
in the sibling checkout. The corresponding repository is
`RittgerLabGroup/spires-interpolation`; the sibling link is for local use.

## Configuration and submission boundaries

Workflow schema 2 combines named jobs; a daily request inside it uses schema 1.
Those schema numbers are unrelated to the spipy release version. The universal
renderer uses one allocation per named job with tasks sequential inside it.
Use standalone daily/interpolation renderers for fine-grained arrays and stage
resource tuning. Do not advertise the universal wrapper as equivalent scheduling.

`plan`, `render-slurm`, and status inspection do not submit compute jobs.
Explicitly inspect paths, date coverage, prerequisites, memory, CPU counts,
partition/account and the node-exclusion policy before submission. Default to
Blanca (`blanca-snow` or an authorized `blanca-rittger` allocation). R0 must fit
all summer scenes for one tile/year in RAM. Resource values in templates are
starting points, not benchmarked guarantees.

## Operator acceptance checklist

- Confirm the intended candidate/environment and output generation.
- Preview data staging and confirm input, LUT, model and ancillary identities.
- Verify dates, grid, units, donor domain, and summer-to-processing-WY mapping.
- Run a representative pilot in an allocation before expanding the campaign.
- Check scheduler success **and** scientific completion/publication receipts.
- Inspect fSCA, grain radius, dust, flat/terrain albedo, QA/source codes, and
  variable-specific observation ages for plausible ranges and spatial artifacts.
- Resolve `current.json` once when reading a publication; do not mix generations.
- Preserve logs, submitted config, resolved plan, input inventory, and resources.
- Keep old raw inversions for comparison; do not silently reuse them in the
  fresh-code acceptance campaign.
