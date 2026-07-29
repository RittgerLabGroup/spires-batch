# spires-batch

`spires-batch` is the operational planning and recovery layer for processing
many SPIReS tiles and dates.

Phase A foundations and the independent Phase D daily executor slice are
implemented:

- strict versioned JSON requests;
- immutable resolved task manifests;
- explicit and CURC discovery;
- schema, semantic, inventory, and metadata preflight;
- validated shared-scratch staging;
- dry-run, scientific serial execution, and direct Slurm rendering;
- structured status, retries, summaries, and output reservations;
- typed preparation, clustering, inversion, and postprocessing options;
- atomic raw-product persistence and reopened scientific validation.

Existing-R0 inversion and full-product postprocessing use the current public
scientific APIs directly rather than the older `spires-io` manifest item
reader. Scene bands are explicitly configured or derived from labeled R0
products, then selected in order from the master reflectance LUT. R0 recipe
identifiers and standalone `results_subset` postprocessing remain integration
decisions. The `interpolate` stage is represented by the schema and generic
task model but intentionally rejected until Phase F defines temporal windows
and dependencies.

## Typical planning flow

```bash
spires-batch validate request.json
spires-batch plan request.json --output resolved-plan.json
spires-batch dry-run resolved-plan.json
spires-batch stage resolved-plan.json
spires-batch execute resolved-plan.json --events-dir task-events
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
