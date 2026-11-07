# Configure archival storage and register history

Use [storage.json](storage.json) after historical interpolation has completed
and passed review. The unified workflow's `register` job already performs this
handoff; do not repeat it for the same stream.

## 1. Choose independent destinations

The working `stream_root` in [cycle.json](cycle.json) belongs on scratch.
Set `archive_root` in storage.json to a writable durable destination under
`/pl/active/rittger_ops/spipy`, distinct from other operators' streams.
Review retention settings without applying cleanup.

## 2. Configure storage

From the directory containing your adapted JSON files:

```bash
STREAM_ROOT="$(python -c 'import json; print(json.load(open("cycle.json"))["stream_root"])')"
spires-batch nrt storage-config storage.json --root "$STREAM_ROOT"
```

Configure storage **before** registration; otherwise archival defaults beneath
the working stream, which could leave both copies on scratch.

## 3. Register the completed historical results

Retain the exact interpolation plan and assembled native-source inventory.
Initial registration archives existing data and can involve substantial I/O;
check input sizes and destinations and run in a suitable Blanca allocation:

```bash
spires-batch nrt bootstrap interpolation-plan.json bound/inventory.json --root "$STREAM_ROOT"
spires-batch nrt status --root "$STREAM_ROOT"
spires-batch nrt storage-report --root "$STREAM_ROOT"
```

Despite its CLI name, `bootstrap` registers real history; it does not produce
a synthetic training dataset. Verify the publication and durable archive before
considering scratch cleanup. Storage configuration alone is not a published stream.

## 4. Continue

[Run the next NRT update](cycle.md). Unexpected full-history recovery stops
instead of transferring the whole archive silently. Consult
[storage rules](../docs/reference/storage.md) before retention or migration.
