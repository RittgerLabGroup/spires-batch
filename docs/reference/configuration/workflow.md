# Workflow configuration rules

Use the [complete example](../../../examples/workflow.md) for the operator sequence.
These rules describe how the submitted JSON is interpreted.

## Configuration controls

- `variables`: literal string values substituted for `${name}` in configuration
  values, including referenced requests. No shell evaluation, recursive
  substitution or environment-variable expansion is performed.
- `selection`: shared tile/year/date selection for batch jobs; historical and
  interpolation domains must agree with explicitly selected tiles.
- `selected_jobs`: choose a subset; empty selects all.
- `build_prerequisites`: explicitly include that subset's dependencies. Otherwise
  omitted prerequisites are an error, not silently treated as complete.
- `request`: an inline object or JSON filename. Inline paths use the workflow
  file's directory; external request paths use their own directory.
- `log_root`: optional common log base; each workflow uses an identity-named
  subdirectory. Defaults to `output/logs`.
- `max_auto_retry_count`: additional attempts after recognized transient
  scheduler failures; defaults to 3. Per-job overrides and the managed batch
  setting are described in [Logs and retries](../../logging-and-retries.md).

The public configuration baseline remains `schema_version: 1`. Saved workflow
plans use a separate format identifier. Regenerate previously saved workflow
plans to use the current logging and retry controller; existing submitted runs
are not upgraded in place.
