# spires-batch command guide

## Name

`spires-batch` — configure, plan and run spipy processing.

## Synopsis

```text
spires-batch COMMAND [SUBCOMMAND] [ARGUMENTS] [OPTIONS]
spires-batch --help
spires-batch historical --help
spires-batch historical plan --help
```

Uppercase words below are placeholders to replace. Brackets indicate optional
arguments; do not type the brackets. Dates use `YYYY-MM-DD`. Append `--help` to
any command for its complete option list. This guide groups commands by task;
it is not a script to execute from top to bottom.

## Description

Use JSON to choose tiles, dates, inputs, destinations and resources. Use the CLI
to check those choices, prepare jobs and explicitly start processing. Start with
the [examples overview](../../examples/README.md) and the
[complete workflow](../../examples/workflow.md).

The recommended path is **configure → review → workflow submit → monitor**.
Verified stages automatically trigger the next submissions. Standalone historical
processing instead follows **plan → render-slurm → review → submit → inspect results**. A plan is a saved description of the work and its inputs.
Rendering writes Slurm scripts; it does not submit them. For historical and
interpolation arrays, submit the generated `submit.sh` only after review.
R0 has a separate managed submission command, shown below.

Use `slurm/blanca`, normally `blanca-snow` or `blanca-rittger`. Review account,
QoS, exclusions and resources before submission. Scientific execution commands
run in the current allocation: they do not acquire compute resources for you.
Do not run them on a login node. NRT is especially important: **`nrt cycle`
downloads and processes observations even without `--execute`**.

## Submit a complete workflow

```text
spires-batch workflow submit CONFIG --output PLAN
spires-batch workflow status PLAN
spires-batch workflow cycle CONFIG --job NAME --through DATE --output NEW_PLAN
```

`submit` validates, saves the plan and submits the chain. R0, historical and
interpolation jobs retain their array scheduling. After verified completion,
the controller submits the next work automatically. `cycle` selects only an NRT
job against an already registered stream; it does not rebuild historical inputs.
Both submission commands contact Slurm. `status` reads durable progress only.
It includes `log_directory`; open its `summary.txt` for the human-readable view.
The JSON fields `log_root` and `max_auto_retry_count` control
[logging and bounded automatic retries](../logging-and-retries.md).
See the [unified JSON walkthrough](../../examples/workflow.md).

For separate review before submission:

```text
spires-batch workflow plan CONFIG --output PLAN
spires-batch workflow render-slurm PLAN --output DIRECTORY
spires-batch workflow start PLAN
```

The first two commands do not submit. Inspect the preview, then use either
`start` or the generated `submit.sh`, not both and not another `submit` call.
Scientific input checks run after prerequisites exist; initial planning is not
a promise that all future inputs already exist.

## Inspect resource defaults

```text
spires-batch scheduler-policy POLICY [--job-type TYPE] [--queue QUEUE]
```

Print resolved resource/queue defaults and their measurement notes. This performs
no submission, transfer or science. See the [shared resource policy](../../examples/scheduler_policy.md).

## Initialize a tile

```text
spires-batch tile-init CONFIG --dry-run
spires-batch tile-init CONFIG [--stage-assets]
```

Preview paths, missing assets and transfer sizes with `--dry-run`. Without it,
create directories and register existing assets. Copy registered assets to
scratch only when `stage_assets` is enabled in the configuration or
`--stage-assets` is supplied. This does not download reflectance, generate
ancillary maps or submit jobs. See [tile initialization](initialization.md).

## Build R0

```bash
spires-batch validate r0.json
spires-batch plan r0.json --output r0-plan.json
spires-batch dry-run r0-plan.json
spires-batch stage r0-plan.json
```

These check a `batch_request` configuration and preview the work/staging. They
do not submit jobs. `stage --execute` explicitly performs configured copying.
Once the plan and resources are approved, the following **submits jobs**:

```text
spires-batch submission start-operational r0-plan.json --state-root STATE_DIRECTORY --output-dir LAUNCH_DIRECTORY
```

This manages stage order, reconciliation and capped retries. Use an exclusive
full-memory `blanca-snow` node for R0 construction. Existing suitable R0 can be
reused instead of running this step. See the [historical guide](../../examples/historical.md)
for checking and registering a newly built R0.

