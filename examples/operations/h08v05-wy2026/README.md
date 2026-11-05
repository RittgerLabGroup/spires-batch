# h08v05 WY2026 acceptance inputs

These are real campaign starting configurations, not completed science results.
Do not submit the entire campaign until assets, environment and donor-domain
choices are approved. Read `docs/operations/historical.md` first.

| File | Purpose and readiness |
| --- | --- |
| `campaign_inventory.json` | Documentation-only date coverage and campaign choices; not a CLI request |
| `tile_initialization.json` | Valid layout-only initializer; asset list intentionally empty until files are supplied |
| `r0_request.json` | Fresh previous-summer R0 from 122 existing shared archive scenes; one full-memory exclusive blanca-snow node |
| `cycle.json` | Reusable science/cloud interpreters and tile/WY retrieval recipe; asset paths and cloud-policy checksum must be completed |

R0 uses explicit 2025 summer source dates, not `selection.water_years=[2026]`,
which would incorrectly filter the source inventory. The resulting artifact is
bound to processing WY2026. A fresh acceptance output prevents old R0 reuse.
The staged source list contains only the needed summer scenes, never the entire
shared reflectance archive.

The R0 request already passed planning/preflight against the current archive.
Its initial execution completed successfully as Blanca job `29318329`; see
the historical guide for measured resources and the local execution record.
Its `memory: "0"` and `extra_directives: ["--exclusive", "--nodes=1"]` belong to
the standalone daily renderer, not the universal workflow resource schema.
For other jobs, use blanca-snow/blanca-rittger first. Approved preemptable
overflow must retain the verified operational node-exclusion list; no empty or
invented replacement for that list is supplied here.

`cycle.json` deliberately contains a non-checksum marker rather than a fabricated
model identity. Replace it with the SHA256 of the actual model manifest and use
the same complete observation policy in the historical interpolation request.
The background binding initially points to the fresh scratch R0; archive/register
that output and finalize stable production bindings before a durable NRT handoff.
The ancillary and LUT paths describe required destinations; they do not claim
those files already exist. Verify the cloud interpreter and native libraries.

Generate prepared observations using the existing `pipeline.prepare_date` API,
then populate a complete interpolation inventory from its real receipts.
`docs/operations/historical.md` explains this missing assembly boundary; no
synthetic daily observations are provided to conceal it.

Historical downloads are separate: use
`earthdata-downloads/examples/h08v05_wy2026.json` in the private acquisition
repository. The initial 230-day history needs no new reflectance download;
the remaining WY dates can be acquired later.
