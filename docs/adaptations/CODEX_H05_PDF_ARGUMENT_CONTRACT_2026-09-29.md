# H05 parcial: contrato único de argumentos para navegación PDF

Ámbito: `pdf_outline`, `pdf_read_section`, `pdf_find_section`. Implementación propia
inspirada en H05/H06 del [análisis local](CODEX_HARNESS_ANALISIS_2026-09-29.md) y la
[orquestación de herramientas de Codex](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/orchestrator.rs),
ya revisada; no se repitió la investigación upstream.

## Evidencia y cambio

Antes: `pdf_find_section(limit=True)` era rechazado por validación nativa, pero el
handler directo lo convertía a `1` y ejecutaba. `pdf_read_section(max_chars=0)` era
admitido por el schema y rechazado por el motor. El catálogo y la ejecución podían
describir contratos distintos.

`src/pdf_tool_contracts.py` concentra definiciones de argumentos, descripciones,
schemas, defaults y parser. No importa handlers, dispatcher ni motor PDF.
`tool_schemas.py` genera sus tres entradas desde allí y la validación nativa PDF
consulta el mismo contrato. Los tres handlers lo consumen antes de cualquier I/O.
Se retiraron los tres schemas duplicados del literal y las conversiones dispersas
de los handlers. El inventario de pruebas combina ahora el literal con la familia
generada sin importar el stack de embeddings.

## Compatibilidad explícita

- Se conservan objetos JSON, fences JSON, path textual de `pdf_outline`, defaults
  de `limit=8` y `max_chars=20000`, y rangos/páginas del motor PDF existente.
- Los enteros deben ser positivos. Números JSON integrales como `2.0` se normalizan
  a `2`, conforme al tipo `integer` de JSON Schema; fracciones e infinitos se rechazan.
- Se rechazan booleanos, strings numéricos (`"20"`), `null`, campos desconocidos,
  strings obligatorios vacíos, valores cero/negativos y JSON de objeto malformado.
  Algunos antes se convertían, truncaban, ignoraban o se trataban como un path.
  Es una corrección deliberada de la entrada, no compatibilidad silenciosa.
- El error mantiene `exit_code=1` y `error_class=pdf_tree.error` para argumentos
  malformados, preservando las pruebas anteriores de ese resultado.
- La reparación nativa anterior a la llamada puede seguir convirtiendo una forma
  reparable en argumentos válidos según sus reglas; el parser ejecutor no hace esas
  conversiones implícitas. El path JSON con claves explícitas sirve para nombres
  que empiezan por `{`, que podrían confundirse con un objeto JSON textual.

## Recorrido y pruebas

No se cambia `execute_tool_block` ni se eluden sus controles. Las pruebas nuevas
ejercen directo, llamada nativa convertida a ToolBlock y Code Mode por el dispatcher
real. Entradas inválidas se rechazan con backend PDF marcado como prohibido; un
PDF generado de una página y marcador comprueba outline → búsqueda → lectura en
las tres rutas. Code Mode continúa respetando herramientas deshabilitadas.

```text
venv/Scripts/python.exe -m pytest tests/test_pdf_tool_contracts.py tests/test_pdf_tree.py tests/test_pdf_tree_argument_errors.py tests/test_tool_index_schema_parity.py tests/test_objective_tool_schema.py tests/test_tool_registry.py tests/test_tool_registry_roundtrip.py -q
```

Resultado: **122 pruebas correctas**; incluye los recorridos nuevos y las pruebas
existentes de PDF, errores, inventario, catálogo y round-trip.

## Límites de H05/H06

Este commit unifica argumentos/schema/parser. El descriptor y el callable todavía
no se capturan juntos por llamada: ese es el siguiente incremento. Una captura por
llamada tampoco equivale a ligar el contrato al catálogo anunciado al modelo al
comienzo del paso; H06 continúa pendiente. Los controles de revocación existentes
siguen teniendo precedencia.

Las entradas generadas quedan al final del catálogo nativo: cambia su orden una
vez; no se afirma preservar hashes/prefijos previos. Los `30.000 ms` genéricos del
descriptor no se presentan como timeout PDF aplicado. No se añade límite duro de
tiempo ni se certifica confinamiento distinto al guard de rutas ya existente.
