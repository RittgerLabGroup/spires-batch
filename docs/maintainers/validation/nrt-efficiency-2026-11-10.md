# NRT efficiency validation — November 10, 2026

The existing five-tile WY2026 continuation through May 18 was rerun after
adding bounded process pools to `cycle --execute` and incremental cumulative
NetCDF publication. The committed historical generation through May 11 remains
the starting state. The run reuses 35 completed retrievals and recomputes all
May 5–18 interpolation tasks; retrieval time is excluded from comparisons.

Completed checks:

- Both existing small-grid scientific replays produced identical values using
  process pools (all eight issues and first/final-only).
- All ten existing cumulative products passed full value/mask comparisons for
  fixed-time conversion, suffix rewriting and appending dates.
- A native-grid h10v04 copy covered every variable/pixel across 16 dates,
  including an eight-day compressed prefix. All values matched.
- Scheduler templates validate; package syntax and whitespace checks pass.

The new acceptance allocation is 16 CPUs / 96 GiB on blanca-snow. It fits
15 screening workers, four spatial workers with four threads each, 11 property
workers and two exports. Pool limits include 2 GiB coordinator headroom.

New cumulative files have an unlimited time dimension. Subsequent revisions
copy the previous immutable file and change the affected suffix. Existing
fixed-time files need a one-time conversion; compatible complete chunks transfer
without decompression, while boundary/revised dates use the regular writer.
Copying and durable archive transfer remain I/O costs. Scientific algorithms,
donor pools, output variables and issue cutoffs are unchanged.

Full-grid NRT 29439290, QA 29439291 and recovery 29439292 completed successfully.
The retry took 2h37m, including about 2h26m of cumulative publication. QA checked
all 40 products and sampled scientific values; recovery checked retry/no-op
behavior and the explicit full-replay guard. All 1,288 reused artifacts remained
unchanged. These timings include reused work and are not a fresh end-to-end
speedup measurement. Scientific map review remains separate; the subsequent
combined-source integration is recorded below.

The first full-grid rerun (29438485) completed screening, spatial interpolation
and property calculation, then failed during export. The process pool submitted
its initial indices twice because its replenishment iterator still started at
zero. Duplicate tile exporters collided on the publication lock. The correction
consumes initial submissions from that same iterator; publication locks and
scientific workers are unchanged. The retry uses the same revision plan and
workspace so completed artifacts can be verified and reused. The historical
generation remains the committed state until cumulative publication succeeds.
Retry source hashes, prior metrics, artifact inventory and replacement job
dependencies are recorded in `archive/nrt-lock-resume-20261110/` at the
organization workspace root. Corrected-dispatch preflight 29439289 passed both
existing small-grid replays with identical values; their task logs confirm
exactly one invocation of every planned index.

The retry is 29439290, with QA 29439291 and recovery 29439292 chained by
successful completion. Before submission, all 600 screening tasks, 14 spatial
dates and 600 property tasks passed the existing artifact verifier. The two
completed tile exports (h08v04/h08v05) also passed. Sizes and modification times
of 1,288 existing artifacts were saved for the follow-up reuse check. The retry
retains the original plan identity and committed historical generation.

Operational evidence and job ledger:
`/projects/ropa5718/SPIReS-Organization/archive/nrt-efficiency-20261110/`.
The separate two/eight-output export benchmark completed in its original runtime.

## Exporter integration and final-only output — November 13

The six export comparison cases passed exhaustive science-value/NaN-mask,
coordinate/metadata and checkpoint comparisons. The prototype's two eight-file
exports took 248.87 and 213.27 minutes, versus 856.68 and 529.46 minutes for the
current eight-file writer. This is a 67% reduction in median export time,
excluding verification, on one tile with two repetitions and uncontrolled shared
filesystem load. Output size increased from 72.13 to 79.21 GiB. Records are in
`archive/export-comparison-20261110T145233Z/` at the organization workspace root.

The maintained historical/replay-interval exporter now uses that same combined
configuration: full-width chunks, 4 MiB per-variable caches, a 223-day transfer
limit and a 512 MiB array budget. Compression remains lossless zlib level 2 with
shuffle. Cumulative snapshots retain their separate incremental-copy layout.

`output_issues: final_only` selects issue 8 (age 7) throughout export, workflow
verification, NRT bootstrap and cumulative publication. It preserves the full
variable schema and restart checkpoints. Empty products obtain their schema from
the selected tile export rather than requiring a first-issue file. The public
configuration remains draft version 1, and the default remains `all_issues`.

The existing 4×5 scientific fixture was replayed with all three modes. Historical
outputs and NRT continuation values/masks matched the existing reference products;
workflow verification and idempotent retries passed. Empty final-only export and
bootstrap also passed. The existing replay guard stopped an attempted continuation
from an insufficient three-day checkpoint; no guard or scientific calculation was
changed. Generated configuration references and package syntax checks passed.
The maintained exporter also passed a one-stripe h10v04 check at native width
(20×2400 pixels, 216 final-issue dates): every scientific value/NaN mask matched
the completed property inputs, and the output used 8×20×2400 chunks. This is
layout/value validation, not a new full-tile performance measurement.
Evidence is in `/projects/ropa5718/SPIReS-Organization/archive/production-export-final-only-20261113/`.
No additional full-grid timing campaign was submitted.

## RLG-spipy integration — November 13

Development moved to `/projects/ropa5718/RLG-spipy`. The unpublished changes
were applied on top of the RittgerLabGroup optional-climatology commits
(`66e1baf` for batch, `b7f3ecd` for interpolation). Both code histories and
climatology configuration/provenance were retained. Only RittgerLabGroup `main`
is the publication target. Completed-job records and immutable datasets remain
at their original SPIReS-Organization/scratch paths.

The merged implementation passed the existing 4×5 fixture for all six combinations
of climatology enabled/disabled and all-issues/first-and-final/final-only output.
Each completed historical export, NRT bootstrap, append through day 16, workflow
artifact verification, exact retry and no-op handling. Enabled-mode scientific
values/masks matched the pre-migration reference; disabled-mode two/final-only
products matched the disabled all-issues result. NetCDF climatology provenance,
default-enabled compatibility and strict Boolean validation passed. No additional
full-grid campaign or new test suite was introduced. Package syntax, generated
configuration/workflow references and Git whitespace checks passed.

The dispatcher and issue selector were transferred byte-for-byte from their
validated source copies. Installation source pins are updated in the follow-up
candidate snapshot commit; that metadata commit deliberately references the
preceding executable-code commit. Historical reports retain their original pins.
Combined validation and migration backup:
`/projects/ropa5718/RLG-spipy/archive/workspace-transfer-20261113T211401Z`.