The root `validate`, `plan` and `render-slurm` commands accept the batch-request
interface; they are **not** generic commands for every configuration type.
Use `historical plan` or `interpolation plan` for those configurations.

## Process daily observations over a date range

```text
spires-batch historical plan CONFIG --output PLAN
spires-batch historical render-slurm PLAN --output DIRECTORY
```

`plan` inventories existing historical reflectance and assets. It does not
download missing files or build R0. Missing reflectance stops planning by default;
`missing_reflectance: "allow"` records explicit unknown tile/dates instead. The
plan and returned summary list each gap. Existing corrupt files, duplicate
vintages and sources lost after planning still fail. See the
[historical walkthrough](../../examples/historical.md).
`render-slurm` writes date-array scripts.
After review, `bash DIRECTORY/submit.sh` **submits the array**. Each task processes
one date, with concurrent tile workers as configured. Daily processing includes
cloud masking, spectral unmixing, canopy adjustment and snow albedo, not gap filling.

The worker command below is normally invoked by generated scripts inside an
allocation, rather than manually:

```text
spires-batch historical run PLAN --index N
```

Wait for successful jobs and inspect products before interpolation. Start with
a small pilot; resource settings must be validated for your run. Existing completed
dates are not overwritten. Retry failed indices only, or use a new output directory
for an intentional rerun.

## Interpolate completed observations

```text
spires-batch historical assemble TEMPLATE --receipts HISTORICAL_OUTPUT --output BOUND_DIRECTORY
spires-batch interpolation plan BOUND_DIRECTORY/interpolation.json --output PLAN
spires-batch interpolation render-slurm PLAN --output DIRECTORY
spires-batch interpolation status PLAN
```

`assemble` connects completed daily output records to the interpolation template.
It writes `interpolation.json` and `inventory.json`; retain both for the NRT
handoff. Planning validates inputs; rendering writes dependent processing stages.
After review, `bash DIRECTORY/submit.sh` **submits those stages**. Ensure the
Blanca cluster/QoS are set appropriately. The selected policy queue supplies both,
and the renderer includes them in the Slurm scripts.

`status` reports screen/property task completion. It is not a substitute for
checking all jobs, exported products and scientific quality. Worker syntax:

```text
spires-batch interpolation execute-task PLAN STAGE INDEX
```

`STAGE` is `screen`, `spatial`, `properties` or `export`. Normally the generated
scripts call this command. See [interpolation](interpolation.md).

## Register history and run NRT updates

After completing and checking historical interpolation, configure durable storage
and register that real history once:

```text
spires-batch nrt storage-config STORAGE_CONFIG --root STREAM_ROOT
spires-batch nrt bootstrap HISTORICAL_PLAN INVENTORY --root STREAM_ROOT
spires-batch nrt status --root STREAM_ROOT
spires-batch nrt storage-report --root STREAM_ROOT
```

`bootstrap` registers and archives existing results; it does not generate a
synthetic history. Initial archival can copy substantial data. Review storage
paths before running it. `STREAM_ROOT` is the working history directory used
by your NRT configuration, not the shared reflectance directory.

Inside a suitable Blanca allocation, run an update:

```text
spires-batch nrt cycle CONFIG --through DATE --output DIRECTORY [--execute]
```

**Without `--execute`, this still downloads, prepares clouds and runs retrievals**,
then stops at the interpolation update plan. With `--execute`, it also completes
the interpolation update and publication. It neither submits a Slurm job nor
creates a cron schedule. Follow the [NRT runbook](../nrt-operations.md) for
the complete handoff and output checks.

Unexpected full-history replay stops with exit status **3** and a durable report
of the required transfer size. It does not wait for interactive approval, so
Slurm/cron operators should inspect the failure report before deciding what to do.

### Individual NRT steps

These are useful for inspection or controlled recovery, not additional steps
to repeat after a successful `cycle`:

