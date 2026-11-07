# Troubleshooting

| Symptom | Operator action |
| --- | --- |
| Missing local scene | Diagnose/download it, or explicitly allow gaps with historical `missing_reflectance: "allow"` and replan; inspect every logged tile/date |
| Corrupt or changed reflectance | Repair/reacquire and deliberately replan; the missing-reflectance option does not suppress read failures or frozen-source changes |
| Missing asset or grid/unit error | Fix the binding/asset and replan; never disable validation |
| Allocation runs out of RAM | Inspect stage peak, adjust resources, retry supported incomplete work; never reintroduce chunked R0 |
| Failed upstream Slurm dependency | Inspect the upstream stage; do not force downstream publication |
| Reservation or lock conflict | Find its owner/job before retrying; do not blindly remove locks |
| Incompatible scientific history | Compare assets/recipe/domain and escalate; do not edit hashes to force reuse |
| Exit status 3 / full replay required | Read `full-replay-required.json` and durable stop report, including estimated bytes; stop and consult the maintainer |
| Job succeeded but no publication | Inspect scientific receipts and generation pointer; scheduler success alone is insufficient |

The full-replay stop does not wait for interactive approval under cron or Slurm
and does not transfer the complete history in the background. The prior
publication remains current. Normal bounded replay is allowed; exceptional
full recovery is an explicit separate decision, not a training prerequisite.


## Unified workflow stops

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

See [Logs and retries](logging-and-retries.md) for directory layout, retry limits and scope.
