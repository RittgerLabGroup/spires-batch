# Environment preparation and validation

Status on 2026-11-05: bounded runtime checks pass, and the local standalone-cloud
change removes the science-side cloud-preparation dependency. This is not a
clean-install validation, a third-party environment lock, or approval to replace
SPIRES_2025_0_1 operations. Use `spipy14` for development; cloud preparation and
inference run in their separate CPU environment.

## Findings from the current installation

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

## Complete an existing development setup

Do not run these against the established operational environment. The commands
assume sibling source checkouts, an already provisioned science environment, and
the third-party prerequisites described in the component packages. They are not
a substitute for creating and validating a clean environment.

Activate your own `spipy14`, then set your checkout and native build locations:

```bash
checkout="/projects/$USER/spipy"
native="/projects/$USER/spipy-native"
cd "$checkout/spires-interpolation"
python -m pip install --no-build-isolation -e '.[io,netcdf]'
python -m spires_interpolation.native_build --directory "$native" --kernel snow_fraction --compiler /usr/bin/g++
python -m spires_interpolation.native_build --directory "$native" --kernel spatial --compiler /usr/bin/g++
```

Record the actual returned library filenames as `snow_library` and
`spatial_library` in interpolation JSON. The spatial library requires OpenMP.
Do not copy the Python 3.14 inversion extension into another Python environment:
its SWIG/NLOpt extension must be built for that environment. The existing batch
bootstrap covers the older six-package editable stack, not interpolation or the
separate cloud environment, so its success message alone is insufficient.

### Cloud preparation stays in the cloud runtime

The original coordinator tried to run matched preparation under `science_python`,
which required exposing cloud code in science and caused a missing-PyTorch
metadata conflict. That arrangement is superseded: `pipeline.prepare_date` now
uses `cloud_python` for both preparation and inference. `science_python` handles
the subsequent snow retrieval and does not need the cloud package installed.

The cloud package owns its VIIRS observation-matching reader. It deliberately
duplicates the matching/scaling/coordinate rules instead of importing SPIReS
packages. Keep the normal complete cloud dependency set in its own environment;
neither a preparation-only extra nor a science-side `PYTHONPATH` workaround is
needed. The earlier workaround was diagnostic only and was not persisted.

Use the draft source manifest that includes both the standalone cloud reader and
the batch coordinator change. Older snapshots still select the science-side
preparation implementation. Keep these revisions coordinated when updating
source pins. This remains an unreleased candidate; source-pin consistency does
not establish a reproducible third-party environment or scientific acceptance.

### Separate cloud environment

The cloud repository supplies `environment.yml`. Run installation through the
explicit target interpreter or `mamba run --prefix`, not an unrelated currently
active `python`. For an already created environment matching that file:

```bash
cloud_python="/projects/$USER/software/anaconda/envs/viirs-cloudmask-batch/bin/python"
"$cloud_python" -m pip install -e "$checkout/viirs-cloudmask-batch"
"$cloud_python" -m pip check
"$cloud_python" -c 'import torch; from viirs_cloudmask_batch.matched import MatchedPredictor; print(torch.__version__)'
```

`cycle.json` must name this interpreter separately from `science_python`. Check
access to the actual model manifest and both model files using that interpreter.
Do not change the model merely to accommodate a different environment.

## Verification before submission

From the activated science environment, the following checks must all pass for
an accepted setup; cloud imports are checked only with `cloud_python` above:

```bash
python -m pip check
python -c 'import sys, spires_batch, spires_interpolation; from spires_inversion import _core; print(sys.executable, spires_batch.__file__, spires_interpolation.__file__, _core.__file__, sep="\n")'
cd "$checkout/spires-batch"
python scripts/check_spipy_candidate.py
```

Run the source checker where Git access is permitted. It verifies the recorded
snapshot, not a clean installation or that every current checkout equals its pin.
Inspect generated Slurm scripts before submission: the daily renderer uses a
mamba environment name, the universal renderer can use `scheduler.python`, and
the interpolation renderer captures the interpreter used to render it.

Remaining validation includes a clean provision/install,
full-grid matched cloud/retrieval, scientific
asset bindings, and historical-to-NRT acceptance. Model loading is not full-grid
inference validation; the small inversion checks are not the VIIRS campaign.
See [per-user paths](user-paths.md) before adapting any example.
