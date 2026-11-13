# Run the complete chain from one JSON

Use [workflow.json](workflow.json) for the five NOAA-20 tiles and WY2026.
Complete [environment setup](../docs/setup.md) and review
[paths and assets](../docs/paths-and-assets.md) first.
It contains the processing requests and scheduler policy together. The initial
historical interval ends May 11, 2026, matching the available training inputs;
the NRT job continues through May 18. For a full WY, extend the historical dates
only after the remaining reflectance and annual assets are available.

## 1. Prepare your configuration

Copy this one JSON into your run directory. Set `variables.scratch`,
`variables.science_python` and `variables.cloud_python`, and replace remaining
`REPLACE_USER` strings in durable domains/archive paths. The examples do not
expand `$USER`. Install the updated batch code in the science environment used
by controllers and scientific workers; an older frozen environment cannot run
these commands.

Review `jobs` in order:

1. `initialize` registers existing non-R0 assets and stages those files to scratch.
2. `background` builds fresh summer-2025 R0 for WY2026, one exclusive full-memory blanca-snow
   task at a time. Historical and NRT baseline/axis bindings point to these files.
   Its controller creates the output/reservation directory and stages the planned
   summer inputs inside its allocation before submitting R0 workers.
3. `historical` processes dates concurrently, with five concurrent tile retrievals
   per date. Its start and stop dates are inclusive.
4. `interpolate` assembles those receipts automatically, then runs the stage arrays.
5. `register` configures the durable archive and adopts the completed products and
   checkpoints. This first archival step can transfer substantial history.
6. `continue_nrt` executes the held-out dates using that registered history.

LUTs, cloud models, ancillary maps and historical reflectance must already exist.
The workflow does not generate missing maps or download a full historical archive.
The historical job defaults to stopping on missing local reflectance. To allow
unknown tile/date observations, set
`jobs.historical.request.missing_reflectance` to `"allow"` instead of `"error"`.
The historical planner records all gaps when that stage is reached; initial
workflow planning does not yet inventory them. Review the
[missing-observation policy](historical.md#choose-what-to-do-with-missing-tiledates)
before enabling it. Corrupt files and failed retrievals are never converted to gaps.
Keep the full tile domain when reproducing this example. A tile is not an
independent interpolation/cloud-context domain.

For another WY, update the R0 processing years and descriptive filenames, the
historical and interpolation date bounds, annual asset bindings, NRT `through`,
and output paths. R0 derives the preceding June–September window automatically.
Changing one year field does not invent or relocate annual assets.

## 2. Review without submitting

```bash
spires-batch workflow plan workflow.json --output workflow-plan.json
spires-batch workflow render-slurm workflow-plan.json --output launch
```

Read `launch/preview.json`: check paths, dependencies, node exclusions, queues,
per-stage budgets and separate coordinator resources. Scientific input checks
are repeated inside allocations when each stage's prerequisites exist.
These two commands do not stage data or submit jobs.

## 3. Submit the reviewed chain

```bash
module try-load slurm/blanca
bash launch/submit.sh
```

The controller submits the first jobs and returns. Successful verification
automatically triggers later submissions; you do not keep a terminal open or
manually submit each processing stage. Scientific work still uses the dedicated
R0, historical and interpolation array controllers.

Alternatively, for a configuration you have already reviewed, combine planning
and submission into one command, using a new plan filename:

```bash
spires-batch workflow submit workflow.json --output workflow-plan.json
```

Use one submission route, not both. An existing plan/run is not overwritten.

## 4. Monitor and inspect results

```bash
spires-batch workflow status workflow-plan.json
```

This reports the recorded state, job IDs and `log_directory`, not live Slurm
accounting. Open `summary.txt` there, then the tile reports and detailed logs.
Set `log_root` in the JSON to choose a different base directory. Recognized
transient worker failures get up to `max_auto_retry_count` additional attempts
(3 by default); failed checks or exhausted retries stop downstream work.
See [Logs and retries](../docs/logging-and-retries.md) for limits and overrides.
Inspect maps, QA, first/final cutoffs
and publication receipts before treating the result as scientifically accepted.

In the example, historical first/final products reach May 11/May 4. After the
May 18 continuation, they should reach May 18/May 11. NRT full-history recovery
still stops with its transfer-size report instead of copying everything silently.

The interpolation job's `request.output_issues` defaults to `all_issues`: six
intermediate NetCDFs fill the daily cutoffs between first and final. Set
`first_and_final_only` to write only those two files, or `final_only` to write
only the final issue (all variables, available through seven days before the
input availability date). Checkpoints are retained in all modes. NRT inherits
this setting and publishes cumulative tile/WY files containing every output variable.

## 5. Submit another NRT update from the same JSON

```bash
spires-batch workflow cycle workflow.json --job continue_nrt --through 2026-05-19 --output cycle-20260519-plan.json
```

This requires registered history and selects only the NRT job. It creates a new
cycle run directory alongside the new plan, leaving historical work untouched.
May 19 is illustrative: review acquisition and asset availability first. Do not
overlap updates to the same stream. Cron/service scheduling remains a separate,
explicit deployment step.

See [troubleshooting](../docs/troubleshooting.md) for stopped jobs and
[workflow configuration rules](../docs/reference/configuration/workflow.md) for
variables and dependencies. No live Slurm submission or full-grid acceptance is implied by this
example; validate the updated orchestration in your deployed environment first.
