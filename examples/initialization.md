# Initialize a tile domain

Use [initialization.json](initialization.json) when preparing directories and
assets separately. The unified workflow already performs this step.

## 1. Review the existing assets and paths

Set the domain, tiles, processing WYs, durable root and scratch root. The example
lists existing LUTs, cloud models and ancillary files, but not the new R0 files
that the next step will build. Required files must actually exist.
Reflectance remains in its shared archive, outside the assets tree.

Initialization creates a catalog and directories, not missing scientific data
or an NRT stream. See [paths and assets](../docs/paths-and-assets.md).

## 2. Preview

From the directory containing your copied configuration:

```bash
spires-batch tile-init initialization.json --dry-run
```

Read the missing-file list and transfer sizes. Confirm that only the intended
individual assets will be staged. Nothing is copied by this command.

## 3. Create and stage

With inputs available, run inside a suitable Blanca allocation for substantial I/O:

```bash
spires-batch tile-init initialization.json --stage-assets
```

This registers the layout and copies listed assets to scratch. Missing required
files stop staging. Check the receipt and bind its scratch locations in the
processing configurations. Initialization does not rewrite those bindings.

## 4. Continue

[Build R0](r0.md), or reuse a validated R0 and proceed to
[historical processing](historical.md). The [initialization reference](../docs/reference/initialization.md)
explains repeat runs and catalog rules.
