# Documentation

Start with the operator guides below. Examples contain commands to follow;
reference pages explain individual commands and fields.

## Learn and run

1. [Overview](overview.md): what each repository does and how data move through processing.
2. [Environment setup](setup.md): install and verify the science and cloud runtimes.
3. [Paths and assets](paths-and-assets.md): shared inputs and settings each operator changes.
4. [Complete workflow](../examples/workflow.md): one JSON, review, submit, monitor, then continue into NRT.
5. [Routine NRT operations](nrt-operations.md): daily checks and water-year transitions.
6. [Logs and retries](logging-and-retries.md): choose the log directory, read summaries and understand automatic recovery.
7. [Troubleshooting](troubleshooting.md): diagnose failures without risking existing history.

For a single task, choose its [configuration and walkthrough](../examples/README.md).
These are alternatives to running the whole chain, not additional submissions
after starting a unified workflow.

## Look up details

- [CLI commands](reference/cli.md)
- [Configuration fields](reference/configuration/README.md) and [reading the schemas](reference/configuration/reading.md)
- [Storage and history](reference/storage.md)
- [Initialization details](reference/initialization.md)
- [Interpolation details](reference/interpolation.md)
- [NRT revision details](reference/nrt.md)
- [Advanced batch submissions](reference/batch.md)

## Maintain the software

- [Execution architecture](maintainers/architecture.md)
- [Release management](maintainers/release.md)
- [Documentation maintenance](maintainers/documentation.md)
- [Environment validation, November 5, 2026](maintainers/validation/environment-2026-11-05.md)
- [Five-tile campaign observations, November 6, 2026](maintainers/validation/campaign-2026-11-06.md)
- [Training candidate installation, November 8, 2026](maintainers/validation/training-candidate-2026-11-08.md)

Dated reports describe checks at that time. They are not setup instructions,
live job status, or approval of later changes.
