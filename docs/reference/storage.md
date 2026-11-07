# NRT storage, retention and recovery

NRT keeps a bounded working inventory and durable, versioned scientific archives.
The numerical 66-day snow/property/age checkpoint and cross-water-year behavior
are unchanged. No benchmark or automatic production cleanup is implied by these
commands.

## Layout and daily I/O

The stream root belongs on fast storage. Set `archive_root` before first publication
to place durable archives on a different filesystem; otherwise it defaults to
`STREAM/archive`.

- `current.json` commits a complete donor-domain generation.
- `generations/` contains current/recent checkpoint bundles and compact ledgers.
- `objects/checkpoints/` stores shared immutable checkpoint files. Generation and
  worker-output entries are hard links on one filesystem, avoiding payload copies.
- `cache/` holds the working native/prepared inputs. The default window is 90 days,
  plus all unresolved NRT or missing dates and their same-day donor domain.
- `workspaces/` records explicitly owned cycle/revision directories and persistent
  work pins. Queued work never expires merely because it has waited a long time.
- The archive contains native inputs, prepared observations, published products,
  annual assets, matching code snapshots, configurations, provenance, monthly
  catalogs, publication records and selected cold recovery checkpoints.

A new publication archives inputs/products before advancing the domain pointer.
Published product paths therefore survive removal of the worker workspace. Native
records retain their original source identity when their physical path moves;
archival relocation does not masquerade as a new observation or cloud context.
Code, configurations and receipts are copied into independent snapshots, so
later edits or retries cannot mutate their archived versions. Cross-filesystem
copies are checksum-verified. Same-filesystem hard links for immutable data avoid
rereading large immutable files. New inputs use their original fast-storage files
to populate the working cache after archival, avoiding a cold-storage round trip.
Source files must remain write-once: do not edit
an archived file through an older hard link.

The schema-2 ledger contains recent/unresolved records and references to immutable
monthly catalog shards, rather than the entire observation/product history. Only
changed monthly shards are rewritten. Older source/prepared/product vintages
remain reachable through archived publication records. The catalog grows with
history, but normal cycles do not load its old monthly records or raster arrays.
Seeded interpolation plans validate only their replay dates. A late replacement
or explicit rebuild loads the required archive metadata on demand.

Restart selection reads small coverage metadata, not snow/age/property rasters.
New checkpoint bundles record this coverage at export. Older bundles require only
their short date vectors and headers. Scientific workers still read the numerical
state once needed for replay and validate its compatibility.

## Configure retention

```json
{
  "config_type": "nrt_storage",
  "schema_version": 1,
  "archive_root": "/archive/spires/noaa20",
  "working_days": 90,
  "daily_days": 7,
  "monthly_months": 12,
  "success_grace_days": 7,
  "failed_grace_days": 14,
  "unresolved_warning_days": 30,
  "warning_bytes": {},
  "growth_warning_bytes": 0
}
```

```bash
spires-batch nrt storage-config storage.json --root /products/noaa20-nrt
```

`working_days` cannot be shorter than the 66-day numerical checkpoint. The archive
root cannot be changed after publication with a setting flip; move an established
archive as a separate verified storage migration. Byte limits are advisory alerts,
not permission to discard scientific data.

The policy always retains the current checkpoint. It also selects the latest
state per day within seven calendar days, the latest state per month within twelve
months, and every available September 30 annual checkpoint. Coincident selections
share one recovery copy. Recent daily checkpoints remain hot; monthly/annual
checkpoints move to the archive. If no September 30 checkpoint exists, the policy
does not fabricate one. Unresolved inputs remain protected without a hard age
expiration, and overdue records generate alerts.

All published first/final products and subsequent published corrections, native
and prepared observations, and scientific provenance remain in the archive. This
policy bounds working storage; it deliberately does not cap the scientific archive.

## Plan and apply cleanup

```bash
spires-batch nrt retention-plan --root /products/noaa20-nrt --output retention.json
# Review candidate paths, bytes, selected checkpoints and workspace pins.
spires-batch nrt retention-apply retention.json
spires-batch nrt storage-report --root /products/noaa20-nrt
```

Planning does not delete files. Application verifies the report digest, current
publication, policy, workspace pins and every candidate file's stat identity.
Changes require a fresh report. Shared storage locks coordinate cycles, planning,
workers, publication and cleanup. Any planned/running workspace defers cleanup;
individual worker tasks also hold a read lock while accessing state.

Before unlinking anything, cleanup archives the retiring publication and selected
recovery state, plus workspace JSON receipts/requests and referenced retrieval R0
assets. Only unreferenced files in registered, owned workspaces and managed
checkpoint/cache locations can be removed. Nonempty unregistered directories,
symlinks and overlapping archive/workspace layouts are rejected. Successful
workspaces receive seven days of grace; failed or explicitly abandoned workspaces
receive fourteen. Unrecognized or unmigrated legacy generations remain protected.

