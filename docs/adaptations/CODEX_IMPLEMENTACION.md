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
| H05 | `7c558eee`: round-trip de 220 herramientas y MCP; `155fad3d`: errores numéricos PDF; `28b2c2b0`: schemas independientes entre origen/snapshot/exportación. `3bfc22e2`: contrato único argumentos/schema/parser PDF, 122 pruebas. `fdce5d9e`: captura ejecutor/contrato PDF por llamada con revocación vigente, 172 pruebas; autoridad común general y snapshot por paso pendientes |
| H06 | `0f8e6dc4`: recibo shadow del schema preparado por candidato, comparado con binding PDF y propagado a eventos; 56 pruebas finales. Ronda/candidato obsoletos no se comparan. Snapshot de paso y autoridad general pendientes |
| H08 | `b7089ff5`: compactación de progreso por call_id conserva llamadas distintas, 52 pruebas y recuperación de traza solo desde disco. `db99abd9`: archivos exclusivos por run, recuperación multirun con commit SQLite confirmado y purga/retención, 114 pruebas. Ledger independiente e identidad de subagentes pendientes |
| H09 | `1a418269`: reutilización exige ámbito/política/solicitud coincidentes, 53 pruebas; `b63f1e5b`: recibos de skills renderizadas/ensambladas y uso solo de cuerpos incluidos, 50 pruebas; versiones universales de fuentes y entrega efectiva pendientes |
| H10 | `6127bd24`: fuentes externas marcadas no se promueven a restricciones/objetivo preservado. `ebf7a856`: persistencia inmediata/diferida con SQLite y tres compactaciones/reaperturas, 99 pruebas. Checkpoint portable y actualización de menciones históricas de aprobaciones pendientes |
| H11 | `3338166d`: drenaje por bloques UTF-8, salida/progreso acotados y actividad sin LF; 91 pruebas y 3 omitidas. `6305ed06`: Code Mode drena stderr, 26 pruebas; `6b19a2fe`: frames grandes dentro de cuota y rechazo explícito del exceso, 32 pruebas. Manager/stdin/handles durables pendientes |
| H12 | `906ae3c7`: reparación de llamadas sin respuesta conserva contenido multimodal; 94 pruebas; `d466bf5a`: marcador legacy no borra imágenes, 128 pruebas. `466c01e3`: renderer Anthropic conserva bloques junto a tool_calls, 100 pruebas focales. Proyección canónica y recibos pendientes |
| H13 | `f22702d9`: función pura compara en shadow ampliación/ciclos/presupuesto sin gobernar el bucle, 75 pruebas y 432 combinaciones. `291907ed`: cierre CE distingue propuesta y continuación concedida al agotar cupo, 56 pruebas; controlador común pendiente |
| H14 | `ce4df06a`: retries de workers acumulan tokens por intento, sin duplicar métricas finales ni borrar campos ausentes; 49 pruebas. Presupuesto transversal pendiente |
| H15 | `30e71fc8`: comentarios históricos de wiring y bridge corregidos contra callers actuales; AST sin docstring idéntico. Alcance de dos módulos completado |
| H16 | `303d4f63`: estados inciertos y llamadas anunciadas sin resultado no verifican progreso/fuentes; 191 pruebas finales. `2d32937a`: Enseñame registra éxito normalizado, 41 pruebas. `dbdf0987`: worker conserva evidencia hasta SQLite/reapertura, 120 pruebas. `5de909f8`: Code Mode propaga incertidumbre interna con recibos acotados, 23 pruebas. Verificación transversal e incertidumbre de host directo pendientes |
| H19 | `d7600e54`: steering sin run/sesión resoluble no se difunde a todos los workers; 61 pruebas. Recibos durables/recuperación pendientes |
| H21 | `54fea979`: instrucciones se refrescan por contenido acotado, no mtime; 66 pruebas y 1 POSIX omitida. `b63f1e5b`: recibos L1 ensamblados. Jerarquía, snapshot aprobado atómico y procedencia completa/entrega de skills pendientes |
| H24 | `92c6ebee`: elimina contaminación global de imports en fixture de skills; mismo orden integrado 252 pruebas correctas tras timeout previo. Banco pareado con modelos y aislamiento general pendientes |
| H20 | `42a29971`: identidad de mutación compartida en deduplicación/conflictos; ámbitos y sesiones distintos no se mezclan, origen de sesión en proyecto permite consolidación. 30 pruebas finales de ámbito/vigencia/propietario. Leases y atomicidad concurrente pendientes |
| H07, H17–H18, H22–H23 | Pendientes conforme al análisis original; no declarar cerrados por este documento |

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


