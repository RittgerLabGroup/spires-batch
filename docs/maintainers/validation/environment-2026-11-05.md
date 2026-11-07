# Environment validation — November 5, 2026

Status on 2026-11-05: clean, non-editable candidate installations pass bounded
runtime validation. Science and cloud use separate environments, and cloud has
zero SPIReS packages or imports. This is not full scientific acceptance or
approval to replace SPIRES_2025_0_1 operations. Use `spipy14` for development;
cloud preparation and inference run in their separate CPU environment.

## Clean-install validation

The isolated installations are under
`/projects/ropa5718/SPIReS-Organization/archive/clean-install-20261105/envs/`:
`spipy14` for science and `viirs-cloudmask-batch` for cloud. Both were provisioned
from environment recipes, then installed from the coordinated source requirement
files, not sibling editable checkouts. Existing environments were not replaced.

- Both `pip check` runs pass. Installed science component commits match the
  draft manifest; imports resolve inside the new environment's `site-packages`.
- The inversion extension builds and imports; all 13 existing contract-boundary
  checks pass using the clean interpreter, without importing the local checkout.
- Both interpolation libraries build from installed package sources. The snow
  library loads and a bounded native spatial calculation passes.
- The selected cloud models load with SPIReS imports explicitly forbidden and
  no SPIReS distribution installed in the cloud environment.

Receipts `science-validation.json`, `cloud-validation.json`, the pip installation
reports, resolved Conda package exports, native build receipts and the inversion
check log are in that same `clean-install-20261105` directory. Resolved exports
are validation evidence, not an approved release lock. Pytest was installed only
as a validation tool. Full-grid inference/retrieval and historical-to-NRT checks
remain separate acceptance gates.

## Earlier development-environment checks

| Check | Result |
| --- | --- |
| Candidate source manifest | Use the coordinated cloud/batch revisions recorded in the draft manifest; rerun the candidate checker after updating pins |
| Science interpreter | Python 3.14.4 in `/projects/ropa5718/software/anaconda/envs/spipy14` |
| Cloud interpreter | Python 3.11.15 in `/projects/ropa5718/software/anaconda/envs/viirs-cloudmask-batch` |
| Installed dependency consistency | `pip check` passed in both environments without the unnecessary science-side cloud source-path workaround |
| Interpolation installation | Initially missing; installed the sibling checkout with `io,netcdf` extras, without changing third-party packages |
| Inversion native extension | Imports successfully and resolves its NLOpt/shared-library dependencies |
| Existing inversion contract checks | 13 passed, including a small native inversion; run from the inversion checkout because fixtures use relative paths |
| Interpolation native libraries | Both built with `/usr/bin/g++` 8.5.0 and loaded; a bounded spatial call returned the expected constant donor value |
| Cloud-model artifacts | Both manifest checksums verified; both models loaded successfully on CPU |
| Standalone matched preparation | Cloud-owned reader matches all 13 prepared arrays exactly for real standard and NRT scenes, with all SPIReS imports forbidden in the cloud subprocess |

The cloud runtime has torch 2.10.0, scikit-learn 1.8.0, numpy 2.4.6,
h5py 3.16.0, joblib 1.5.3 and pandas 3.0.3. The science runtime has numpy 2.4.5,
scipy 1.17.1, h5py 3.16.0, netCDF4 1.7.4 and pydantic 2.13.4. These observations
are not a complete dependency specification. Installed editable package version
strings can lag source changes; verify the imported source locations and Git
revisions rather than treating `pip check` as a source-pin check.

The local native build receipts are under
`/projects/ropa5718/SPIReS-Organization/archive/environment-validation-20261105/native`.
They are validation artifacts, not a portable binary distribution. No production
runtime, cron configuration, model artifact, source pin or release was changed.
The reader comparison ran as Blanca job `29327825` in 60 seconds with 24 GiB
requested and batch MaxRSS `1638152K`; its log is beside the native directory.
Both 2400-by-2400 scenes exercised compact observations (244,403 matched extra
pixels in the standard scene; 278,573 in the NRT scene). Temporary prepared
files were removed after comparison. This is reader equivalence, not a new
full-grid model-inference benchmark.
