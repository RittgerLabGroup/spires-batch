# Execution architecture

Operators should start with the [workflow walkthrough](../../examples/workflow.md).
This page describes controller responsibilities, not another submission sequence.

## Jobs and automatic handoffs

| Kind | Behavior |
| --- | --- |
| `initialize` | Register directories/assets, optionally stage the listed assets; missing required assets stop the chain |
| `daily` | Run a batch-request plan (including R0) through the existing managed arrays, reservations and capped retries |
| `historical` | Plan and submit historical date arrays, retaining concurrent tile workers |
| `interpolation` | Assemble historical receipts when `historical_job` is supplied; plan and run stage-specific arrays |
| `register` | Configure durable storage, then register the named interpolation job's completed history and inventory |
| `nrt` | Execute acquisition, cloud preparation, retrieval, revision and publication in its allocated resources |
| `prepare` | Prepare retained observations using explicit field bindings |
| `acquire` | Acquire native observations for an already registered NRT stream |

Every dependency must appear in `depends_on`. For an interpolation job,
`historical_job` names a historical dependency; the controller runs
`historical assemble` and retains `bound/interpolation.json` and
`bound/inventory.json` automatically. A register request contains exactly
`interpolation_job`, `stream_root`, and an inline `storage` configuration.
Its interpolation prerequisite must have a `historical_job`.

Paths connecting R0 outputs to retrieval inputs remain explicit, as do scientific
dataset selectors. Registering assets does not generate LUTs, masks or ancillaries.
Historical reflectance must already be downloaded; `acquire` is not a historical
archive downloader. The private earthdata-downloads utility remains independent.

## Resources and execution

There is no season-long serial wrapper around the array stages:

- R0/batch jobs use their existing managed submission controller.
- Historical processing uses arrays of dates and the configured tile concurrency.
- Interpolation uses its existing screening, spatial, properties and export arrays,
  with the configured resources for each stage.
- Chunks run in order, preserving each renderer's concurrency limit. The next chunk
  or stage is submitted only after accounting and scientific completion checks.
- Initialization, registration, preparation, acquisition and NRT are individual
  allocations. Independent ready jobs can run concurrently.

The workflow's `coordinator` provides separate, non-preemptable Blanca resources
for planning, verification and submissions. It is not a scientific-worker budget.
Choose enough memory/time for metadata inspection of your domain.
`scheduler.python` must name the installed, executable science interpreter.
All participating runtimes must contain the coordinated current code.

`scheduler.policy` accepts a policy filename or the complete policy object.
Per-job `job_type` and `queue` choose profile defaults. Scientific array budgets
live in the child request's scheduler/execution settings. Explicit job
`resources` override historical/interpolation/NRT scheduler values; batch
overrides instead belong in `request.execution.resources`. Interpolation
stage overrides still apply. Top-level scheduler resource defaults apply to
individual allocations, not as implicit overrides of scientific array budgets.
All unified jobs currently use Blanca; array queues can be explicitly preemptable.
The usual node exclusions remain in every generated submission.


## Submission records and failure boundaries

Each submission has an intent record before Slurm is called. Accepted job IDs are
recorded while jobs are held, before release. Continuations use `afterany`, then
check array accounting and scientific outputs; scheduler success alone is not
enough. R0/batch continuations follow the existing retry coordinators to a terminal
result. Interpolation export verification checks the published generation and
its product/checkpoint artifacts.

`OUTPUT/control/state.json` records jobs, Slurm identities, progress and failure
reasons. Adjacent submission records preserve scripts, hashes and accepted IDs.
Logs and readable summaries are beneath the workflow's `log_root` (default
`OUTPUT/logs`), in an identity-named run directory. `workflow status` includes
`log_directory`. Scientific plans remain under each job directory. `workflow status` reads this durable record; it does not query
Slurm, so a killed/cancelled controller may still appear queued or running.
Check its recorded job ID in Slurm if progress stops.

Any failed verification blocks new downstream submissions. Already running
independent work is not cancelled. A submission timeout, missing coordinator or
uncertain release is not automatically retried: inspect the ledger and Slurm
before recovery. Recognized transient worker failures receive bounded retries;
failed scientific checks and exhausted retries stop the chain. Array retries
include only failed indices; individual workers use separate follow-up controllers.
Never delete the state file
or rerun the launcher to bypass this protection. There is no general automatic
resume command for an interrupted unified chain yet.

NRT's existing full-history replay stop remains active, including exit status 3
and the durable size report. Initial registration is an explicitly requested
archival operation and can copy substantial data; it is not a daily replay.
Automatic checks do not replace scientific review of a representative pilot.

See [Logs and retries](../logging-and-retries.md) for directory layout, retry limits and scope.
