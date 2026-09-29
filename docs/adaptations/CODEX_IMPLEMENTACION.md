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
| H03 | `fc435eae`: intención sincronizada antes del correo con call_id y run activo, aliases MCP incluidos; 64 pruebas ampliadas y 29 finales. `e4970af7`: intención/resultado usan el recorder privado del mismo run, sin migrar al reemplazo; 72 pruebas finales. Outbox independiente, rutas sin recorder e idempotencia externa pendientes |
| H04 | `0572e1af`: 55 pruebas de normalización→evento→reinicio. `a40b8bcb`: productores propagan timeout real, 71 pruebas y 7 omitidas; exit 124 voluntario no implica timeout. `1b2537c9`: Studio conserva parcial/desconocido en directo e historial, 33 pruebas, tipos/build y render aislado correctos. Errores genéricos e identidad de intentos pendientes |
| H05 | `7c558eee`: round-trip de 220 herramientas y MCP; `155fad3d`: errores numéricos PDF; `28b2c2b0`: schemas independientes entre origen/snapshot/exportación. `3bfc22e2`: contrato único argumentos/schema/parser PDF, 122 pruebas. `fdce5d9e`: captura ejecutor/contrato PDF por llamada con revocación vigente, 172 pruebas. `a7428aea`: structural_rewrite rechaza apply no booleano antes del backend; 69 pruebas. Autoridad común general y snapshot por paso pendientes |
| H06 | `0f8e6dc4`: recibo shadow del schema preparado por candidato, comparado con binding PDF y propagado a eventos; 56 pruebas finales. Ronda/candidato obsoletos no se comparan. Snapshot de paso y autoridad general pendientes |
| H08 | `b7089ff5`: progreso por call_id; `db99abd9`: archivos exclusivos por run y recuperación, 114 pruebas. `ddbf3a6f`: identidad de delegación en historial. `2e208adf`: origen del drain capturado antes de dispatch, 58 pruebas. `b986d80c`: cada intento worker tiene UUID propio compartido por harness/trace/contexto causal y nietos; sin recorder no envía correo, 79 pruebas finales. Ledger/journal propio, replay completo y aprobación reanudada pendientes |
| H09 | `1a418269`: reutilización exige ámbito/política/solicitud coincidentes; `b63f1e5b`: recibos de skills renderizadas. `56ec3829`: FileSource identifica cuerpo capturado con hash y scope, 95 pruebas. `23c4961b`: valida referencias file entregadas antes de reutilizar, desconocido/legacy recompila, 79 pruebas finales. Documentos/memoria, versiones universales y entrega efectiva pendientes |
| H10 | `6127bd24`: fuentes externas no se promueven a restricciones/objetivo. `ebf7a856`: persistencia con SQLite y compactaciones, 99 pruebas. `09f174d7`: prompt refresca sólo referencias mecánicas verificables de approvals y conserva historia/prosa; metadata inválida no rompe el chat, 69 pruebas finales. Checkpoint portable y prosa histórica no mecánica pendientes |
| H11 | `3338166d`: drenaje por bloques UTF-8, salida/progreso acotados y actividad sin LF; 91 pruebas y 3 omitidas. `6305ed06`: Code Mode drena stderr, 26 pruebas; `6b19a2fe`: frames grandes dentro de cuota y rechazo explícito del exceso, 32 pruebas. Manager/stdin/handles durables pendientes |
| H12 | `906ae3c7`: reparación de llamadas conserva contenido multimodal; `d466bf5a`: marcador legacy no borra imágenes. `466c01e3`: Anthropic conserva bloques junto a tool_calls. `d5d23dc7`: fallback sin visión conserva texto/bloques junto a images nativas, 114 pruebas finales. `ed87cf00`: captura del evento live normalizada antes de emit, 42 pruebas finales. Proyección canónica y recibos pendientes |
| H13 | `f22702d9`: función pura compara en shadow ampliación/ciclos/presupuesto sin gobernar el bucle, 75 pruebas y 432 combinaciones. `291907ed`: cierre CE distingue propuesta y continuación concedida al agotar cupo, 56 pruebas; controlador común pendiente |
| H14 | `ce4df06a`: retries de workers acumulan tokens por intento; `47cffe37`: uso auxiliar observado en traza. `acb098e0`: compactor carga tokens/gasto observado en ledger del turno y comprueba admisión antes de inferir, incluido fallback y coste sin tokens; 160 pruebas finales. Otros auxiliares, reserva previa y cuentas transversales pendientes |
| H15 | `30e71fc8`: comentarios históricos de wiring y bridge corregidos contra callers actuales; AST sin docstring idéntico. Alcance de dos módulos completado |
| H16 | `303d4f63`: estados inciertos y llamadas anunciadas sin resultado no verifican progreso/fuentes; 191 pruebas finales. `2d32937a`: Enseñame registra éxito normalizado, 41 pruebas. `dbdf0987`: worker conserva evidencia hasta SQLite/reapertura, 120 pruebas. `5de909f8`: Code Mode propaga incertidumbre interna con recibos acotados, 23 pruebas. Verificación transversal e incertidumbre de host directo pendientes |
| H19 | `d7600e54`: steering sin run/sesión resoluble no se difunde a todos los workers; 61 pruebas. `31f4381e`: recepción por intento cierra antes de done/error terminal y reabre en retry; 46 pruebas finales/2 omisiones. Recibos durables/recuperación y entrada aceptada en última ronda pendientes |
| H21 | `54fea979`: instrucciones se refrescan por contenido acotado, no mtime; 66 pruebas y 1 POSIX omitida. `b63f1e5b`: recibos L1 ensamblados. `da728abe`: prompt y compactor renderizan los bytes capturados usados por la comprobación de aprobación; 90 pruebas finales coordinador. Jerarquía, reglas/objetivos fuera del digest, transacción de directorio y procedencia completa/entrega de skills pendientes |
| H24 | `92c6ebee`: elimina contaminación global de imports en fixture de skills; mismo orden integrado 252 pruebas correctas tras timeout previo. Banco pareado con modelos y aislamiento general pendientes |
| H20 | `42a29971`: identidad de mutación compartida en deduplicación/conflictos; ámbitos y sesiones distintos no se mezclan, origen de sesión en proyecto permite consolidación. 30 pruebas finales de ámbito/vigencia/propietario. Leases y atomicidad concurrente pendientes |
| H18 | `70974a90`: mutex compartido process-local para edit_file/write_file; `9ce4a9ff`: preview efectiva revalidada dentro del mutex. `1290a5c5`: apply_patch valida todas sus revisiones preparadas antes del journal bajo mutex múltiples ordenados, incluida compensación; 157 pruebas conjuntas/4 omisiones. Previews desconocidas, escritores externos y claims generales pendientes |
| H17 | `03432131`: lookup y categorías filtran permisos antes de anunciar schema/promoción; fallback permitido respeta pool vacío, 42 pruebas finales. Exposición por descriptor/snapshot y ranking después de filtro pendientes |
| H22 | `639ef1e6`: probes omitidos conservan observaciones anteriores. `358f2f34`: clave scoped y writer nativo/capabilities/fit/explorer, 115 pruebas. `1f3bfcbd`: provider_policy y alternativas usan almacén contextual, 106 pruebas. `0e696621`: router offline sólo declara sin contexto, 94 pruebas finales. `50ce7888`: cambios de modelo/sesión contextualizan hints, 65 pruebas finales. Configuración/version del endpoint y probes específicos pendientes |
| H23 | `7575dbc5`: listado de trazas conserva run y uso observado, cero/parcial y coste desconocido sin inventarlo; 53 pruebas finales. Vista y agrupación causal por fase pendientes |
| H07 | Controlador común pendiente conforme al análisis original |

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

