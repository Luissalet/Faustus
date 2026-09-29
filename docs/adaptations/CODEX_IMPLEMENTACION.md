# Adaptaciones del harness de Codex: implementación

Plan original: [CODEX_HARNESS_ANALISIS_2026-09-29.md](CODEX_HARNESS_ANALISIS_2026-09-29.md). Referencia fijada: [openai/codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9). Las implementaciones son propias y parciales salvo indicación contraria; no se ha ejecutado el producto Codex como benchmark.

## H01: declaración verificable del runtime host

El mínimo revisable está implementado: las descripciones nativas, catálogo y ajustes dicen que Code Mode ejecuta Python en el host con acceso a archivos/red. `tools.call` conserva su puerta de políticas, pero Python directo no la atraviesa. La herramienta ya estaba clasificada como `EXECUTE_CODE` y desactivada por defecto; se conserva esa clasificación.

Cada resultado del runner incluye `runtime_guarantees`: `host_process`, filesystem/network no aislados y alcance de políticas `tools.call_only`. Se incluye también al fallar/terminar por cuota. No se convierte `python -I`, el entorno mínimo ni el directorio temporal en una supuesta frontera del sistema operativo. La descripción de la pausa de aprobación refleja que depende del ajuste existente.

Prueba decisiva: el runner real escribe un señuelo temporal fuera del cwd del guest sin usar herramientas; el resultado declara correctamente acceso host. También se comprueba la declaración en un error real de Python. Pasaron 16 pruebas de runner, paridad de políticas, cuotas y aprobación.

**Pendiente de H01/H02:** aislamiento de filesystem/red efectivo y comprobaciones de procesos hijos. Este commit no proporciona confinamiento. No se modifica la configuración personal ni se habilita Code Mode.

## Estado del backlog

| ID | Estado |
|---|---|
| H01 | `664894a5`: autoridad host explícita, 16 pruebas; confinamiento pendiente |
| H02 | `5f781023`: política solicitada/efectiva. `4fe0d52e`: salida Docker ambigua conserva incertidumbre. `5ad626b9`: modo opt-in required rechaza fallback host para bash/python/powershell, política capturada durante despacho; 124 pruebas y 5 Docker omitidas. Backend Windows y probes reales pendientes |
| H03 | `fc435eae`: intención sincronizada antes del correo con call_id y run activo, aliases MCP incluidos; 64 pruebas ampliadas y 29 finales. Outbox independiente, rutas no trazadas e idempotencia externa pendientes |
| H04 | `0572e1af`: 55 pruebas de normalización→evento→reinicio. `a40b8bcb`: productores propagan timeout real, 71 pruebas y 7 omitidas; exit 124 voluntario no implica timeout. `1b2537c9`: Studio conserva parcial/desconocido en directo e historial, 33 pruebas, tipos/build y render aislado correctos. Errores genéricos e identidad de intentos pendientes |
| H05 | `7c558eee`: round-trip de 220 herramientas y MCP; `155fad3d`: errores numéricos PDF; `28b2c2b0`: schemas independientes entre origen/snapshot/exportación. `3bfc22e2`: contrato único argumentos/schema/parser PDF, 122 pruebas. Captura ejecutor/contrato por llamada en curso; autoridad común general pendiente |
| H08 | `b7089ff5`: compactación de progreso por call_id conserva llamadas distintas, 52 pruebas y recuperación de traza solo desde disco. Ledger independiente, retención entre runs e identidad de subagentes pendientes |
| H10 | `6127bd24`: fuentes externas marcadas no se promueven a restricciones/objetivo preservado. `ebf7a856`: persistencia inmediata/diferida con SQLite y tres compactaciones/reaperturas, 99 pruebas. Checkpoint portable y actualización de menciones históricas de aprobaciones pendientes |
| H15 | `30e71fc8`: comentarios históricos de wiring y bridge corregidos contra callers actuales; AST sin docstring idéntico. Alcance de dos módulos completado |
| H16 | `303d4f63`: estados inciertos y llamadas anunciadas sin resultado no verifican progreso/fuentes; 191 pruebas finales. `2d32937a`: Enseñame registra éxito normalizado, 41 pruebas. No unifica aún toda verificación de workers/Code Mode |
| H19 | `d7600e54`: steering sin run/sesión resoluble no se difunde a todos los workers; 61 pruebas. Recibos durables/recuperación pendientes |
| H06–H07, H09, H11–H14, H17–H18, H20–H24 | Pendientes conforme al análisis original; no declarar cerrados por este documento |

Los pilotos de reinicio de procesos (`403756a9`) y memoria (`9e0e8e50`) ya aportan evidencia a H11/H20/H24, pero no completan esos cambios transversales.

Informes de entregables: [H02](CODEX_H02_POLICY_METADATA_2026-09-29.md), [H04](CODEX_H04_EFFECT_RESULTS.md), [H05](CODEX_H05_RUNTIME_ROUNDTRIP_2026-09-29.md). Cada uno conserva referencia original, pruebas y límites. No repetir estas inspecciones salvo regresión o pregunta nueva.

Revisión cruzada: [incertidumbre Docker](CODEX_H02_DISPATCH_UNCERTAINTY_2026-09-29.md), [intención de correo](CODEX_H03_DURABLE_EMAIL_INTENT.md) y [timeouts de comandos](CODEX_H04_COMMAND_TIMEOUTS_2026-09-29.md). Los receptores de correo son sintéticos; no se enviaron correos reales. La salida Docker 126 se probó con un doble que produce un efecto antes de devolverla; falta el daemon para verificar el confinamiento real.

[Presentación Studio](CODEX_H04_UI_OUTCOMES.md): estados inciertos y parciales tienen etiqueta explícita y aviso, también tras restaurar historial y completar aprobaciones. Render de ToolRail con fixtures sin backend a anchura de escritorio y contenedor de 390 px; no equivale a un recorrido completo con SSE real.

Nuevos informes: [modo required](CODEX_H02_REQUIRED_MODE_2026-09-29.md),
[aislamiento de schemas](CODEX_H05_SCHEMA_SNAPSHOT_ISOLATION.md),
[identidad en replay](CODEX_H08_REPLAY_CALL_IDENTITY.md) y
[comentarios actuales](CODEX_H15_CURRENT_WIRING.md). Ningún cambio activa opciones
en la configuración personal. `required` cubre las herramientas de comandos
indicadas, no confina Code Mode ni las demás herramientas host.

## Comprobación integrada

Selección conjunta de 19 módulos de pruebas: H01/Code Mode, H02/sandbox,
H03/intención, H04/normalización/presentación, H05/catálogo, H08/replay y
persistencia/observabilidad de runs. Resultado: **167 correctas, 5 omitidas por
Docker, 19,92 s**. No equivale a la suite completa ni a ejecución con un LLM real.
