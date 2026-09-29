# H05/H06 parcial: recibo shadow de schemas preparados y binding PDF

## Diferencia reproducida

El binding PDF estabiliza ejecutor y contrato desde la captura de una llamada.
No cubría el intervalo anterior: el modelo puede recibir el schema con
`pdf_find_section.limit.default = 8`, cambiar la definición local a `3`, y la
llamada posterior capturar `3`. La reproducción local usa schemas y binding
reales, sin PDF, red ni modelos. El catálogo exportado al importar conserva `8`;
la captura posterior ya refleja `3`.

## Incremento implementado

`tool_schema_receipts.capture_candidate` captura un recibo inmutable por candidato
y ronda, después de `slim_tool_schemas`, en la fábrica de peticiones del loop.
Guarda únicamente hashes y nombres; no retiene descripciones, schemas completos,
argumentos, rutas, owner, URL ni credenciales. El hash del catálogo identifica el
conjunto/lista preparado. Cada definición tiene hash exacto y firma estructural
que excluye anotaciones `description`, conservando tipos, required, límites,
defaults y propiedades que se llamen literalmente `description`.

El binding PDF añade esas mismas identidades del contrato capturado por llamada.
Tras recibir su resultado, el loop compara con el recibo del `candidate_index`
que realmente respondió. No sustituye por el candidato primario si falta el
recibo del fallback y no reconstruye retrospectivamente qué se ofreció.

Estados de `schema_receipt_observation`:

- `match`: definición y firma coinciden.
- `compatible_projection`: difiere la definición, coincide la firma estructural
  sin descripciones; por ejemplo, prose acortada por slimming.
- `mismatch`: firma estructural diferente. Observación, no prueba de cambio de
  efecto ni rechazo automático.
- `not_advertised`: llamada no encontrada en ese anuncio nativo preparado;
  no declara que esté prohibida, pues existen fences y otras superficies.
- `not_comparable`: sin recibo, superficie textual/sin schema nativo, nombre
  duplicado o binding sin identidad disponible.

El recibo siempre identifica la etapa **`candidate_prepared`** y `shadow=true`.
`llm_core` puede adaptar schemas al protocolo después: no se presenta como hash
de los bytes recibidos por el proveedor. Cambiar el orden de listas del schema
puede cambiar la firma; no es un comprobador de equivalencia lógica JSON Schema.

## Autoridad y alcance

No cambia autorización, selección, despacho, parser ni permisos. Un mismatch no
bloquea ni permite la llamada. La revocación vigente sigue por encima del binding.
Errores de captura/comparación se omiten como observaciones; una identidad shadow
indisponible no convierte un resultado de herramienta correcto en fallo.

Piloto únicamente para resultados que llevan `pdf_call_contract`. No garantiza
versionado de todo el catálogo, snapshot atómico de owner/política, implementación
de handler, MCP, herramientas Code Mode ni anuncios textuales. Un handler cambiado
con el mismo contrato puede conservar los mismos hashes. No crea nuevo endpoint
histórico ni índice durable: la observación se propaga al SSE tool_output y al
tool_event de los metrics destinados a persistencia. Solo se copia el mapa
producido localmente; identidades de binding que no sean hashes SHA-256 válidos
no se exportan. No se añade el recibo al texto del prompt.

## Validación

Regresiones: default anunciado 8 → binding 3; captura sin alias mutable; slimming
compatible; propiedad/default denominados `description` preservados; candidato
fallback correcto y recibo ausente; textual/no anunciado/nombre duplicado; fallo
del observador. Dispatcher real con receptor sintético conserva resultado exitoso
a pesar del mismatch. Loop real con streaming sintético prepara candidato 0 a 8,
candidato 1 a 3, recibe fallback 1 y compara con el recibo correcto. La misma observación aparece
en tool_output y en metadata tool_events emitida; no se afirmó persistencia DB
independiente de la ruta existente.

Si el estado activo arrastra un recibo de una ronda anterior o de otro candidato,
la comparación devuelve `stale_candidate_receipt` y no atribuye una coincidencia
falsa. La comprobación usa ronda e índice capturados al preparar el candidato.

Suite: `venv/Scripts/python.exe -m pytest tests/test_tool_schema_receipts.py
tests/test_pdf_call_binding.py tests/test_foreground_model_routing.py
tests/test_llm_core_fallback.py -q`.

Resultado: **198 pruebas pasadas** en la suite amplia (105,37 s). Tras añadir
propagación SSE/metadata y validación de hashes, se repitieron las suites
`test_tool_schema_receipts.py` + `test_pdf_call_binding.py` +
`test_pdf_tool_contracts.py`: **56 pasadas en 4,09 s** sobre el código final,
incluida la regresión de ronda obsoleta.
`git diff --check` limpio.

## Fuentes y estado

[Análisis H05/H06 fijado](CODEX_HARNESS_ANALISIS_2026-09-29.md),
[binding PDF previo](CODEX_H05_PDF_CALL_BINDING_2026-09-29.md),
[Codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia; se consultaron referencias locales ya revisadas, sin
repetir upstream ni el radar de proyectos. H05/H06 permanecen parciales.