Esas evaluaciones dieron lugar a `da728abe` (H21) y `ddbf3a6f` (H08).
No confundir estos pilotos con cierre transversal. Leases H20,
renderer canónico H12 y demás pendientes siguen abiertos.

### Checkpoint: contrato booleano, identidad y bytes aprobados

- `a7428aea`: [rewrite estructural](CODEX_H05_STRUCTURAL_REWRITE_BOOLEAN.md),
  69 pruebas correctas en 2,99 s; `apply="false"` no selecciona escritura.
- `ddbf3a6f`: [identidad de workers](CODEX_H08_SUBAGENT_CAUSAL_IDENTITY.md),
  78 correctas en 36,22 s con board, retries y outcomes; sin padre terminal ficticio.
- `da728abe`: [snapshot aprobado](CODEX_H21_APPROVED_SNAPSHOT.md),
  90 correctas finales en 7,75 s con reglas/compacción; 171 y 1 omisión POSIX
  en selección amplia previa del agente, diferenciada del conjunto final.

Evaluaciones siguientes: contexto causal del caller→delegación H08,
consumo observado de auxiliares H14 y concurrencia de ediciones H18.
`edit_file.replace_all` corregido en `2ffaec79`: [informe](CODEX_H05_EDIT_FILE_BOOLEAN.md),
77 correctas y 2 omisiones Windows en 3,81 s; valores inválidos sin operaciones de archivo.
Numéricos de structural_search evaluados sin cambio: el dispatcher ya normaliza
errores y el backend tiene coerciones existentes; no se declaran certificados.

