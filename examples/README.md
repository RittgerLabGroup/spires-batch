# Configurations and walkthroughs

Start with [the complete workflow](workflow.md). It uses one self-contained
[workflow.json](workflow.json) to initialize assets, build R0, process historical
observations, interpolate, register history, and continue into NRT.

First complete [environment setup](../docs/setup.md) and review
[paths and assets](../docs/paths-and-assets.md). Examples contain placeholders:
they are not ready to submit unchanged.

## One coherent example set

All scientific examples use NOAA-20, WY2026, and the same five tiles:
h08v04, h08v05, h09v04, h09v05 and h10v04. Historical processing covers
October 1, 2025–May 11, 2026; May 12–18 are held out for NRT continuation.
This is real historical coverage, **not a complete WY or a synthetic history**.
For another WY/domain, update the dates, annual assets, R0 bindings and destinations
together. Do not drop neighboring tiles merely to reproduce a smaller example.

| Configuration | Accompanying walkthrough | Use |
| --- | --- | --- |
| [workflow.json](workflow.json) | [Complete workflow](workflow.md) | Recommended end-to-end operator/training path |
| [initialization.json](initialization.json) | [Initialize](initialization.md) | Register directories and stage existing assets |
| [r0.json](r0.json) | [Build R0](r0.md) | Build reusable background reflectance |
| [historical.json](historical.json) | [Historical processing](historical.md) | Short interval or full WY, with canopy correction and albedo but no interpolation |
| [interpolation.json](interpolation.json) | [Interpolation](interpolation.md) | Fill gaps in completed daily observations |
| [cycle.json](cycle.json) | [NRT update](cycle.md) | Continue an already registered stream |
| [storage.json](storage.json) | [Storage setup](storage.md) | Set durable archival before registration |
| [scheduler_policy.json](scheduler_policy.json) | [Resources and queues](scheduler_policy.md) | Choose resources independently from submission queue |

Each walkthrough gives prerequisites, fields to edit, commands in order, and
checks before continuing. Individual tasks are **alternatives**, not extra steps
to run after submitting `workflow.json`. Never submit both routes against the
same output paths.

Copy only `workflow.json` for the complete chain. For standalone tasks, copy the
task JSON and `scheduler_policy.json` together; copy the other task files when
following their handoffs. Use actual per-user paths in standalone JSON.
Only the unified workflow supports its declared `${name}` variables.

## Look up options

The [CLI reference](../docs/reference/cli.md) describes command effects.
The [configuration reference](../docs/reference/configuration/README.md) explains
fields; its complete generated JSON lives under `docs/reference/` and is
documentation, not a submission file.

Maintainers generate `workflow.json` from these task files and the shared policy.
An operator's copied JSON remains independent; it has no runtime dependency on
the example source files. See [documentation maintenance](../docs/maintainers/documentation.md).
