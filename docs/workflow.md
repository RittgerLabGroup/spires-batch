# Universal batch workflow

The schema-2 workflow is an explicit dependency graph around the scientific
controllers. One JSON can carry every request inline; request filenames are an
optional convenience, not a requirement. Planning freezes configuration without
downloading data, building R0, running inversion or submitting scheduler jobs.

```bash
spires-batch workflow schema
spires-batch workflow plan workflow.json --output workflow-plan.json
spires-batch workflow render-slurm workflow-plan.json --output launch
# Inspect allocations, assets and dependencies before explicitly submitting:
bash launch/submit.sh
```

Use the `spipy14` environment and install coordinated production packages before
rendering. `scheduler.python` may explicitly name that environment's interpreter.
Blanca is the default cluster, with `blanca-snow` as the default partition/account;
override these for an authorized `blanca-rittger` allocation. Scheduler node
exclusions are snapshotted when planning. No cron or service is enabled.

## Top-level fields

| Field | Meaning |
| --- | --- |
| `schema_version` | Must be 2 |
| `mode` | `historical` or `nrt`; cycle jobs require `nrt` |
| `sensor`, `platform` | Shared sensor identity; evaluated interpolation supports VIIRS/NOAA-20 only |
| `selection` | Default `tiles`, `water_years`, `dates` for daily jobs |
| `output` | Workflow receipt/plan directory; use a distinct directory for each configuration |
| `scheduler` | Policy file, interpreter, partition, account, cpus, memory, time |
| `jobs` | Named jobs, each with kind, request, dependencies and optional resources |
| `selected_jobs` | Requested jobs; empty selects all |
| `build_prerequisites` | Include transitive prerequisites of selected jobs; defaults false |

Daily requests can explicitly override shared selection. Interpolation requests
must name the full shared tile domain, not separate independent tile jobs: donors
cross tile boundaries. Specify one named historical interpolation job per target
water year, with that year's climatology and a distinct output root. The controller
does not silently invent climatologies or expand a single request across years.
NRT cycles instead maintain their registered context across water-year transitions.

Unknown fields, unknown prerequisites, selected dependency cycles and unsafe job
names are rejected. If a prerequisite is omitted, either select it, enable
`build_prerequisites`, or explicitly remove that dependency because its products
already exist. Merely disabling a stage never silently builds it anyway.

## Job kinds

| Kind | Request and behavior |
| --- | --- |
| `initialize` | [Tile initialization](initialization.md); directory layout, external-asset registration and optional explicit asset staging |
| `daily` | Daily request schema 1; selects `build_r0`, `invert`, `albedo`, etc. through its existing `steps` and typed options |
| `prepare` | Retained-observation preparation request plus an explicit `output` HDF5 path |
| `interpolation` | [Interpolation schema 2](interpolation.md); all four scientific stages and publication |
| `acquire` | `stream_root`, `through`, optional collection; downloads native standard/NRT candidates for an already registered stream |
| `nrt` | Full [cycle configuration](nrt.md) plus `through`; acquisition, matched clouds, retrieval, revision and publication |

`acquire` is stream-aware, not a generic historical archive downloader. Historical
R0/retrieval requests must point to existing reflectance inventories or separately
acquired data. Initial NRT registration remains explicit; the workflow never
guesses an appropriate history to adopt. Preparation requires retained retrievals,
matched QA and corrected cloud context; it cannot recover missing provenance.

Paths in inline requests resolve relative to the workflow JSON. Paths in referenced
requests resolve relative to that request file. The scientific inventory is
resolved just before each job executes, after prerequisites succeed, so a
downstream input need not exist when the workflow graph is first planned.
Scientific preflight remains authoritative; graph planning is not a guarantee
that external data or scientific assets are valid or available.

## Execution and resources

The renderer creates one allocation per named job, links them with `afterok`, and
records returned job IDs in `submissions.tsv`. Each job runs its tasks sequentially
inside that allocation. Independent jobs can run concurrently. Size each allocation
for its peak stage requirements, including all summer scenes in RAM for R0.
The workflow CPU allocation must cover every interpolation stage's configured
thread count. For per-stage resource optimization and large tile/date arrays, use
the standalone daily/interpolation renderers instead; this wrapper does not claim
their fine-grained scheduling or persistent reservation lifecycle.

Workers require a compute allocation. A workflow/job lock prevents duplicate
execution of the same job directory. Completed jobs write receipts only after
successful controller return; downstream jobs also perform scientific preflight.
Receipts describe completed work, not a guarantee against later external product
deletion. Use a new workflow output directory to intentionally regenerate removed
products. Do not direct unrelated concurrent workflows at the same daily output
paths; use the existing reservation-backed submission interface for shared-output
campaigns. Interpolation/NRT retain their own output locks and atomic generation
checks, including rejection of stale parent generations.

Daily jobs execute their configured input staging after planning and before
computation. Unexpected full-history NRT recovery exits with status 3, writes a
durable explanation and does not produce a completion receipt. Dependent jobs
therefore remain blocked; no interactive prompt, approval workflow or automatic
full replay is attempted.

## Templates and scientific compatibility

`examples/workflow_template.json` demonstrates a two-year interpolation graph and
a separately selectable R0 job. Its one-day interpolation requests illustrate
field bindings, not complete-year production inventories. Replace placeholders,
expand every daily date explicitly and configure the evaluated donor domain.
Requests may be inline or files using the same layout. `examples/workflow_nrt.json`
shows stream-aware acquisition and a full cycle using a deployment-specific cycle
request. The existing `examples/config_template.json` documents daily leaf fields.

Versioned recipes, grids, assets and observation lineage—not source hashes—decide
whether checkpoints can be reused. All scientific versions must be updated when
numerical behavior changes. A workflow configuration digest still protects its
own immutable plan; it is not a requirement that the next workflow have the same
digest to reuse compatible scientific history.
