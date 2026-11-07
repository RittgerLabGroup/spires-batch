# How spipy fits together

Operators normally interact with **spires-batch**: edit a JSON configuration,
review the plan, and submit it. Batch calls the scientific packages and the
standalone cloud-mask package. You do not manually run every repository.

## Repositories

| Repository | Responsibility |
| --- | --- |
| spires-batch | Configuration, input checks, staging, Slurm submission and automatic handoffs |
| spires-contract | Shared array names, dimensions, units and validation rules |
| spires-io | Read reflectance and ancillary data; match VIIRS observations |
| spires-r0 | Build reusable background reflectance (R0) |
| spires-inversion | Fit the observed reflectance to estimate snow properties |
| spires-postprocess | Apply canopy corrections and calculate radiative products |
| viirs-cloudmask-batch | Prepare matched VIIRS observations and predict cloud masks; no SPIReS package dependencies |
| spires-interpolation | Screen observations, fill gaps, calculate albedo and save history for future updates |
| spires-lut | Generate LUT assets, usually separately from operational processing |
| earthdata-downloads | Optional private utility for filling historical reflectance archives |

These repositories are maintained under RittgerLabGroup. Use a coordinated
source snapshot, not arbitrary combinations of checkouts. Science and cloud
run in separate environments; see [setup](setup.md).

## Processing sequence

1. **Initialize:** create directories and register/stage externally produced assets.
   This does not generate missing LUTs, maps or reflectance.
2. **Build or reuse R0:** the summer recipe uses June 1–September 30 before the
   processing water year. Reuse a suitable validated R0 rather than rebuilding daily.
3. **Process observations:** prepare matched reflectance and clouds, run the spectral
   fit, apply canopy adjustments and calculate daily albedo.
4. **Interpolate:** use the completed observations and their history to produce
   fSCA, grain radius and dust; calculate flat and terrain albedo from the completed
   properties. Albedo is not a separate fourth gap-filling algorithm.
5. **Register history:** archive the completed products and checkpoints so NRT
   updates can continue from actual historical results.
6. **Continue NRT:** acquire new/replacement observations, revise affected dates,
   and publish a verified generation.

The [unified example](../examples/workflow.md) connects these steps automatically,
while retaining date/stage arrays. The [individual examples](../examples/README.md)
are useful when only one task is needed.

## Scientific concepts operators need

- The current interpolation recipe is NOAA-20 VIIRS `viirs_snow`. MODIS retrieval
  support does not imply that MODIS interpolation has been validated.
- A tile is an output unit; the full domain supplies neighboring cloud context and
  interpolation donors. Dropping tiles can change the results.
- Missing observations are unknown, not zero snow.
- First and final products use zero and seven days of look-ahead respectively.
  A later standard observation can revise previously published final products.
- Raw retrieval diagnostics and prepared observations are needed for interpolation
  and later revisions; exported maps alone are not sufficient restart history.

For units, QA codes and algorithm details, consult the
[interpolation algorithm guide](https://github.com/RittgerLabGroup/spires-interpolation/blob/main/docs/algorithms.md).

## Storage and resources

Shared reflectance remains under `/pl/active/rittger_ops/vj109ga.002/input`,
outside spipy's assets directory. Durable assets/state live under
`/pl/active/rittger_ops/spipy`; scratch provides faster working I/O.
Each independent exercise needs its own output, stream and archive paths.

Use Slurm/Blanca. R0 needs all summer scenes for a tile/year in RAM and uses an
exclusive full-memory blanca-snow allocation. Other tasks use their measured or
provisional [resource profiles](../examples/scheduler_policy.md).
Unexpected full-history NRT recovery stops with a transfer-size report rather
than silently copying the entire history.

Successful automation does not establish scientific acceptance. Inspect the
maps, QA, tile boundaries, date coverage and first/final cutoffs before deployment.
