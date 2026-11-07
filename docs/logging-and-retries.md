# Logs and automatic retries

## Choose where logs go

Set these fields in the unified workflow JSON:

```json
{
  "log_root": "/scratch/alpine/your_username/spipy/logs",
  "max_auto_retry_count": 3
}
```

This is a fragment, not a complete configuration. `log_root` is optional; by
default it is `output/logs`. Relative paths are relative to the workflow JSON.
Every planned workflow gets its own identity-named directory below that root,
including each separately planned NRT cycle. Scientific output paths do not change.
Choose a writable directory and retain it as long as you need the operational record.

Run `spires-batch workflow status PLAN` to find `log_directory`, the job IDs and
the recorded progress. Start with `summary.txt` in that log directory:

| Location beneath the run's log directory | Contents |
| --- | --- |
| `summary.txt` | Whole-chain progress, verified task counts, failures and links to detailed reports |
| `summary.json`, `attempts.csv` | Machine-readable state and task attempts |
| `tiles/TILE/summary.txt` and `.json` | Tile progress, attempts and verified dates deliberately left missing |
| `JOB/controller-JOBID.out` | Planning, verification and submission messages |
| `JOB/*.out`, `JOB/*.err` | Scientific-worker/array Slurm output, separate for each submitted job ID |
| `JOB/TILE/DATE/*.log` | Reflectance preparation, retrieval/canopy and albedo output for that tile/date |
| `JOB/shared/DATE/*.log` | Cloud inference and other shared date/domain work |
| `JOB/TILE/undated/*.log` | Interpolation stripe/export tasks spanning many dates |
| `JOB/operation/` | Managed batch/R0 logs, attempts and existing run/tile summaries |

Each detailed scientific log has a JSON sidecar with its stage, tile/date where
applicable, Slurm identifiers and completion/error status. Retries create new
logs rather than overwriting the old ones. Concurrent tile workers have separate
files. A killed process may leave its sidecar marked `running`; the controller's
recorded Slurm outcome is authoritative in that case.

Summaries update at controller transitions, not continuously from Slurm. A date
array's outcomes appear after its current chunk ends. Shared tasks appear in
each affected tile's report; counts are tasks, not pixels or unique observations.
Allowed missing reflectance is listed as a gap, not a successful retrieval.
R0's established detailed attempt counts remain in its linked managed reports.
The durable submission ledger stays at `OUTPUT/control/`, alongside immutable
job plans under `OUTPUT/JOB/`; changing `log_root` does not move that control state.

## What gets retried

By default the unified workflow permits **three retries after the first attempt**.
Set `max_auto_retry_count` to `0` to disable them. A job can override the workflow
default with `jobs.NAME.max_auto_retry_count`. For managed batch/R0 jobs, an
explicit `request.execution.max_auto_retry_count` overrides the workflow default;
the per-job setting overrides both.

- Recognized node/boot failures, preemption, time limits and out-of-memory
  termination can be retried. Resources and queue remain unchanged. Repeated
  memory/time failures still need an operator to revise the request.
- Historical and interpolation arrays resubmit only transiently failed indices.
  Successful outputs must pass scientific checks; the next chunk/stage waits
  until the current one is verified. A historical index is a whole date/domain,
  not one tile; complete compatible intermediate files can be reused.
- Individual initialization, preparation, acquisition, registration and NRT
  allocations have separate follow-up controllers, so a killed worker can be
  noticed and retried. NRT resumes a saved revision only against its original
  parent history, or recognizes that exact revision's completed publication.
  If planning did not finish, a retry uses a fresh cycle workspace.
- R0/batch retains its existing managed retry and summary infrastructure.

Bad inputs, corrupted files, failed scientific verification, cancellation and
unclassified failures stop the chain. Missing/uncertain scheduler accounting,
ambiguous submission/release and a failed controller are **not** automatically
resubmitted. Neither is the full-history replay stop: its transfer-size report
still requires operator diagnosis. Already running independent jobs are not
cancelled when another branch fails.

These additional array/individual-job retries are provided by `workflow submit`,
`workflow start` and `workflow cycle`. Standalone historical/interpolation
`submit.sh` scripts do not gain a retry controller. Standalone scientific calls
write their detailed logs beneath their working/output directory; managed batch
commands retain their own log/output arguments.

Regenerate workflow plans from the JSON before using this controller version.
The public JSON baseline is still version 1. Existing submitted/frozen workflows
are not modified, and there is no general command to restart a failed controller.
