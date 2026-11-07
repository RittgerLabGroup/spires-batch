# Read the complete configuration reference

Start with the [Markdown field guides](README.md) for
readable descriptions and searchable field headings. For the consolidated schemas,
open `docs/reference/config_reference.json` in your editor. It is a JSON docstring,
not an executable configuration. JSON does not support comments, so explicit
`description`, `type`, `required`, `default`, enum and constraint entries explain
each option. Do not copy those documentation wrappers into a submission JSON.

## One release baseline

All standalone spires-batch configurations require both `config_type` and the
integer `schema_version: 1`. The type identifies the interface, not an algorithm:
`batch_request` (including R0), `historical`, `interpolation`, `workflow`,
`initialization`, `nrt_cycle`, `nrt_storage`, or `scheduler_policy`. Advanced
workflow requests also use `acquisition` and `prepared_observation`.

Version 1 is the first public spipy release contract, not the retired development
interpolation. Missing headers, wrong types and development version 2 are rejected;
there are no aliases, implicit upgrades or selectable old scientific pipelines.
The improved in-memory R0, observation-pointer-matched VIIRS inputs, selected
canopy adjustment and evaluated interpolation together define the baseline.
A future feature may deliberately introduce a new public configuration version;
it need not change in lockstep with a package patch release.

Nested recipe fragments (for example `historical.preparation`, a tile's retrieval
assets, or inline scheduler resources) are not independent configurations and do
not receive these headers. A referenced scheduler-policy file does. Independent
cloud-model manifests and earthdata-downloads requests retain their own package
contracts; their version numbers are not spipy algorithm switches.

Immutable plans, observation/checkpoint files and scientific recipe component
versions describe different contracts. Do not relabel stored files to match the
public JSON number. No archived artifacts or running environments are rewritten.
Registration of already computed **current-science** history verifies its saved
scientific context, then emits baseline configuration metadata for continuation;
it does not accept retired algorithms or rewrite the original plan/checkpoints.

## Reference contents

The top-level sections are:

- `schemas.workflow`: baseline universal graph, jobs and allocation resources.
- `schemas.daily_request`: every declared baseline `batch_request` field and nested
  definition, directly generated from the maintained Pydantic models.
- `schemas.initialization`: tile/domain layout, storage and asset registration.
- `schemas.historical`: matched preparation, daily albedo and date-array resources.
- `daily_request_guidance`: the detailed batch-request reference, including
  named inputs, cross-field constraints, adapters and resource profiles.
- `interfaces`: the separately parsed interpolation, retained preparation,
  native retrieval, NRT cycle/acquisition, storage, scheduler policy, mapped-field
  and external historical acquisition dictionaries.
- `fixed_scientific_recipe_not_user_options`: actual evaluated interpolation
  settings for orientation, explicitly not additional JSON tuning options.

Within a JSON schema, follow `$ref` into that schema's own `$defs` map. The
`required` array lists mandatory properties. Absence of a `default` does not
necessarily mean a field is required: some fields are populated by default
factories or contextual validators. Read the cross-field guidance as well.
Omitting a field, setting it to null and passing an empty object can mean
different things. Generated plans, receipts and checkpoint internals are not
alternative user configuration fields.

The arbitrary dictionaries currently used by some controllers are not fully
described by `workflow schema` alone. Their consumed fields are documented in the
[processing interface guide](interfaces.md); the reference explicitly identifies this
validation boundary rather than promising unknown-key rejection everywhere.

For regeneration instructions and source locations, see
[documentation maintenance](../../maintainers/documentation.md).
The [examples](../../../examples/README.md) provide one coherent set of standalone
tasks and the generated, self-contained workflow.
