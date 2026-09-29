# H22 parcial — capacidades contextuales al cambiar de modelo

Fecha: 2026-09-30. Continuación del [piloto scoped](CODEX_H22_SCOPED_CALIBRATION.md),
con fuente original fijada [Codex b1e72963c3b71a9265a551e54beff078384efed9](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
No se repite la revisión upstream ni se ejecutan modelos o requests externos.

## Cambio y contrato

`agent_loop.recompute_capabilities_on_model_switch` y el bloque de cambio de
modelo en session PATCH leían manifiestos legacy completos. Sus probes podían
seguir apareciendo en `capabilities.tested` aunque no identificasen el protocolo
de la ruta nueva. Ambos seleccionan ahora el lector efectivo: observaciones sólo
del mismo endpoint, modelo/digest y protocolo nativo explícito; sin contexto
suficiente, únicamente anuncios legacy.

El helper acepta los kwargs opcionales `previous_digest` y `new_digest`, sólo
strings explícitos aportados por el caller. No obtiene digests desde tags ni
otros registros. Sin digest no adopta calibraciones guardadas bajo un blob; con
digest coincidente puede reutilizar tags dentro del ámbito exacto. Los callers
de fallback existentes no se amplían en este incremento.

PATCH conoce el endpoint nuevo cuando se selecciona por ID, pero la sesión
anterior sólo conserva URL/modelo: no se inventa un ID anterior. Para ese lado
se recuperan declaraciones globales, nunca probes de una conexión supuesta.
PATCH tampoco dispone de digest; una observación bajo digest permanece desconocida.
El protocolo se selecciona sólo para vendor Ollama y URL `/api` o `/api/chat`.

`lost` sigue comparando exclusivamente anuncios de vision/tools y conserva las
etiquetas y su orden. Un cambio de True a False en probes sin cambio de anuncios
no crea una pérdida. La respuesta `capabilities` conserva los cuatro campos
announced/tested/degraded/updated_at; los metadatos de selección quedan internos.
Una ruta nunca registrada conserva hints vacíos, sin añadir degradaciones por
suponer capacidades. No se modifican opciones, headers, atribución, políticas,
transporte, la lógica del bucle ni otras rutas de autocalibración.

## Validación y límites

20 pruebas nuevas con almacén temporal real y PATCH sobre SQLite temporal cubren
legacy True/False, ámbito nativo exacto, endpoint distinto o ausente, protocolo
desconocido, digest explícito/ausente/no string/incorrecto, aliases con digest,
lost basado sólo en anuncios, ausencia de origen previo y persistencia intacta
de modelo/URL/headers. Las suites existentes de MOD06/QA28 y session PATCH se
ejecutan sin adaptar fixtures.

La suite conjunta de switches y pilotos scoped/provider policy/router contiene
91 pruebas. Un primer paso detectó el hint fence añadido a una ruta sin registro;
se conservó el contrato empty anterior en ambos lectores de switches.

Persisten los límites del piloto scoped: endpoint_id sin huella de configuración
o versión del servidor y disponibilidad real de digest en callers. No se
migran ni borran manifiestos legacy. Otras rutas que escriben pruebas legacy no
obtienen autoridad scoped automáticamente.
