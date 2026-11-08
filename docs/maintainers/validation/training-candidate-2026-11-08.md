# Training candidate installation — November 8, 2026

The published draft candidate passes fresh science and cloud installation and
bounded runtime validation. Full-grid acceptance of the eight-issue workflow
remains outstanding. The canonical training scenario is still the five NOAA20
tiles in WY2026; no additional pilot or training scenario was introduced.

## Published source revisions

- `spires-interpolation`: `2cca6e16d0f0c64db884557cc93647063ecaef90`.
- `spires-batch` executable code: `9bfdf85b3a2a1aa7bcc725bcd262f91e2f912474`.
- Candidate manifest/source requirements: batch commit
  `04e4d0ac3c74c43fe319ad40e69620e5e8969ac2`.

Later documentation and validation-metadata commits retain these executable
source pins. The public configuration schema remains draft version 1. There is
no release tag or approved environment lock.

## Fresh installation and checks

The source requirements came from a fresh GitHub clone of the published batch
repository. Science and cloud were installed non-editably into separate new
Conda prefixes using the documented recipes and exact source commits. Installed
provenance checks passed; imports resolve inside the new environments.

- Dependency consistency passes in both environments.
- The inversion extension builds and imports. All 13 existing contract-boundary
  checks pass against the installed package, including native inversion.
- Both interpolation native libraries build from the installed sources.
- On a synthetic 4-by-5 grid, historical processing produces all eight issues
  or only first/final according to configuration. All output variables and masks
  match the earlier bounded validation products. NRT bootstrap and append pass
  for both modes, extending the input window from 12 to 16 days. Output lengths
  follow issue age, and date bounds are inclusive.
- Both selected cloud models load on CPU with SPIReS imports forbidden and
  no SPIReS packages installed in the cloud environment.

Local evidence and disposable environments are retained under
`/projects/ropa5718/SPIReS-Organization/archive/training-candidate-install-20261108T230911Z/`.
Evidence includes `science-validation.json`, `cloud-validation.json`,
`inversion-checks.log`, pip install reports, resolved package exports and native
build receipts. The environment prefixes are `envs/spipy14` and
`envs/viirs-cloudmask-batch`. These exports document this installation; they are
not approved release locks. Pytest was added only for validation.

The first installation attempt used `/tmp`. The site's 15-minute cleanup
deleted newly extracted Conda files that retained old package modification
dates. Both environments were rebuilt in the retained directory above and all
reported checks were rerun there. Setup instructions now explain this constraint.
Established operational environments were not replaced.

## Remaining five-tile acceptance

The older first/final-only historical interpolation completed all 1,428 tasks.
Its ten full-grid NetCDFs provide a comparison baseline: first issue has 223
dates through May 11, 2026; final issue has 216 dates through May 4. All five
summer-2025 R0 files and the 230 daily prepared receipts through May 18 remain
available. Reflectance coverage also extends through October 7, 2026.

The remaining acceptance work uses the existing five-tile example:

1. Configure an isolated output directory and stream using this candidate.
   Confirm reusable input provenance and profile representative tasks with the
   eight-issue output before the larger submission.
2. Reuse the R0 and prepared daily data and rerun historical interpolation for
   October 1, 2025 through May 11, 2026. With 20-row stripes this comprises 600
   screening tasks, 223 spatial tasks, 600 property tasks and five exports.
   The old two-issue checkpoints do not validate the new eight-issue state.
3. Validate all 40 NetCDFs, including variables, dates, masks, grids and tile
   boundaries. Compare first/final against the existing baseline and record
   resource and storage requirements.
4. Register the new historical products and run the held-out May 12–18 NRT
   continuation. A complete cycle should exercise acquisition/preparation,
   retrieval, daily albedo and revision for those 35 tile-days. Replaying prepared
   inputs alone would cover less of the operational workflow. After continuation,
   first issue ends May 18 and final issue ends May 11.
5. Check restart/no-op behavior, retries and the full-history transfer guard;
   review scientific outputs and record acceptance.

These full-grid jobs have not been submitted for this validation. Recreating R0
or the entire historical retrieval campaign is unnecessary for this acceptance
step. Processing the rest of WY2026 is a separate production exercise.
