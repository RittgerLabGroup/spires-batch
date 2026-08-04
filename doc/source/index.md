# spires-batch

`spires-batch` is the operational planning and recovery layer for processing
many SPIReS tiles and dates.

Phase A foundations and the independent Phase D daily executor slice are
implemented. The Phase E0 controlled serial exit gate passed on 2026-08-01:

- strict versioned JSON requests;
- immutable resolved task manifests;
- explicit and CURC discovery;
- schema, semantic, inventory, and metadata preflight;
- validated shared-scratch staging;
- dry-run, scientific serial execution, and direct Slurm rendering;
- immutable submission previews, readiness rechecks, and all-or-none output
  reservations with audited pre-submission rollback;
- profile-driven Blanca/Alpine selection, non-mutating Slurm test records, and
  live submission with durable group, job, array-element, and reservation
  identities;
- worker-side reservation verification before scientific writes and
  outcome-derived completed or failed reservation transitions;
- strict stage-gated operational waves, scheduler reconciliation, and capped
  automatic retries through tested `afterany` coordinators;
- structured status, retries, summaries, and output reservations;
- typed preparation, clustering, inversion, and postprocessing options;
- atomic raw-product persistence and reopened scientific validation.

Existing-R0 inversion and full-product postprocessing use the current public
scientific APIs directly rather than the older `spires-io` manifest item
reader. Scene bands are explicitly configured or derived from labeled R0
products, then selected in order from the master reflectance LUT. R0 builds use
the strict `viirs_summer_composite` and `modis_summer_composite` recipe names.
Standalone `results_subset` postprocessing is rejected during preflight. The
`interpolate` stage is represented by the schema and generic task model but
intentionally rejected until Phase F defines temporal windows and dependencies.

## Typical planning flow

```bash
spires-batch validate request.json
spires-batch plan request.json --output resolved-plan.json
spires-batch dry-run resolved-plan.json
spires-batch stage resolved-plan.json
spires-batch execute resolved-plan.json --events-dir task-events
spires-batch render-slurm resolved-plan.json --output-dir slurm-preview
```

Slurm rendering does not submit jobs. Each script records its explicit cluster,
partition, account, and QoS; built-in profiles cover Blanca Snow, Blanca
Rittger, and Alpine's `acpu` CPU partition.

Phase E1/E2 add ``spires-batch submission prepare`` and
``spires-batch submission reserve``. Phase E3 adds ``submission test-only`` and
``submission submit``. Together these commands persist and hash the exact
intent, recheck mutable inputs and outputs, acquire all reservations, retain
Slurm's non-mutating validation, and submit once under an exclusive lock.
Returned base job IDs and array indices are attached to reservations and
retained in immutable ``scheduler-submission.json``. E4 passes the immutable
reservation set to each submitted worker, verifies its exact output and Slurm
identity before scientific writes, and derives completed or failed reservation
state only after the terminal task event is durable. Use
``submission rollback-reservations`` only when a prepared run will not
proceed; it refuses the complete set after any job identity is attached.

E5 adds ``submission start-operational``. It submits only dependency-free
tasks, then a small ``afterany`` coordinator reconciles exact worker events
with ``sacct``. Eligible transient failures are written to immutable retry
manifests, failed same-family reservations are re-armed under new submission
and Slurm identities, and downstream tasks are released only after complete
upstream success. Deterministic, cancelled, and retry-exhausted failures
durably block all unsubmitted downstream tasks.

The default package installation provides the planning core. Install the
scientific runtime from a source checkout with ``pip install '.[science]'``;
on CURC, the merged sibling editable stack in ``spipy14`` remains preferred.

## Schemas

```bash
spires-batch schema request
spires-batch schema resolved-plan
spires-batch schema submission-record
spires-batch schema reservation-set
spires-batch schema scheduler-test
spires-batch schema scheduler-submission
spires-batch schema operational-run
spires-batch schema operational-advance
```

Requests and manifests use schema version `1`. Canonical JSON SHA-256 digests
track request intent and resolved task identity without changing scientific
filenames.

## Operational paths

Authoritative `/pl` roots, scratch roots, patterns, output roots, and ancillary
locations are supplied in each JSON request. No site path is embedded in the
public package. Resolved tasks retain both authoritative and staged paths.

For the complete configuration, naming, preflight, staging, status, retry, and
reservation reference, see the
[repository README](https://github.com/SPIReS-Organization/spires-batch).
