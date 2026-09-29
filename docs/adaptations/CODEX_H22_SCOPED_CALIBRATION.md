# H22 parcial — observaciones por endpoint y protocolo

Fecha: 2026-09-30. Adaptación local del [backlog H22](CODEX_HARNESS_ANALISIS_2026-09-29.md),
con fuente original fijada [Codex b1e72963c3b71a9265a551e54beff078384efed9](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
No se repite la revisión upstream ni se ejecutan modelos o servicios externos.

## Problema y contrato

El manifiesto legacy de Ollama podía compartir observaciones booleanas entre
endpoints con el mismo digest. Su clave identifica el artefacto, pero no prueba
que otro endpoint o protocolo admita la misma capacidad.

Las APIs aditivas `calibration_key`, `save_scoped_tested` y
`get_effective_manifest` separan vendor, endpoint_id, protocolo y digest/model_id.
La clave usa JSON canónico y SHA256; la procedencia `calibration_scope` se guarda
atómicamente con los resultados y debe coincidir exactamente al recuperar.
Ollama puede reutilizar evidencia entre tags del mismo digest exclusivamente
dentro del mismo endpoint y protocolo. Sin digest se usa model_id.

`get_manifest`, `manifest_key` y los bools legacy permanecen intactos como
historial compatible. El lector efectivo no migra ni eleva esos bools a verified.
Puede recuperar sus anuncios como declaraciones, identificando su origen en
`announcement_manifest_key`. Cuando existe un registro scoped exacto, sus
anuncios actuales prevalecen incluso si son deliberadamente vacíos. Los intentos
omitidos conservan el contrato de [H22 skipped probes](CODEX_H22_SKIPPED_PROBES.md).

## Integración del piloto

El calibrador nativo escribe observaciones bajo el endpoint real y el protocolo
`ollama_native_api_chat`, correspondiente a las peticiones nativas que ejecuta.
El endpoint de capabilities lee ese contexto nativo. `stable_model_id` conserva
su valor legacy; `calibration_key` identifica separadamente la calibración.

Fit-explain y el explorer seleccionan ese protocolo sólo para URLs explícitas
`/api` o `/api/chat`. Una raíz o `/v1` deja el protocolo desconocido y sólo puede
mostrar declaraciones. No se deduce transporte de posibles reroutes internos.
Fit-explain recupera el digest desde `/api/tags` también cuando la URL configurada
termina en `/api/chat`, sin duplicar `/api`.

## Validación y límites

Pruebas con almacén temporal real, rutas y transporte simulado: aislamiento por
endpoint/protocolo/digest, ausencia de contexto, legado intacto, procedencia
obligatoria, aliases del mismo digest, strings de endpoint sin colisiones por
sanitización, anuncios exactos actuales y vacíos, explorer y calibrador nativo.
Fit-explain conserva sus asserts de capacidad faltante y alternativa; una prueba
separada verifica que `/v1` no convierte evidencia legacy en tested.

Suite conjunta: 156 pruebas (21 nuevas de scope), sin modelos ni requests externos.

Piloto parcial: router offline, provider_policy y cambios de modelo/sesión que
todavía leen APIs legacy no migran aquí. La identidad usa endpoint_id, no una
huella de URL/configuración ni versión del servidor. Reutilizar ese ID después
de cambiar su configuración puede conservar evidencia previa; invalidación por
configuración/versión y migración explícita de evidencia histórica quedan
pendientes. No se afirma equivalencia universal de deployments ni atomicidad
entre cambios de configuración del endpoint y lectura del almacén.
