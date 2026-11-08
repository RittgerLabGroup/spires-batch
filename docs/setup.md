# Set up your environments

This is an unreleased candidate. Use an approved coordinated source snapshot;
cloning repositories alone does not install the runtime or populate assets.
Science runs in `spipy14`; cloud preparation and inference use a separate
`viirs-cloudmask-batch` environment with no SPIReS dependencies.

## Install or update a candidate

### Fresh candidate installation

The initial science environment recipe is `environments/science.yml`. It is a
provisioning recipe, not a complete third-party lock or scientific acceptance.
Use a new prefix, leaving established operations untouched. The basename
`spipy14` identifies the science environment used for development and validation.
Create environments sequentially to avoid concurrent writes to the package cache.

Use disposable prefixes under `/projects/$USER` even for temporary validation.
On this site, `/tmp` is purged every 15 minutes using file modification dates:
newly extracted Conda files can retain old package timestamps and be deleted
while an installation is running. Keep the package cache outside `/tmp` too.

Before installation, colleagues need access to the private RittgerLabGroup
repositories. Anonymous HTTPS access is insufficient. At this site SSH access
works; verify it with `git ls-remote` before starting. Do not put credentials in
JSON files, requirements URLs, shell history, or the training material.
The commands below assume sibling checkouts of `spires-batch` and
`viirs-cloudmask-batch` under `/projects/$USER/spipy`. Obtain those from
RittgerLabGroup at the agreed candidate revisions; the pinned requirements install
the scientific packages, so a non-editable runtime does not need every source
repository cloned locally.

```bash
module load miniforge
checkout="/projects/$USER/spipy"
science="/projects/$USER/spipy-envs/spipy14"
cloud="/projects/$USER/spipy-envs/viirs-cloudmask-batch"
cd "$checkout/spires-batch"
git ls-remote git@github.com:RittgerLabGroup/spires-interpolation.git HEAD
mamba env create --prefix "$science" --file environments/science.yml

GIT_CONFIG_COUNT=1 \
GIT_CONFIG_KEY_0=url.ssh://git@github.com/.insteadOf \
GIT_CONFIG_VALUE_0=https://github.com/ \
GIT_LFS_SKIP_SMUDGE=1 CC=/usr/bin/gcc CXX=/usr/bin/g++ \
mamba run --prefix "$science" python -m pip install --no-build-isolation \
  -r releases/spipy-v1.0.0/sources-runtime.txt

mamba env create --prefix "$cloud" --file ../viirs-cloudmask-batch/environment.yml
GIT_CONFIG_COUNT=1 \
GIT_CONFIG_KEY_0=url.ssh://git@github.com/.insteadOf \
GIT_CONFIG_VALUE_0=https://github.com/ \
mamba run --prefix "$cloud" python -m pip install \
  -r releases/spipy-v1.0.0/sources-cloudmask.txt
```

The command-scoped URL mapping uses existing SSH authentication without editing
global Git configuration or changing source pins. Sites with authenticated HTTPS
can omit that mapping. Git LFS is included in the science recipe because the
inversion repository uses it; skipping smudge avoids implicitly downloading test
fixtures during runtime installation. Operational LUTs/models remain explicit
external assets. Build isolation is disabled for science so inversion compiles
against the provisioned NumPy, SWIG and NLOpt; normal dependency resolution stays
enabled. Run without source-path overrides such as `PYTHONPATH`.

The LUT-generation package is optional and separately pinned in
`sources-lut.txt`; it is not needed merely to consume an approved LUT. Build the
native interpolation libraries with the newly installed science interpreter:

```bash
native="/projects/$USER/spipy-native"
mamba run --prefix "$science" python -m spires_interpolation.native_build --directory "$native" --kernel snow_fraction --compiler /usr/bin/g++
mamba run --prefix "$science" python -m spires_interpolation.native_build --directory "$native" --kernel spatial --compiler /usr/bin/g++
```

Record the returned library paths in the interpolation configuration. Then perform
the import, source-provenance and model checks below. Cloning and
installing packages does not populate the ancillary catalog or prepare history.

### Existing editable workspace

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
its SWIG/NLOpt extension must be built for that environment.

### Keep cloud processing separate

Cloud preparation and inference both use `cloud_python`. Snow retrieval uses
`science_python`; the science environment does not need the cloud package.
The cloud package owns its observation-matching reader and has no SPIReS package
dependencies. Keep model dependencies in the cloud environment; no science-side
`PYTHONPATH` workaround is needed.

Use the coordinated batch/cloud revisions in the candidate manifest. Verify the
installed source revisions and unified launcher with the commands below before
teaching or submitting a workflow.

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

For a non-editable candidate installation, use the explicit environment prefixes
set above. These checks need only the batch checkout and installed distributions;
they do not require the other component repositories or invoke Git:

```bash
cd "$checkout/spires-batch"
mamba run --prefix "$science" python -m pip check
mamba run --prefix "$science" python -c 'import sys, spires_batch, spires_interpolation; from spires_inversion import _core; print(sys.executable, spires_batch.__file__, spires_interpolation.__file__, _core.__file__, sep="\n")'
mamba run --prefix "$science" python scripts/check_spipy_candidate.py --installed-group runtime
mamba run --prefix "$science" python -m spires_batch.cli workflow submit --help
mamba run --prefix "$cloud" python -m pip check
mamba run --prefix "$cloud" python scripts/check_spipy_candidate.py --installed-group cloudmask
```

The installed check verifies each component's repository and exact Git revision
from pip provenance, rejects editable/local snapshots, and checks that packages
belong to the selected environment. The cloud check also rejects SPIReS packages.
Load the cloud models separately as described above. These checks do not replace
native execution or scientific validation.

Maintainers with all nine sibling repositories can still run
`python scripts/check_spipy_candidate.py` where Git access is permitted. That
mode validates the source snapshot and generated requirements; it does not check
the installed environment and is not an operator installation prerequisite.

Set `variables.science_python` to `"$science/bin/python"` with the actual absolute
path in workflow JSON. Daily/R0 workers and their coordinators use the explicit
`execution.resources.science_python`, inherited from the workflow interpreter
when omitted. The standalone R0 example includes this field too. Inspect the
rendered commands to confirm the path; the legacy `environment_name` fallback
is used only when no explicit interpreter is configured. Interpolation arrays
capture the interpreter used to render them.

The new unified orchestration still requires a deployed end-to-end pilot.
Consult the dated validation reports for the checks already performed. Model loading is not full-grid
inference validation; the small inversion checks are not the VIIRS campaign.
See [per-user paths](paths-and-assets.md) before adapting any example.
