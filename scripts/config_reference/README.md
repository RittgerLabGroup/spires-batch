# Configuration documentation sources

These JSON files are internal build inputs for `../build_config_reference.py`,
not examples to submit or the operator-facing reference.

- `daily_request.json`: detailed batch-request field descriptions and rules.
- `annotations.json`: descriptions supplementing the typed configuration models.
- `interfaces.json`: manually documented dictionary interfaces and defaults.

Edit these when the corresponding options change, then regenerate from the
repository root in `spipy14` with `python scripts/build_config_reference.py`.
Run `python scripts/build_config_reference.py --check` to check both output formats.
The readable [Markdown reference](../../docs/reference/configuration/README.md) and
[complete JSON reference](../../docs/reference/config_reference.json) are generated
together, including fields and defaults inspected from the live implementation.
