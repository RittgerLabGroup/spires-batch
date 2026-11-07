# Maintain the documentation and examples

## Content ownership

- Root README: short introduction and starting links.
- Operator guides in `docs/`: overview, setup, paths, routine operation and diagnosis.
- `examples/`: runnable JSON plus an annotated Markdown walkthrough for each task.
  `workflow.md` is the only complete teaching sequence.
- `docs/reference/`: command syntax, configuration fields and detailed behavior.
- `docs/maintainers/`: implementation/release information and dated evidence.

Do not create a second training/template set or another narrative source tree.
A dated validation report is not current setup guidance. Do not change frozen
campaign artifacts when updating examples.

## Generate the unified example

The standalone JSON files and shared scheduler policy are the maintained
configuration sources. `scripts/build_workflow_example.py` combines them into
the self-contained `examples/workflow.json`, declares its variables and adds job
dependencies. It enables asset staging in the initialization job and embeds the
storage configuration in history registration. It does not submit or compute.

From the batch checkout in `spipy14`:

```bash
python scripts/build_workflow_example.py
python scripts/build_workflow_example.py --check
```

Edit the task examples/policy first, then regenerate. Review agreement among
tile domains, date windows, R0 paths and annual bindings. Updating source examples
does not update operators' copied requests or existing plans.

## Generate configuration documentation

Descriptions live in the model fields and `scripts/config_reference/`.
Those internal JSON files are authoring inputs, not operator configurations.
The builder also checks selected fields/defaults against the current code:

```bash
python scripts/build_config_reference.py
python scripts/build_config_reference.py --check
```

This generates the Markdown field guides and `docs/reference/config_reference.json`.
Do not hand-edit generated pages. Read the
[reference limitations](../reference/configuration/reading.md) when changing
dictionary-based interfaces.

## Publish the same Markdown

Sphinx reads the repository root so that it can render both `docs/` and
`examples/` without copying them into a second source tree. Its configuration
and build requirements live under `docs/`. Read the Docs uses the same build.

With documentation dependencies installed in your development environment:

```bash
python -m pip install -r docs/requirements.txt
python -m sphinx -W -b html -c docs . docs/_build/html
```

The local equivalent is `make -C docs html SPHINXOPTS=-W`.
Check local links, example JSON headers, both generators' `--check` results,
and rendered navigation before merging. Building documentation does not require
installing scientific packages; generating field references does.

After moving files, update relative Markdown links, literal example paths,
generator output paths, the Read the Docs build, and the release checker.
The release checker accepts both documentation-requirements locations in pinned
component history; that is not a second live documentation tree.
