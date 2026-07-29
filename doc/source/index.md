# spires-batch

`spires-batch` is the operational planning and recovery layer for processing
many SPIReS tiles and dates.

Phase A is implemented:

- strict versioned JSON requests;
- immutable resolved task manifests;
- explicit and CURC discovery;
- schema, semantic, inventory, and metadata preflight;
- validated shared-scratch staging;
- dry-run, serial, and direct Slurm-rendering foundations;
- structured status, retries, summaries, and output reservations.

Scientific stage execution is connected later in Phase D. The `interpolate`
stage is represented by the schema and generic task model but intentionally
rejected until Phase F defines temporal windows and dependencies.

## Typical planning flow

```bash
spires-batch validate request.json
spires-batch plan request.json --output resolved-plan.json
spires-batch dry-run resolved-plan.json
spires-batch stage resolved-plan.json
spires-batch render-slurm resolved-plan.json --output-dir slurm-preview
```

Slurm rendering does not submit jobs.

## Schemas

```bash
spires-batch schema request
spires-batch schema resolved-plan
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
