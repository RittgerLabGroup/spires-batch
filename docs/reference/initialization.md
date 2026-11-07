# New-tile initialization

Use `spipy14` and start with `examples/initialization.json`. Change the tile
domain and processing water years before applying it. A domain is the complete
set of tiles that will share interpolation donors, not necessarily one tile.

```bash
spires-batch tile-init initialization.json --dry-run
spires-batch tile-init initialization.json
```

The dry run only inspects paths and file sizes. Initialization creates directories
and writes a catalog and receipt; it does not download reflectance, generate
ancillaries, construct R0, bootstrap an NRT stream, or submit jobs. Missing assets
are expected during setup and are reported rather than replaced with dummy files.
The example registers externally produced LUTs, models and ancillary files.
New R0 outputs are built by the next task rather than registered as existing inputs.

## Layout

Defaults are site conveniences, not hardcoded scientific input locations:

```text
/pl/active/rittger_ops/
  vj109ga.002/                         existing shared archive; untouched
  vnp09ga.002/                         existing shared archive; untouched
  spipy/
    assets/
      luts/
      cloud-models/
      ancillary/<tile>/<version>/
      climatology/<tile>/<version>/
      r0/<platform>/<tile>/summer-<year>/
      catalogs/<domain>.json
    products/<platform>/<tile>/
    state/<platform>/<domain>/
    runs/initialization/<domain>.json

/scratch/alpine/ropa5718/spipy/
  cache/assets/                       registered immutable asset copies
  work/<platform>/<tile>/WY<year>/    parent for separate run directories
  streams/<platform>/<domain>/       reserved working-stream location
```

Summer directories use processing WY minus one. This is a Northern Hemisphere
layout convention, not a request to build a summer composite. Each run should
use a distinct child of its scratch work directory. Keep the authoritative
reflectance archives outside SPIpy; initialization neither creates nor scans
their contents. The report only checks whether the configured roots exist.

Initialization is repeatable. Reusing a domain with a different tile set, sensor,
platform or storage roots is rejected rather than silently moving an operational
stream. Use another domain or explicitly migrate existing state. Expanding the
registered water years and asset inventory is allowed. No existing data files
are deleted or overwritten by directory creation.

## Register externally produced assets

Add individual files to `assets`, with paths relative to the durable `assets/`
directory. For example:

```json
{
  "id": "noaa20-h09v04-r0-summer2025",
  "kind": "r0",
  "relative_path": "r0/noaa20/h09v04/summer-2025/r0.nc",
  "tile": "h09v04",
  "water_year": 2026,
  "required": true
}
```

Kinds are `lut`, `cloud_model`, `ancillary`, `climatology`, and `r0`. Tile-specific
kinds require `tile`; R0 also requires its processing `water_year`. Register each
file once, even when it contains multiple datasets. `required` defaults to true.
Names, formats and dataset bindings remain the responsibility of the external
asset producer and scientific request. Asset availability is not a claim that
all inputs required by a particular scientific recipe have been registered.

The receipt includes missing required files, unregistered tile/WY R0 bindings,
per-file sizes and source/scratch paths. Its `existing_r0` object is a valid daily
request fragment with `mode: "existing"`; it never requests an R0 build. Normal
daily metadata preflight still checks the scientific compatibility of that R0.
If construction is explicitly selected in a separate daily job, all summer
scenes for one tile/year must fit in RAM; no chunked/Zarr R0 execution is added.

## Scratch staging

After assets are supplied, preview and then explicitly stage them:

```bash
spires-batch tile-init initialization.json --stage-assets --dry-run
spires-batch tile-init initialization.json --stage-assets
```

The command prints per-file and aggregate transfer sizes before copying. It
copies only the registered available files, never an entire directory tree.
Missing required files stop staging before any file is copied. Optional missing
files are reported and skipped. Copies use temporary files and atomic promotion;
unchanged cache entries are reused by size and nanosecond modification time.
Assets are expected to be immutable/versioned; stat matching is not a content
checksum. Reflectance and state history are never staged by this command.

Use the reported scratch paths in processing requests when desired. Alternatively,
daily requests can keep authoritative paths and enable their existing
`execution.staging` mechanism; the universal workflow now executes that explicit
staging step before daily computation. These are alternative cache layouts; do
not stage the same inventory both ways unnecessarily. Initialization does not
rewrite arbitrary scientific request files or their restart identities.

## One universal JSON

An initialization configuration can be inline in a baseline workflow:

```json
{
  "kind": "initialize",
  "request": {
    "config_type": "initialization",
    "schema_version": 1,
    "domain": "h09v04",
    "water_years": [2026],
    "tiles": ["h09v04"],
    "assets": [],
    "stage_assets": false
  }
}
```

This is a named-job fragment, not a complete workflow. Sensor/platform come from
the workflow; tiles and water years default to its shared selection. Storage
roots may be overridden inside the request. Subsequent jobs can depend on this
job. The standalone initializer can run without an allocation when just making
directories; use a compute allocation for substantial transfers. Workflow workers
retain their existing allocation requirement.

## State and output boundaries

The initializer does not adopt, move or recursively synchronize existing NRT
streams. `state_root` is the durable location and `scratch_stream_root` is a
reserved path, not an initialized or automatically recoverable stream. Continue
to use explicit historical bootstrap and NRT storage configuration. In particular,
do not move the sole current checkpoint to scratch merely because its directory
now exists. Full scratch-stream restoration/publication automation is separate
from this directory-and-assets initializer. Historical output archival also
remains an explicit processing/deployment step; no blanket rsync is installed.

Unexpected full-history NRT replay is now stopped before full-history planning
or staging. See [NRT storage](storage.md#unattended-full-replay-stop) for the
durable report and scheduler exit behavior. No approval CLI or transfer cap is
introduced.
