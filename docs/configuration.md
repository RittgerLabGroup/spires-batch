# Reading and maintaining the complete configuration reference

Open `examples/config_template.json` in your editor. It is a JSON docstring,
not an executable configuration. JSON does not support comments, so explicit
`description`, `type`, `required`, `default`, enum and constraint entries explain
each option. Do not copy those documentation wrappers into a submission JSON.

The top-level sections are:

- `schemas.workflow`: universal schema-2 graph, jobs and allocation resources.
- `schemas.daily_request`: every declared schema-1 daily request field and nested
  definition, directly generated from the maintained Pydantic models.
- `schemas.initialization`: tile/domain layout, storage and asset registration.
- `daily_request_guidance`: the earlier detailed daily reference, including
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
described by `workflow schema` alone. Their consumed fields are authored under
`examples/reference/interfaces.json`; the reference explicitly identifies this
validation boundary rather than promising unknown-key rejection everywhere.

From the batch checkout in `spipy14`, with the sibling IO/interpolation sources:

```bash
python scripts/build_config_reference.py --check
python scripts/build_config_reference.py
```

The builder reads the live daily, initialization and workflow models, the VIIRS
preparation signature and the fixed scientific recipe. Every declared model
field must have a description. It checks prepared-field and storage-default
coverage and the interpolation planner's required/optional keys. New fields
require authored annotations; changed defaults/constraints
appear in the regenerated output. It does not fetch data, submit jobs or modify
runtime configuration. Review manually parsed interfaces when their controllers
change; no generated schema can infer all scientific cross-field constraints.

Keep human guidance in `examples/reference/daily_request.json`, field annotations
in `examples/reference/annotations.json`, and dictionary interfaces in
`examples/reference/interfaces.json`. Regenerate the main file rather than
hand-editing it. The examples under `examples/operations/h08v05-wy2026` are the
separate, much smaller campaign inputs.
