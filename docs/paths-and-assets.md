# Per-user paths and shared scientific assets

Processing paths are explicit JSON fields. In the unified example,
`variables.scratch`, `variables.science_python` and `variables.cloud_python`
replace the corresponding paths throughout the embedded requests. Replace
remaining `REPLACE_USER` values, particularly durable domain/archive names.
Changing a variable does not create assets or install an environment.

Standalone task JSONs contain literal paths. Initialization's scratch root does
not rewrite other configurations. Keep R0 output paths and downstream
baseline/coordinate bindings consistent.

## What changes between colleagues

The keys below are relative to the named request, even when that request is
embedded under a universal workflow's `jobs.<name>.request`.

| Purpose | Where configured | Per-user rule |
| --- | --- | --- |
| Checkout location | Clone destination and environment installation/source paths | Own sibling checkouts under `/projects/<user>/spipy`; do not edit another operator's editable install |
| Science/cloud interpreters | Cycle `science_python`, `cloud_python`; workflow `scheduler.python` | Point to the user's provisioned environments, or an explicitly approved shared runtime |
| Daily job environment | `execution.resources.environment_name` or `execution.profiles.<name>.resources.environment_name` | The daily renderer uses `mamba run -n`; the environment must be discoverable in that user's job session |
| Interpolation job interpreter | Interpreter running `interpolation render-slurm` | Captured into the script, not a cycle-interpreter override; regenerate scripts under the intended environment |
| Initial scratch layout | Initialization `storage.scratch_root` | Use `/scratch/alpine/<user>/spipy` explicitly; the current default contains `ropa5718` |
| Initial durable layout | Initialization `storage.durable_root`, `domain` | Shared assets may use the common root; independent training registrations need distinct domains, such as `five-tiles-wy2026-<user>` |
| Daily outputs and caches | `output.root`, `execution.staging.root` | Unique per user/run; these are separate from initialization's scratch root |
| Explicit R0 destination | `r0.artifacts[].path` in the campaign R0 request | Update together with the output root; changing the root alone does not relocate an explicitly named artifact |
| Workflow/interpolation output | Top-level `output` in each request | Unique per user/run; avoid copying another person's publication paths |
| Unified workflow logs | Top-level `log_root` | Writable per-user base; each workflow/cycle gets its own subdirectory. Defaults to `output/logs`; see [Logs and retries](logging-and-retries.md) |
| Working NRT state | Cycle `stream_root`; matching CLI `--root` | Separate scratch stream per independent exercise; never point multiple students at the operational stream |
| Durable NRT archive | Storage JSON `archive_root` | Separate durable archive per exercise; configure before first bootstrap/publication |
| Cycle work, logs and plans | CLI `--output`, render destination, submission state/event directory arguments | Unique run directories; not all are JSON fields |
| Native binaries | Interpolation `snow_library`, `spatial_library` | Own compatible builds or approved shared binaries; keep their build receipts |
| Scheduler authorization | Daily resource profiles; workflow/interpolation `scheduler` and job/stage overrides | Set authorized account/partition and policy path; keep the verified exclusions for preemptable overflow |
| Historical download output | Downloader `script_root`; `archive_root` is the shared input archive | Separate generated scripts per user; shared archive writes require authorization |

Use actual absolute paths in the JSON. Shell examples expand `$USER`, but the
standalone JSON loaders do not generally expand `$USER` or `${SCRATCH}`.
The unified workflow substitutes only variables explicitly declared in its
`variables` object; it does not read shell environment variables. Relative paths and
`~` are handled differently across entry points; do not assume shell expansion
or that all paths are resolved relative to the config file. There is no general
`${storage.scratch_root}` reference/interpolation mechanism.

## What normally stays shared

| Asset | Binding |
| --- | --- |
| Standard reflectance | Initialization `storage.reflectance_roots`; daily `inputs.roots`/`inputs.files`; native inventory records |
| Retrieval LUT | Cycle `tiles.<tile>.<WY>.lut`; daily named LUT input |
| Albedo LUT | Interpolation `albedo_lut`; daily named albedo LUT input when applicable |
| Cloud model | Cycle `cloud_model` plus the manifest's stage-1/stage-2 file paths |
| R0 and axes | Cycle `tiles.<tile>.<WY>.background.baseline` and `.axes.x/.y`; daily existing-background binding |
| Retrieval ancillary | Cycle `tiles.<tile>.<WY>.ancillary`: canopy fraction, slope, aspect and ice fraction |
| Interpolation context | Interpolation `tiles.<tile>.terrain`, `.agriculture`, `.coastal`, `.frequency` and `.bright_soil`; cycle `annual_bright_soil` for later WYs |
| Prepared observations | Interpolation `tiles.<tile>.daily.<date>.<field>` from actual retrieval receipts |

A mapped field includes its file `path` and dataset name, sometimes `index`,
`band`, `axis` or `bitwise_and`. Relocating the same file changes the path, not
the scientific dataset/units/grid definition. Do not change those selectors
merely because the operator changes. Likewise do not alter the sensor, tile
domain, thresholds, model identity or annual scientific context for a username.

Read-only shared assets need not be copied once per user. Use scratch staging
only for the required files and bind the resulting locations explicitly. The
reflectance archive stays at `/pl/active/rittger_ops/vj109ga.002/input`, outside
the managed `assets` tree. Production asset population and access permissions
still need verification; the example destination paths do not assert the files
already exist.

For independent training, reuse immutable approved inputs, but create new output
and state destinations. Do not search-and-replace usernames inside an existing
resolved plan, checkpoint, publication or provenance receipt: make a new request
and plan. Archive relocation after publication is a verified migration, not a
routine per-user setting change. The cloud policy currently identifies manifest
bytes, so even rewriting paths inside that manifest requires coordinated policy
handling; use the same verified manifest where possible.

## Scratch working state and durable archive

For example, user `colleague` can choose:

```text
scratch work:   /scratch/alpine/colleague/spipy/work/noaa20/five-tiles/WY2026/training-01
working state: /scratch/alpine/colleague/spipy/streams/noaa20/five-tiles-wy2026-training-01
archive:       /pl/active/rittger_ops/spipy/training/colleague/five-tiles-wy2026-training-01/archive
```

The durable training parent needs group-approved write permissions. Set the
working-state path as `cycle.json`'s `stream_root`, and the durable archive path
as `storage.json`'s `archive_root`. Apply storage configuration before bootstrap.
Otherwise the default archive is `STREAM_ROOT/archive`, which would leave both
working state and archive on scratch. The configured durable archive supports
recovery; it does not make deletion of scratch state harmless or authorize a
silent full-history restore. The full-replay stop remains in force.

The five-tile examples illustrate this split. Initialization reserves both
durable and scratch directories but does not configure the NRT storage policy
or create a published stream. See [NRT handoff](nrt-operations.md).

Credentials, group permissions, compiler/toolchain setup and package installation
are outside JSON. Cloud preparation and inference both use `cloud_python`; no
cloud-package import or `PYTHONPATH` workaround is needed in science. Never store credentials in a
config or copy another user's authentication files. See [environment setup](setup.md).

The new `historical.json` embeds the preparation recipe under `preparation`.
Change its interpreters, cached asset paths, `output`, and scheduler account/QoS;
keep the corresponding scientific bindings in `cycle.json` consistent. Terrain
and `albedo_lut` are explicit historical fields. `reflectance_root` may remain the
shared archive. `start`/`stop` control a pilot versus a whole WY. The interpolation
`daily` mappings are assembled from receipts, not edited by replacing usernames.
