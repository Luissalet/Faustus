# Language of the Studio health panel

Studio translates the health registry by stable component name. Labels, known
value templates and service/degradation templates use the selected interface
language and its number format. Changing the language recomputes the display
from the original readings immediately, including cached usage.

The REST and MCP responses retain their original English fields. The client
does not alter cached readings, severity, missing measurements, recovery
commands, timestamps or server identifiers. Unknown diagnostic text remains
literal so a new or provider-specific error is still available to inspect.

The implementation is `studio/src/lib/vitals-i18n.ts`, consumed by the usage,
services and degradation hooks. Vitals itself keeps its existing layout and
behaviour. `tests/test_vitals_i18n.py` compares the label map to every backend
`DEFAULT_COMPONENTS` entry and executes the client contracts: locale,
singular/plural, unknown data, immutable originals and language switching.
