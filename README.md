# spires-batch

Run spipy processing across tiles and dates using JSON configurations and
Slurm/Blanca. One workflow can initialize a tile domain, build R0, process
historical observations, interpolate snow properties, register completed history,
and continue with NRT updates. Successful checks trigger the next submissions.

## Start here

1. [Understand the repositories and processing stages](docs/overview.md).
2. [Install and verify your environments](docs/setup.md).
3. [Set your paths and check the shared assets](docs/paths-and-assets.md).
4. [Follow the complete workflow example](examples/workflow.md).

After preparing and reviewing your configuration, submit the chain:

```bash
spires-batch workflow submit workflow.json --output workflow-plan.json
spires-batch workflow status workflow-plan.json
```

This is a **live submission**, not a dry run. The walkthrough also shows how
to plan and review before submitting.

## Find something specific

- [Documentation index](docs/README.md)
- [Examples and individual-task walkthroughs](examples/README.md)
- [CLI reference](docs/reference/cli.md)
- [Configuration fields](docs/reference/configuration/README.md)
- [Routine NRT operations](docs/nrt-operations.md)
- [Troubleshooting](docs/troubleshooting.md)

This is an unreleased spipy v1.0.0 candidate. The unified launcher still needs
a deployed end-to-end acceptance pilot. Installing code or completing jobs
does not replace scientific review. See [release management](docs/maintainers/release.md)
and the dated validation reports for the scope of existing checks.