### Checkpoint: despacho causal y exclusión de escritores

`2e208adf`: [origen de dispatch](CODEX_H08_DISPATCH_CAUSALITY.md), 58 pruebas
finales en 19,36 s. Un caller antiguo mantiene su run real aunque se sustituya
el registro de sesión. Reset probado incluso si falla el finalizador.

`70974a90`: [mutex por archivo](CODEX_H18_FILE_MUTATION_MUTEX.md), 66 correctas
y 2 omisiones Windows en 3,56 s; 33 con política de reescritura en 1,63 s.
Las cuatro parejas edit/write con la misma base ya no devuelven dos éxitos
pisándose. No hay locks atravesando awaits; la vigencia de previews previas
requiere otro incremento. Tampoco son locks de OS o entre procesos.

Esos tramos están implementados: `47cffe37`, `e4970af7` y `9ce4a9ff`, respectivamente.

### Checkpoint: uso auxiliar, previews y recorder

- [Uso auxiliar](CODEX_H14_AUX_USAGE_TRACE.md) `47cffe37`: 146 correctas finales
  en 7,51 s; 307 en selección amplia del agente. Se conserva evidencia, sin
  integrarla aún en el presupuesto; dimensiones ausentes no son cero ficticio.
- [Preview](CODEX_H18_REVIEW_PREVIEW_REVISION.md) `9ce4a9ff`: 130 correctas,
  2 omisiones Windows, 4,66 s. Review noop/error/unparsed conserva fail-open;
  lecturas desconocidas quedan explícitamente fuera de su garantía.
- [Recorder](CODEX_H03_EFFECT_RECORDER_CAUSALITY.md) `e4970af7`: 72 correctas
  finales en 19,17 s. Pending y terminal siguen en el run original; un writer
  cerrado no entrega el resultado al nuevo. Memoria sin persistencia conservada.

`acb098e0`: [compactor y presupuesto](CODEX_H14_COMPACTION_BUDGET.md),
160 correctas finales en 142,68 s. Mantiene exclusión del utility local y bypass
del principal local. No reserva presupuesto antes de cada auxiliar.

`31f4381e`: [recepción de instrucciones](CODEX_H19_STEERING_LIFECYCLE.md),
9 casos nuevos; selección final 46 correctas y 2 omitidas en 34,97 s.
La admisión confirma encolado; no equivale a inyección ni persistencia.
H17 anuncio/permisos implementado en `03432131`; H22 skip probes en `639ef1e6`.
Identidad endpoint/protocolo H22 en implementación; vigencia de fuentes H09
y menciones históricas H10 solo evaluación.

`1290a5c5`: [preparación de patches](CODEX_H18_PATCH_PREPARATION.md),
13 regresiones nuevas; selección conjunta final de archivos/workers:
157 correctas y 4 omitidas en 39,61 s. Los destinos duplicados canónicos se
rechazan antes del batch. No proporciona transacción del filesystem ni
aislamiento frente a procesos externos.

### Efectos terminales y capacidades — checkpoint 30-09-2026

- `cdcd04c0`: [recuperación de efectos terminales](CODEX_H03_TERMINAL_EFFECT_RECOVERY.md), 80 pruebas finales del coordinador en 31,95 s. Advertencia persistida y acuse durable antes de poda; no reenvío ni journal propio del worker. H03 continúa parcial.
- `50ce7888`: [capacidades al cambiar modelo](CODEX_H22_MODEL_SWITCH_SCOPE.md), 65 pruebas finales en 7,03 s. Sin contexto anterior resoluble sólo anuncios, sin probes inventados.
- `ed87cf00`: [capturas de eventos](CODEX_H12_TOOL_IMAGE_EVENT_NORMALIZATION.md), 42 pruebas finales en 24,13 s. No modifica la proyección histórica ni construye el flujo de edición de adjuntos del chat.

H07/H13 controlador de continuación está en implementación tras evaluación de equivalencia del gate completo. Ningún cierre transversal del backlog. Fuentes y límites constan en cada documento técnico; no se han hecho envíos ni llamadas a modelos reales.