| Command (after `spires-batch nrt`) | Purpose and effects |
| --- | --- |
| `acquire --root ROOT --through DATE --output DIRECTORY [--download]` | Query available source files; `--download` also transfers them. |
| `prepare REQUEST --output HDF --receipt JSON` | Compute prepared observations and write a record of their inputs. |
| `retrieve REQUEST --output HDF --receipt JSON` | Run spectral retrieval/preparation and write its record. |
| `plan CANDIDATES --root ROOT --through DATE --output DIRECTORY` | Plan revisions; may restore/copy required saved history, unlike a metadata-only historical plan. |
| `run UPDATE` | Execute a saved update and publish its results in the current allocation. |
| `finish UPDATE` | Finalize publication after separately completed processing; not a shortcut around execution. |
| `catalog --root ROOT --output FILE` | Export the observation catalog; supports inclusive `--start`/`--stop` and `--publication`. |
| `retention-plan --root ROOT --output REPORT` | Write proposed cleanup actions for review. |
| `retention-apply REPORT` | Apply reviewed cleanup; can delete eligible files. |
| `migrate --root ROOT [--seed-plan PLAN]` | Update storage/catalog organization; see storage documentation first. |
| `rebuild --root ROOT --output DIRECTORY [--checkpoint CHECKPOINT]` | Prepare a recovery/rebuild plan. |
| `abandon --root ROOT DIRECTORY` | Abandon a pending update; use only after diagnosing it. |

`nrt plan` also accepts `--annual-bright-soil FILE` and `--seed-plan PLAN`.
See [NRT details](nrt.md) and [storage management](storage.md) before recovery
or cleanup operations.

## Advanced and internal commands

These interfaces support more customized operations. They are not prerequisites
for the complete workflow or individual-task walkthroughs.

| Command (after `spires-batch`) | Purpose |
| --- | --- |
| `schema ARTIFACT [--output FILE]` | Print/save a schema; use `schema --help` for artifact names. |
| `render-slurm PLAN --output-dir DIRECTORY` | Write scripts for a batch-request plan; does not submit. |
| `summarize PLAN --events-dir DIRECTORY --output-dir DIRECTORY [--revalidate-outputs]` | Summarize task events and optionally reopen products for validation. |
| `retry-manifest PLAN --events-dir DIRECTORY --output FILE` | Write a plan for retryable work; does not submit. |
| `execute PLAN --events-dir DIRECTORY` | Execute serially in the current allocation, not on a login node. |
| `execute-task --manifest PLAN --task-index FILE --array-index N` | Execute an array worker; normally called by generated scripts. |
| `workflow schema` | Print the named-workflow configuration schema. |
| `workflow execute-job PLAN NAME` | Internal individual-job worker; array jobs require the orchestrator. |

The [workflow interface](../../examples/workflow.md) preserves the scientific controllers' arrays
and verifies completion before advancing. Its render command writes a launcher;
scientific scripts are generated after prerequisites become available.
Notice that root `render-slurm` uses `--output-dir`, while the historical,
interpolation and workflow renderers use `--output`.

### Submission and reservation administration

`submission start-operational` is the managed entry point described for R0 above.
The lower-level `submission` commands are `prepare`, `reserve`,
`rollback-reservations`, `test-only` and `submit`. They separate script preparation,
output reservation, rollback, Slurm testing and actual submission. `test-only`
contacts Slurm but does not queue jobs. Do not execute this sequence as an
additional submission of work already started with `start-operational`.

`submission advance OPERATION_RECORD --wave-dir DIRECTORY` reconciles a finished
wave and can submit its successor. `submission summarize-operational
OPERATION_RECORD` regenerates run summaries. Both accept `--revalidate-outputs`.

`reservations list`, `diagnose`, `prune` and `release-stale` inspect or maintain
records that prevent duplicate output writes. `prune` and `release-stale` preview
by default and require `--apply` to change records. Read each command's `--help`
and the [batch submission reference](batch.md) before using these controls.

## Files and further reading

- [Runnable JSON templates](../../examples/README.md): R0, historical and interpolation.
- [Field reference](configuration/README.md): human-readable option descriptions.
- [Full configuration reference](config_reference.json): generated JSON
  documentation, not an executable request.
- [Per-user paths and assets](../paths-and-assets.md): what each operator must change.
- [Environment setup](../setup.md): science and standalone cloud runtimes.
