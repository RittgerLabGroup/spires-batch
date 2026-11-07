# Configuration field reference

These pages explain options; they are **not configurations to submit**. Start with
the [runnable templates](../../../examples/README.md) or [training sequence](../../../examples/workflow.md),
then look up an unfamiliar field here. Use your editor's search or the headings
in each page to find an exact configuration key.

| Guide | What to find there |
| --- | --- |
| [Standard resource requests](resources.md) | Current per-job CPU, memory, time and queue defaults, with their measurement evidence. Generated from the shared scheduler policy. |
| [Batch-request fields](daily_request.md) | R0 and advanced batch requests: input files, tile/date selection, scientific settings, resource profiles and rules between fields. |
| [Processing interfaces](interfaces.md) | Interpolation, NRT cycles, storage, acquisition, prepared observations and scheduler settings. Each interface has its own section. |
| [Workflow rules](workflow.md) | Variables, selection and dependency rules for the unified configuration. |
| [Field glossary](annotations.md) | Short definitions of fields used by the typed configuration schemas. |
| [Complete JSON reference](../config_reference.json) | All generated schemas, including historical processing, initialization and named workflows, plus the interface guidance and fixed scientific settings. |

Field names and allowed values are literal. Descriptions, defaults and required
flags explain a field; do not copy those documentation wrappers into your JSON.
An omitted value, `null` and an empty object are not always equivalent. Refer to
the complete schema and cross-field rules as well as the individual description.
The glossary alone does not specify every constraint or valid nesting.

See the [CLI guide](../cli.md) for commands and their side effects, and
[per-user paths](../../paths-and-assets.md) for settings to change when
copying an example.

## Maintaining these pages

See [documentation maintenance](../../maintainers/documentation.md) for authored
sources, generation commands and validation. Do not edit generated pages directly.