Deletion is an explicit file list, not recursive removal of arbitrary directory
trees. A journal records completion and exact retries. If deletion is interrupted,
create a fresh report; the completed scientific archive remains intact. Hard-link
removal does not necessarily reclaim the logical byte count immediately, so the
storage report deduplicates physical allocation by device/inode. Reports separate
storage categories and flag configured limits, unexpected growth and overdue
unresolved inputs. Run the scan as maintenance, not in the daily numerical path.

A queued plan remains pinned until completed or explicitly abandoned:

```bash
spires-batch nrt abandon --root /products/noaa20-nrt /work/abandoned-cycle
```

Confirm that the scheduler will not later execute an abandoned plan. This command
releases its persistent pin; it does not cancel scheduler jobs or delete files.

## Storage migration, archive queries and rebuilds

An uncompacted storage ledger can be archived and sharded without numerical
recomputation only when its scientific context and checkpoint format already
match the current baseline. These storage-layout identifiers are independent of
the public configuration version. This is catalog maintenance, not support for a
retired interpolation algorithm:

```bash
spires-batch nrt migrate --root /products/noaa20-nrt --seed-plan historical-plan.json
```

The original plan is a provenance reference, not a compatibility override. A
stream lacking scientific context metadata or using an obsolete checkpoint
format must be bootstrapped under the new scientific contract. Historical plans
from preserved runtimes retain their exact source snapshots. If recorded code
has already been overwritten, the archive records its expected hash as unavailable
rather than mislabeling newer code as that implementation. Frozen runtimes are
historical provenance, not templates for new production jobs.

Use catalog queries to locate older products or inspect an as-issued publication;
the working ledger alone intentionally no longer enumerates all historical dates:

```bash
spires-batch nrt catalog --root /products/noaa20-nrt \
  --start 2026-08-01 --stop 2026-09-30 --output september.json
spires-batch nrt catalog --root /products/noaa20-nrt \
  --publication generation-EXAMPLE --output as-issued.json
```

A complete history rebuild reads archived prepared observations and plans all
scientific stages using the deployed numerical implementation. It does not
repeat native inversion or resample previous-year observations:

```bash
spires-batch nrt rebuild --root /products/noaa20-nrt --output /work/rebuild
spires-batch nrt run /work/rebuild/update.json
```

Missing/changed static asset bindings can be restored to their verified archive
copies for a full rebuild. For a shorter replay from a retained recovery point:

```bash
spires-batch nrt rebuild --root /products/noaa20-nrt \
  --checkpoint generation-EXAMPLE --output /work/recovery
spires-batch nrt run /work/recovery/update.json
```

The selected checkpoint must have compatible science, adequate donor history,
and an unchanged source prefix at the restart date. Replay normally starts
immediately after that checkpoint. If its last observations were subsequently
replaced by standard data, the controller first rewinds within its retained tail
to before those changes, including the seven-day final-issue context. It rejects
that checkpoint if the rewind lacks complete donor history. Replay runs through
current availability. Ordinary late-replacement planning can
also select such an earlier checkpoint automatically. If no retained checkpoint
is suitable, unattended planning stops instead of falling back to full history.

## Unattended full-replay stop

An ordinary NRT revision never automatically changes to full-history replay.
If no compatible retained checkpoint covers the replay interval, it exits with
status **3** before making the scientific full-history plan or copying full-history
payloads. This applies to standalone planning, full cycles and universal workflow
jobs. Older automatic full-history update plans are also rejected by `nrt run`.
Normal bounded checkpoint replay and explicitly requested historical WY jobs
remain available. The existing manual `nrt rebuild` operation remains explicit;
it is not invoked automatically and no approval/resume CLI is introduced.

The message includes the reason, date range, unique prepared-history file count,
known logical bytes, estimated uncached bytes, largest file, and durable report
path. The JSON report includes every unique file and any unknown sizes. It is
written to both the revision workspace (`full-replay-required.json`) and
`archive_root/stops/`. Reports read monthly catalog metadata and file stats only,
not historical raster arrays. Estimates cover prepared observation files, not a
promise of exact wire traffic or static-asset costs. Already cached files are
identified by the existing cache layout and stat identity; unknown sizes are
never represented as an exact zero-cost estimate.

Cron and Slurm jobs receive a nonzero exit and a clear stderr explanation; they
do not wait for input or submit recovery work. No successful workflow receipt is
written, and the last published generation remains unchanged. The normal cycle
may already have acquired/prepared changed observations before it reaches this
guard; the stop prevents the subsequent full-history operation. There is no
background full-history rsync or strict transfer-byte ceiling. Review the report
and decide on recovery manually if this exceptional situation occurs.

Both rebuild commands prepare reviewable plans; they do not submit jobs or change
cron. Run them in appropriately sized allocations. Execution preserves the atomic
expected-parent publication check. Rebuilding prepared observations themselves
requires the archived native data, dated R0/masks, model/configuration and matching
science/cloud environments; use the acquisition/retrieval workflow for that distinct
operation. Retention preserves those inputs and recipes rather than substituting
newer annual assets.