### Checkpoint posterior: 547 pruebas

Sobre `fdce5d9e`, selección integrada de 25 módulos: **547 correctas en 25,92 s**.
Incluye recuperación por run/SQLite, intención durable, incertidumbre, ámbito de
reutilización, compacción, evidencia del harness/Enseñame, contratos y binding PDF,
catálogo, steering, modo required y presentación. Sin LLM ni servicios externos;
no sustituye las pruebas Docker pendientes ni la suite completa.

Informes: [H08 por run](CODEX_H08_PER_RUN.md),
[H09 ámbito](CODEX_H09_LIVE_REUSE_SCOPE.md),
[H05 binding PDF](CODEX_H05_PDF_CALL_BINDING_2026-09-29.md).
H21 corregido en `54fea979`: caché por mtime podía retener instrucciones antiguas
tras TTL si cambiaban bytes conservando mtime. Jerarquía por directorio
y vinculación de confianza al contenido requieren alcance separado.

Informes posteriores: [H12 multimodal](CODEX_H12_MULTIMODAL_PROJECTION.md),
[H14 retries](CODEX_H14_RETRY_USAGE.md) y
[H21 instrucciones](CODEX_H21_INSTRUCTION_CACHE.md). Todos conservan limitaciones
y procedencia; el backlog transversal sigue abierto.


### Checkpoint posterior: procesos, retries e instrucciones

Selección integrada sobre los cambios hasta `d466bf5a`: **313 correctas y 4
omitidas, 21,37 s**. Cubre H11 procesos/ownership/cancelación/sandbox, H14
contabilidad de retries/SQLite, H21 instrucciones/trust y H12 saneamiento/compactor.
Omisiones por entorno POSIX/Docker en Windows; no se ejecutan modelos remotos.
H16 conservación de estados en workers es un incremento posterior en curso y
requiere su propia validación. [H11 detallado](CODEX_H11_STREAM_DRAIN.md).


Informes de continuidad/evidencia: [workers](CODEX_H16_WORKER_OUTCOMES.md),
[Code Mode](CODEX_H16_CODE_MODE_OUTCOMES.md),
[skills](CODEX_H09_SKILL_DISCLOSURE_RECEIPTS.md) y
[ampliaciones shadow](CODEX_H13_CONTINUATION_SHADOW.md). No se habilitan opciones
ni se convierte el observador en autoridad. La propuesta de continuación y la
continuación realmente concedida requieren distinguirse también en el cierre CE;
corregido de forma acotada en `291907ed`, sin conceder rondas adicionales.


### Checkpoint: contaminación de pruebas resuelta

La selección conjunta de skills, shadow, plan/cierre, workers y Code Mode falló
por contaminación de imports en una prueba histórica. El caso aislado pasó en
17,08 s; tras corregir la fixture en `92c6ebee`, **252 pruebas correctas en
65,69 s** en el mismo orden. No se cuenta el intento con timeout como éxito.
El piloto H06 se confirmó después en `0f8e6dc4`, con 56 pruebas del código final;
la selección amplia de 198 pruebas corresponde a su variante previa, como distingue el informe.

Informes: [aislamiento del banco](CODEX_H24_TEST_IMPORT_ISOLATION.md),
[cierre por cupo](CODEX_H13_COMPLETION_BUDGET_REASON.md),
[stderr Code Mode](CODEX_H11_CODE_MODE_STDERR.md),
[frames Code Mode](CODEX_H11_CODE_MODE_FRAMES.md).


[Recibos de schemas H06](CODEX_H06_SCHEMA_RECEIPTS.md): detección de discrepancias
y versiones del candidato preparado, sin ampliar la autoridad. No se reabre el radar de 41 proyectos ya completado.

### Checkpoint Sol 6.1: ámbitos y bloques

H20 aislamiento de curación implementado en `42a29971`; H12 bloques assistant
junto a llamadas Anthropic en `466c01e3`. Informes [curator](CODEX_H20_CURATOR_SCOPE.md)
y [Anthropic](CODEX_H12_ANTHROPIC_BLOCK_CONTENT.md) conservan fuentes, reproducciones
y límites. Revisión del coordinador: **146 pruebas conjuntas correctas, 16,84 s**,
y **30 correctas, 2,63 s** de curator/aislamiento/vigencia sobre el código final.
Ninguna petición real a modelos ni acceso a memoria personal.

Siguiente evaluación: snapshot aprobado de instrucciones H21 e identidad causal
de subagentes H08. No confundir evaluación con implementación. Leases H20,
renderer canónico H12 y demás pendientes transversales siguen abiertos.
