# Adaptaciones del harness de Codex: implementación

Plan original: [CODEX_HARNESS_ANALISIS_2026-09-29.md](CODEX_HARNESS_ANALISIS_2026-09-29.md). Referencia fijada: [openai/codex b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9). Las implementaciones son propias y parciales salvo indicación contraria; no se ha ejecutado el producto Codex como benchmark.

## H01: declaración verificable del runtime host

El mínimo revisable está implementado: las descripciones nativas, catálogo y ajustes dicen que Code Mode ejecuta Python en el host con acceso a archivos/red. `tools.call` conserva su puerta de políticas, pero Python directo no la atraviesa. La herramienta ya estaba clasificada como `EXECUTE_CODE` y desactivada por defecto; se conserva esa clasificación.

Cada resultado del runner incluye `runtime_guarantees`: `host_process`, filesystem/network no aislados y alcance de políticas `tools.call_only`. Se incluye también al fallar/terminar por cuota. No se convierte `python -I`, el entorno mínimo ni el directorio temporal en una supuesta frontera del sistema operativo. La descripción de la pausa de aprobación refleja que depende del ajuste existente.

Prueba decisiva: el runner real escribe un señuelo temporal fuera del cwd del guest sin usar herramientas; el resultado declara correctamente acceso host. También se comprueba la declaración en un error real de Python. Pasaron 16 pruebas de runner, paridad de políticas, cuotas y aprobación.

**Cerrado después (30-09/01-10):** Code Mode corre por defecto en un contenedor confinado (sin red, solo las carpetas concedidas, hijos incluidos) y se rechaza en vez de caer al host; `sandbox_probe` lo comprueba con accesos reales. Ver la tabla y `docs/design/codex-closure/H01.md`.

## Estado del backlog

Todos los puntos H01-H24 están cerrados (30-09/01-10-2026). Cada nota de `docs/design/codex-closure/` recoge lo añadido, las pruebas y los límites que quedan.

| ID | Estado |
|---|---|
| H01 | `664894a5`: autoridad host explícita, 16 pruebas; confinamiento pendiente **CERRADO 30-09/01-10** — `39006e1d`/`46dbe69a`: Code Mode confinado en contenedor por defecto, rechazo en vez de host; `5847fd9e` y verificación en Windows con Docker Desktop; nota `docs/design/codex-closure/H01.md`. |
| H02 | `5f781023`: política solicitada/efectiva. `4fe0d52e`: salida Docker ambigua conserva incertidumbre. `5ad626b9`: modo opt-in required rechaza fallback host para bash/python/powershell, política capturada durante despacho; 124 pruebas y 5 Docker omitidas. Backend Windows y probes reales pendientes **CERRADO 30-09/01-10** — `2a959ef5`/`46dbe69a`: `sandbox_probe` con accesos reales permitidos y prohibidos por operación; en Windows, todas `verified`; nota `docs/design/codex-closure/H02.md`. |
| H03 | `fc435eae`: intención sincronizada antes del correo con call_id y run activo, aliases MCP incluidos; 64 pruebas ampliadas y 29 finales. `e4970af7`: intención/resultado usan el recorder privado del mismo run, sin migrar al reemplazo; 72 pruebas finales. Outbox independiente, rutas sin recorder e idempotencia externa pendientes **CERRADO 30-09/01-10** — `0dfbacba`/`baae1ca5`: bandeja de salida con intención escrita y releída antes de cada efecto irrepetible; nota `docs/design/codex-closure/H03.md`. |
| H04 | `0572e1af`: 55 pruebas de normalización→evento→reinicio. `a40b8bcb`: productores propagan timeout real, 71 pruebas y 7 omitidas; exit 124 voluntario no implica timeout. `1b2537c9`: Studio conserva parcial/desconocido en directo e historial, 33 pruebas, tipos/build y render aislado correctos. Errores genéricos e identidad de intentos pendientes **CERRADO 30-09/01-10** — `0dfbacba`/`840e42ba`: `attempt_id` y `effect_certainty` en cada resultado con efecto; las lecturas vuelven tal cual; nota `docs/design/codex-closure/H04.md`. |
| H05 | `7c558eee`: round-trip de 220 herramientas y MCP; `155fad3d`: errores numéricos PDF; `28b2c2b0`: schemas independientes entre origen/snapshot/exportación. `3bfc22e2`: contrato único argumentos/schema/parser PDF, 122 pruebas. `fdce5d9e`: captura ejecutor/contrato PDF por llamada con revocación vigente, 172 pruebas. `a7428aea`: structural_rewrite rechaza apply no booleano antes del backend; 69 pruebas. `d223b67f`/`b18803cc`: sesión/contrato/argumentos MCP capturados, 105 pruebas. `1c854de1`: argumentos ligados a schema preparado del candidato, 126 pruebas. Autoridad común general y snapshot por paso pendientes **CERRADO 30-09/01-10** — `608becab`/`5aabe258`: autoridad única por herramienta (esquema, parser, límites, efectos, exposición) y paridad; nota `docs/design/codex-closure/H05.md`. |
| H06 | `0f8e6dc4`: recibo shadow del schema preparado por candidato, comparado con binding PDF y propagado a eventos; 56 pruebas finales. Ronda/candidato obsoletos no se comparan. `1c854de1`: proyección preparada para validación builtin y recibo observable. Snapshot de paso y autoridad general pendientes **CERRADO 30-09/01-10** — `87690104`/`34544d71`: `StepSnapshot` por paso con contrato anunciado y comprobaciones vivas; nota `docs/design/codex-closure/H06.md`. |
| H08 | `b7089ff5`: progreso por call_id; `db99abd9`: archivos exclusivos por run y recuperación, 114 pruebas. `ddbf3a6f`: identidad de delegación en historial. `2e208adf`: origen del drain capturado antes de dispatch, 58 pruebas. `b986d80c`: cada intento worker tiene UUID propio compartido por harness/trace/contexto causal y nietos; sin recorder no envía correo, 79 pruebas finales. Ledger/journal propio, replay completo y aprobación reanudada pendientes **CERRADO 30-09/01-10** — `8fc3d2a8`: libro de ejecución de solo añadir; aprobación reanudada por su id tras reinicio; nota `docs/design/codex-closure/H08.md`. |
| H09 | `1a418269`: reutilización exige ámbito/política/solicitud coincidentes; `b63f1e5b`: recibos de skills renderizadas. `56ec3829`: FileSource identifica cuerpo capturado con hash y scope, 95 pruebas. `23c4961b`: valida referencias file entregadas antes de reutilizar, desconocido/legacy recompila, 79 pruebas finales. `42e6d978`: consulta objetivos revalidada, 286 pruebas. `769e39a1`: consulta memoria personal revalidada sin escritura. `d71a6b10`: permiso/owner documental antes del store, 210 pruebas. `0f710cf9`/`169c5bd2`: consulta documental estricta revalidada con adaptador capturado, QA Chroma real44. `99d27ee5`: standing revalidado, 300 pruebas/1 omisión. `c0514371`: prerequisito vector strict; `9dc7eeed`: by-ID scope/vigencia. `1e9bca1f`: semanticdeshabilitado no consulta vector. `85058e46`: primitiva híbrida readonly, QA real70. MemEngine recibo híbrido, configuración/versiones universales y entrega efectiva pendientes `5aada5d7`: consulta híbrida con texto por `context_search` estricta y recibo con ruta, identidad vectorial y reloj de puntuación; 89 pruebas + QA Chroma real 83. **CERRADO 30-09/01-10** — `62e646d6`/`cffb1d1f`: auditoría del prompt final contra el manifiesto por ronda; nota `docs/design/codex-closure/H09.md`. |
| H10 | `6127bd24`: fuentes externas no se promueven a restricciones/objetivo. `ebf7a856`: persistencia con SQLite y compactaciones, 99 pruebas. `09f174d7`: prompt refresca sólo referencias mecánicas verificables de approvals y conserva historia/prosa; metadata inválida no rompe el chat, 69 pruebas finales. Checkpoint portable y prosa histórica no mecánica pendientes **CERRADO 30-09/01-10** — `034dfb7b`: registro de continuidad portable con autorizaciones atadas a prueba estructurada; nota `docs/design/codex-closure/H10.md`. |
| H11 | `3338166d`: drenaje por bloques UTF-8, salida/progreso acotados y actividad sin LF; 91 pruebas y 3 omitidas. `6305ed06`: Code Mode drena stderr, 26 pruebas; `6b19a2fe`: frames grandes dentro de cuota y rechazo explícito del exceso, 32 pruebas. `dad52508`: consulta de logs bg_jobs con IO/memoria acotados, 70 pruebas. `da7412f6`: cancelación CodeMode recoge hijo directo, 2 nuevas-Werror. `58c8b7c9`: JobObject propio con KILL_ON_JOB_CLOSE contiene nietos de Code Mode en Windows, 13 pruebas -Werror y 41 Code Mode. Stdin/handles durables y manager general pendientes **CERRADO 30-09/01-10** — `2a959ef5`: gestor de procesos con handles; `5847fd9e`: parada por job object cuenta como señalada, probado en Windows; nota `docs/design/codex-closure/H11.md`. |
| H12 | `906ae3c7`: reparación de llamadas conserva contenido multimodal; `d466bf5a`: marcador legacy no borra imágenes. `466c01e3`: Anthropic conserva bloques junto a tool_calls. `d5d23dc7`: fallback sin visión conserva texto/bloques junto a images nativas, 114 pruebas finales. `ed87cf00`: captura del evento live normalizada antes de emit, 42 pruebas finales. Proyección canónica y recibos pendientes Imágenes en chat: `a756c1f5`/`3cb3f911` (9 fallos reales del recorrido completo en navegador), `bcfe6e8e` armonizar vía estudio, `d86f9726` + Prospero `6a01d9d` cancelación acotada y recolección tras reinicio. `512bf6d4` + Prospero `8b08591` (y `qa-alpha`): ampliar y quitar fondo en el estudio, recorte con alfa probado en navegador. **CERRADO 30-09/01-10** — `4e99ba0c`: historial proyectado a forma canónica con recibos de reparación; nota `docs/design/codex-closure/H12.md`. |
| H13 | `f22702d9`: función pura compara en shadow ampliación/ciclos/presupuesto sin gobernar el bucle, 75 pruebas y 432 combinaciones. `291907ed`: cierre CE distingue propuesta y continuación concedida al agotar cupo, 56 pruebas; controlador común pendiente **CERRADO 30-09/01-10** — `87690104`: política única de rondas extra (`extra_round_policy.py`) en modo `enforce`; nota `docs/design/codex-closure/H13.md`. |
| H14 | `ce4df06a`: retries de workers acumulan tokens por intento; `47cffe37`: uso auxiliar observado en traza. `acb098e0`: compactor carga tokens/gasto observado en ledger del turno y comprueba admisión antes de inferir, incluido fallback y coste sin tokens; 160 pruebas finales. `28385388`: recuperación observada por step con admisión, 53 pruebas. `2d32dcf8`: admisión recovery incluye main observado pendiente, 73 pruebas. `ff0b10a3`: admisión main retry con uso pendiente, 91 pruebas. `e119cec5`: residual principal liquidado al cierre, 109 pruebas. `1f66cd71`: admisión antes de tools con consumo pendiente, 61 y 94 pruebas en selecciones distintas. `6974f6c8`: espera main observada por await y compensada, 113 pruebas. Duración recovery, otros auxiliares, reservas y cuentas transversales pendientes `984ce0e6`: pasos 2/3 de recuperación miden solo esperas del proveedor y la admisión del siguiente paso las ve; 48 pruebas. **CERRADO 30-09/01-10** — `5a45c37c`: una cuenta por turno de todas las llamadas a modelo, por intento y propósito; nota `docs/design/codex-closure/H14.md`. |
| H15 | `30e71fc8`: comentarios históricos de wiring y bridge corregidos contra callers actuales; AST sin docstring idéntico. Alcance de dos módulos completado |
| H16 | `303d4f63`: estados inciertos y llamadas anunciadas sin resultado no verifican progreso/fuentes; 191 pruebas finales. `2d32937a`: Enseñame registra éxito normalizado, 41 pruebas. `dbdf0987`: worker conserva evidencia hasta SQLite/reapertura, 120 pruebas. `5de909f8`: Code Mode propaga incertidumbre interna con recibos acotados, 23 pruebas. Verificación transversal e incertidumbre de host directo pendientes **CERRADO 30-09/01-10** — `2735a316`: verificación de resultado común (verified/unverified/uncertain); nota `docs/design/codex-closure/H16.md`. |
| H19 | `d7600e54`: steering sin run/sesión resoluble no se difunde a todos los workers; 61 pruebas. `31f4381e`: recepción por intento cierra antes de done/error terminal y reabre en retry; 46 pruebas finales/2 omisiones. `2b5b6a3f`: cierre antes de fanout de tres guards terminales, 86 pruebas; recuperación no terminal conserva recepción. `d9ae4eeb`: cierre en budget_exhausted, 71 pruebas. `72694f76`/`0716884d`: cancelled terminal actual comprobado. `bfd5992a`/`4dc501a1`: journal queued/drained/applied/dropped, 104 pruebas. `ca616048`: consulta de journal tras reinicio con ACL de ambas sesiones, 101 pruebas. Último drain evaluado: admisión en cola puede terminar dropped explícito; restauración operativa pendiente **CERRADO 30-09/01-10** — `ca805e57`/`a87262bd`: recibos duraderos de mensajes de dirección; los de «enviar después» los manda el cliente; nota `docs/design/codex-closure/H19.md`. |
| H21 | `54fea979`: instrucciones se refrescan por contenido acotado, no mtime; 66 pruebas y 1 POSIX omitida. `b63f1e5b`: recibos L1 ensamblados. `da728abe`: prompt y compactor renderizan los bytes capturados usados por la comprobación de aprobación; 90 pruebas finales coordinador. `e62ad0eb`: caché de reglas por proyección capturada, 165 correctas/1 omitida. `a327ccd3`: snapshot/digest conjunto reglas, 137 pruebas; v1sinreglas conservado. Objetivos JSONL relectura evaluada con mismo timestamp: sin fallo de caché reproducido. Jerarquía, transacción de directorio y procedencia completa/entrega de skills pendientes **CERRADO 30-09/01-10** — `ceff3430`: jerarquía de instrucciones por ruta y versión de skill revelada; nota `docs/design/codex-closure/H21.md`. |
| H24 | `92c6ebee`: elimina contaminación global de imports en fixture de skills; mismo orden integrado 252 pruebas correctas tras timeout previo. Banco pareado con modelos y aislamiento general pendientes **CERRADO 30-09/01-10** — `c1a83704`/`b18b5770`: banco pareado del harness y servidor MCP `harness`; nota `docs/design/codex-closure/H24.md`. |
| H20 | `42a29971`: identidad de mutación compartida en deduplicación/conflictos; ámbitos y sesiones distintos no se mezclan, origen de sesión en proyecto permite consolidación. 30 pruebas finales de ámbito/vigencia/propietario. Leases y atomicidad concurrente pendientes **CERRADO 30-09/01-10** — `aef12f13`: mantenimiento de memoria con arrendamientos, vallas y solo con trabajo nuevo; nota `docs/design/codex-closure/H20.md`. |
| H18 | `70974a90`: mutex compartido process-local para edit_file/write_file; `9ce4a9ff`: preview efectiva revalidada dentro del mutex. `1290a5c5`: apply_patch valida todas sus revisiones preparadas antes del journal bajo mutex múltiples ordenados, incluida compensación; 157 pruebas conjuntas/4 omisiones. Previews desconocidas, escritores externos y claims generales pendientes **CERRADO 30-09/01-10** — `132a72a0`: reclamaciones de recursos antes de las llamadas y rechazo tras escritura externa; nota `docs/design/codex-closure/H18.md`. |
| H17 | `03432131`: lookup y categorías filtran permisos antes de anunciar schema/promoción; fallback permitido respeta pool vacío, 42 pruebas finales. `634de422`: filtro antes del corte final del catálogo, 50 pruebas. `a2fba514`: filtro del índice antes de corte; `17b6bc8a`: ventana adaptativa hasta256 y filtro lexical en fusión, 22 focales correctas. `a1b488e1`: autoridad de permisos fallida no se anuncia, 43 pruebas focales. Adapters legacy, candidatos tras256 y exposición por descriptor/snapshot pendientes **CERRADO 30-09/01-10** — `87690104`: oferta de herramientas según exposición (`tool_exposure.py`); nota `docs/design/codex-closure/H17.md`. |
| H22 | `639ef1e6`: probes omitidos conservan observaciones anteriores. `358f2f34`: clave scoped y writer nativo/capabilities/fit/explorer, 115 pruebas. `1f3bfcbd`: provider_policy y alternativas usan almacén contextual, 106 pruebas. `0e696621`: router offline sólo declara sin contexto, 94 pruebas finales. `50ce7888`: cambios de modelo/sesión contextualizan hints, 65 pruebas finales. `4f096bfd`: revisión UUID de configuración y writer capturado, 185 pruebas; lectores/snapshots y credenciales vinculadas implementados; `ee863d66` guarda de coherencia; `24f3685c` actualización optimista de credenciales evita refresh obsoleto; 102 pruebas integradas finales. SQL externo y probes específicos pendientes **CERRADO 30-09/01-10** — `7460d733`/`ea47cb28`: revisiones forzadas en la base de datos y sondas de endpoints; nota `docs/design/codex-closure/H22.md`. |
| H23 | `7575dbc5`: listado de trazas conserva run y uso observado, cero/parcial y coste desconocido sin inventarlo; 53 pruebas finales. `8889446f`: fase explícita compaction/recovery y step, 65 pruebas finales. `19167960`: fase capturada antes de cierre diferido, 127 pruebas. `33c845da`: run de entrada en trazas diferidas, 95 pruebas. Vista y agrupación causal pendientes **CERRADO 30-09/01-10** — `e75b9dac`/`a87262bd`: desglose causal del coste del turno en Studio y `run_report`; nota `docs/design/codex-closure/H23.md`. |
| H07 | `4cac9054`: primer gate de extensión gobernado por acción pura, fallback legacy ante error/malformed; grants y cierre intactos. 30 pruebas finales. Controlador común transversal pendiente **CERRADO 30-09/01-10** — `87690104`/`5aabe258`: decisión de cierre de ronda extraída a `loop_decisions.py` y caracterizada; nota `docs/design/codex-closure/H07.md`. |

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

### Continuación y catálogo — 30-09-2026

`baa744dd`: checkpoint documental anterior, sin cambio funcional.

- VISITADO/IMPLEMENTADO `4cac9054` H07/H13: la acción pura extend/stop gobierna el gate de ampliación; error o resultado malformado usa condición legacy. Grants, mutaciones, SSE y razones públicas conservados. Coordinador: 30 correctas finales / 1 matriz anterior deselectada, 3,51 s. Selección ampliada previa: 70 correctas / 1 deselectada, 89,74 s; recogió 13 de las 15 nuevas antes de los dos últimos casos, no sumar corridas. Documento CODEX_H13_CONTINUATION_ACTION_CONTROL.md. Controlador común transversal aún pendiente.
- VISITADO/IMPLEMENTADO `634de422` H17: filtra candidatos permitidos antes del límite final del catálogo. Coordinador: 50 correctas, 3,10 s; agente 50, 3,43 s. Documento CODEX_H17_LOOKUP_BEFORE_LIMIT.md. El corte interno vector/lexical del índice permanece pendiente y tiene prueba explícita; k/scores no alterados.
- Fuente original: https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9. No repetir métodos cerrados sin nueva pregunta.
- H03/H08 journal de workers sólo evaluado: requiere child confirmado en SQLite, lifecycle, fence de retry y resume ante incertidumbre. No conectado ni correo habilitado. Primer fence de retry incierto en evaluación/implementación; H20 concurrencia curator sólo evaluación. Fase 2 abierta. Uso 78% permitido, automatización activa; archivos ajenos preservados.


### Workers y marketplace — checkpoint 30-09-2026

- VISITADO/IMPLEMENTADO `52f7451d` H03: bloquea retry automático ack_only/empty ante herramientas partial/outcome_unknown/cancelled; informa incertidumbre. Coordinador: 97 pruebas correctas, 54,69 s; agente: 97, 56,82 s. Documento CODEX_H03_WORKER_RETRY_FENCE.md. No journal propio de worker, resolución de efectos ni fence completo de resume; correo del worker sigue sin habilitar. Fuente: https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9.
- VISITADO/IMPLEMENTADO por nueva petición directa: `77cb197e` catálogo inicial; `63c83c8a` API/CLI y catálogo final de 28; `b8dd8a73` interfaz Conectores → Marketplace. HoardLink obligatorio (id hoardhub), Watch/Book independientes y excluidos. Cada entrada guarda repository_url original en plugins/marketplace.json; referencia https://github.com/Luissalet/HoardLink y docs/api/plugin-marketplace.md. Instalar un opcional prepara primero el código del obligatorio si falta. Clones en plugins/<id>/repository, ignorados por Git; catálogo y manifiestos sí versionados. Enlaces personales en DATA_DIR/plugin-marketplace-links.json, fuera de Git. No instala dependencias ni arranca servicios.
- Verificación marketplace: 153 pruebas backend/API/catálogo/plugins correctas en 5,65 s; catálogo final 35 en 1,06 s. Clone HTTPS real de HoardLink en directorio temporal, identidad del manifiesto comprobada; sin ejecutar plugin. Check React/HappyDOM pasa con las 28 entradas, instalación/refresh, enlace, unlink, errores, traducciones y cobertura ejecutada del preview. TypeScript sin errores y Vite compila (2196 módulos, 4,67 s), avisos habituales de fuentes/chunks. Visual: escritorio oscuro y móvil 390×844 claro, rutas largas sin desbordamiento. Fixture inicial de preview con 26 imports corregida y comprobada por id; no defecto de producto. Traducciones anteriores preservadas.
- Estado local: los 28 repositorios existentes del usuario quedan enlazados, sin mover ni duplicar. No incluir rutas personales en commits. API nueva requiere recarga/reinicio habitual de Faustus; no se reinició el servicio del usuario.
- VISITADO/SÓLO EVALUADO H20: dos curators concurrentes reproducen borrado mutuo de ganadores (0 filas), cada uno anuncia total_active=1. CODEX_H20_CURATOR_SCOPE.md registra el caso; todavía sin reparación ni atomicidad transversal.
- VISITADO/SÓLO EVALUADO H22: cambiar base_url/api_key conservando endpoint id puede conservar evidencia scoped vieja. Reproducción con store temporal, no prueba end-to-end del PATCH (fixture AST falló por Request). Pendiente revisión de configuración y carreras con calibración en vuelo; no declarar corregido.
- Continuar por pendientes registrados y backlog Codex antes de nueva exploración. No repetir catálogo ni métodos terminados sin pregunta nueva o versión. Hardware/Ollama y APNs siguen dependencias reales. Uso 80% consumido, ordinaryUsageAllowed=true; automatización activa. Cambios ajenos preservados. No informe general nuevo.


### H20 concurrente — 30-09-2026

VISITADO/IMPLEMENTADO `7eba4083`: curaciones comparten un RLock de proceso desde selección hasta informe. Corrige intercalado entre curators con supervivientes opuestos, incluso ámbitos diferentes que comparten filas globales. SQLite real temporal: coordinador 103 correctas (15,34 s) y suite final de seis casos (1,00 s); agente 118 (16,35 s). No sumar selecciones solapadas. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9, documento CODEX_H20_CURATOR_SERIALIZATION.md. No transacción durable, multiproceso, rollback ni bloqueo de writers externos. No revisitar este piloto sin nueva pregunta.

H17 filtro antes de corte interno en implementación; H22 cambio de configuración SÓLO EVALUADO: PATCH registrado real + Request Starlette + SQLite temporal confirma base_url/api_key modificados conservando id y evidencia scoped anterior. Writer tardío vuelve a aceptar scope antiguo. Falta revision/fingerprint contextual y cobertura de calibración concurrente; no reparado.

Nueva pregunta del usuario: evaluar extracción de procesamiento de imágenes a Prospero para evitar duplicación. Ruta existente de chat con modelo de imagen genera/edita adjunto; no afirmar ausencia de toda edición. Verificados 21 tests existentes de routing/galería/URLs (2,15 s), no certifican edición real con GPU ni recorrido completo de Studio. Prospero local HEAD38fad2b896447da211aa206209d7432b1a0a8525 leído, sin cambios. Comparación API/motores/propiedad/backend activo, todavía sin extracción. Prioridad registrada; no crear segundo motor por inercia.


### Índice y frontera de imágenes — 30-09-2026

VISITADO/IMPLEMENTADO `a2fba514` H17: candidate_filter del is_permitted existente llega a ToolIndex cuando su firma lo admite. Estrecha ranking vector directo, RRF fusionado y lexical después de scores y antes de top-k; anchor bloqueado no consume plaza. Corpus, pesos, scores, profundidad consultada y k conservados. Coordinador: 189 pruebas correctas finales, 8,82 s; agente: 185, 8,51 s. Documento CODEX_H17_INDEX_PERMISSION_CUTOFF.md, fuente https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/registry.rs. Ventanas internas de 24/pool y adapters legacy siguen pudiendo ocultar candidatos fuera del pool; no se amplía consulta ni promete completar k. No repetir este método cerrado sin pregunta nueva.

VISITADO/SÓLO EVALUADO extracción imágenes, petición directa. Prospero https://github.com/Luissalet/ProsperosHoard.git revisión local38fad2b896447da211aa206209d7432b1a0a8525: api.py1309/1321 generación/edición; engine.py830/868, comfy_driver/jobs/store. Solapamiento funcional con Faustus media_runs/media_backends ComfyUI. Arquitectura recomendada: Prospero concentra ejecución y recetas; Faustus mantiene chat, adjuntos, permisos, historial y vistas de resultados. Primer piloto adapter opt-in ComfyUI generación simple y edición con referencia, sin otro motor paralelo.

Prerequisitos reales: Prospero GuardMiddleware es Host/origin, no owner; store/assets/proyectos carecen aislamiento usuario. Necesario mapping autorizado owner/session→project/job/asset y descarga filtrada. Reencolado startup de running y prompt_id sólo local NO equivalen al outbox/reconcile de media_runs Faustus; preservar controles de consentimiento/admisión y no reenvío automático incierto. Prospero actual no cubre OpenAI images/generations ni images/edits de Faustus: conservar transporte legacy hasta trasladarlo con pruebas, no quitar cobertura. HoardLink descubre endpoints físicos por Faustus; endpoint virtual Prospero no puede redescubrirse a sí mismo. Referencias de assets primero, sin migración destructiva ni dos archivos maestros. Evaluación de código, sin generaciones, migración ni extracción implementadas. Próximas pruebas piloto: adjunto→asset→job→resultado y reapertura, aislamiento usuarios, desconexión, cancelación, reinicio sin duplicar envío.

### Prioridad e imágenes — 30-09-2026

Orden obligatorio actualizado por petición directa: IMÁGENES → backlog Codex → adaptaciones de repositorios y exploración nueva. Automatización actualizada con este orden. H22 queda pendiente, no implementado por este bloque. No repetir revisiones de 41 proyectos ni pilotos cerrados sin pregunta nueva o versión.

- VISITADO/IMPLEMENTADO Faustus `350e0189`: el runtime no selecciona conectores de otro propietario cuando no hay uno visible. Conserva propios y compartidos. Coordinador 37 pruebas correctas, 1,46 s. Prerrequisito del puente, no revisión del marketplace.
- VISITADO/IMPLEMENTADO Faustus `3a43274a`: restaura imágenes de tool_events al reabrir un chat, deduplica y rechaza URLs inseguras; comprobaciones generated-images y model pasan. Conserva referencia de entrada.
- VISITADO/IMPLEMENTADO Prospero `ad45e84`: esquema SQLite 7 y recibos de intención/envío ComfyUI, prompt_id tras aceptación. Al arrancar retiene jobs running de imagen y queued/waiting_gpu con recibo; mensaje outcome_unknown sin reenvío. Legacy running también retenido. jobs sin envío probado y otros tipos conservan su política. Agente: 428 pruebas completas correctas, 219,92 s, frontend compila; coordinador: 31 focales correctas, 6,55 s. API local ComfyUI simulada, sin GPU/modelos reales. Guarda último recibo del batch, no journal completo ni recolección automática tras reinicio. Fuente original https://github.com/Luissalet/ProsperosHoard, revisión previa38fad2b896447da211aa206209d7432b1a0a8525; docs/IMAGE_SUBMISSION_RECOVERY.md. Repositorio limpio tras commit.
- VISITADO/IMPLEMENTADO Faustus `34ce0df9`: selección por usuario del estudio Prospero en Ajustes; generation y edición action=instruction desde chat/herramientas. Adjuntos propios incluyen Gallery image ID textual, incluso sin visión. Adapter exige permisos actuales, owner/sesión/conexión/proyecto/job/asset propios; localhost fijado a loopback, sin redirects/proxy. Recibos SQLite antes de POST y publicación idempotente PNG en galería. `image_job` consulta/recolecta el mismo trabajo sin POST ni referencia original. IDs de petición derivan de run/call reales, no argumentos del modelo. Resultado incierto tipado impide retry worker; IDs y estado sobreviven SSE/historial y texto que recibe el modelo.
- Pruebas Faustus: primera selección 74 correctas44,06s; selección tras recuperación/ruta85 correctas45,31s. Última ampliada87 correctas y2 fallos de fixture NameError por insertar asserts de captura bajo nuevo test; reubicados sin cambio de producto, archivo final4 correctas44,69s. Incluye adapter51, tools19, entrypoints, adjuntos, esquema y loop real. Regresión ampliada159 correctas22,60s (selecciones solapadas, no sumar). Node Settings/generated-images/model pasan; TypeScript y py_compile sin errores; Vite2197 módulos5,55s con avisos habituales fuentes/chunks. Visual desktop oscuro y móvil390x844 claro sin overflow. No generación/edición real con GPU ni certificación de calidad.
- Documento técnico docs/api/prospero-images.md registra uso, fuente, recuperación y límites. No informe general nuevo. PENDIENTE de consolidación: traslado OpenAI-compatible images/generations e images/edits y editores upscale/rembg/inpaint/harmonize; cancelación scoped (interrupt ComfyUI global) y reconciliación de trabajos GPU inciertos tras reinicio. Host/origin guard Prospero no equivale a auth multiusuario. Puente opt-in implementado, extracción total NO terminada. Sin migración destructiva ni otro motor ComfyUI en Faustus.
- Uso84% consumido, ordinaryUsageAllowed=true. Automatización activa. Cambios ajenos Stop-Faustus/desktop/Stop-Local-Models y documentos Codex sin versionar preservados. Servidor y pestaña de QA propios cerrados; viewport restaurado. No reinicio del servicio del usuario ni push.

### Imágenes: edición con máscara — 30-09-2026

VISITADO/IMPLEMENTADO `b2ce39f1`: action=inpaint del chat delega al estudio Prospero seleccionado; conserva servicio anterior para configured. Source y máscara se resuelven por GalleryImage activa del propietario, dimensiones iguales, prompt y strength finito0..1 antes de enviar. Adapter importa ambos al proyecto privado, guarda operation/mask_asset_id con migración additiva sin borrar recibos y envía /api/assets/{source}/edit. Fingerprint generación anterior intacto; inpaint incluye operación, source, máscara y strength, impidiendo reutilizar IDs con otro contenido. Resume sin inputs consulta trabajo existente sin nuevos imports/POST. Intentos ambiguos de importación/envío permanecen outcome_unknown, sin fallback a otro motor ni reenvío.

Fuente original https://github.com/Luissalet/ProsperosHoard: revisión ad45e84, api.py EditImageBody/op_edit/ui_edit, engine.py edit_image y plantilla sdxl_inpaint existentes. NO nuevo motor ni cambios de código en Prospero. Necesita checkpoint SDXL disponible; no generación GPU real. VISITADO/SÓLO EVALUADO matiz upscale: hires de Prospero vuelve a ejecutar receta SDXLtxt2img y exige esa receta; no equivale a upscale2x/4x del servicio existente de Faustus. No conectarlos por similitud de nombre. RemBg, harmonize, transportes OpenAI-compatible y reconciliación GPU/cancelación scoped siguen PENDIENTES; extracción total y backlog Codex aún no cerrados.

Coordinador: 135 pruebas finales correctas70,48s, incluyendo adapter79, tools28, legacygallery, entrypoints, routing, schema y privilegios; py_compile y diffcheck correctos. Agente73 completas60,51s antes6 casos de imports,6 nuevas5,33s y12 focales finales11,51s; no sumar lotes solapados. Fixture de incertidumbre inicial esperaba result_status raw que el wrapper no añade: corregida para probar normalize_tool_result real; sin fallo de producto. Docs API/esquema/índice describen máscara propia, estudio y requisito SDXL. No cambios UI, servicios reiniciados, push ni cambios ajenos incluidos. No repetir piloto cerrado ni matiz hires sin nueva pregunta. Orden vigente IMÁGENES→Codex→repositorios.

### VISITADO / PROBADO CON GPU REAL — instrucciones sin máscara, 30-09-2026

Petición nueva del usuario: probar de verdad sombrero/anime/personaje/famoso y entregar imágenes en este chat. Cuenta/sesión/SQLite/settings/connector aislados bajo D:/LocalAI/tmp/prospero-live-edit-20260930, sin modificar datos/configuración personales. ComfyUI0.37 propio en8189 --cuda-device1, RTX5060Ti16GB, Prospero propio8815 no demo. Modelos ya instalados: SDXL original512²24steps seed123456 (26,82s); cuatro qwen21_edit Qwen-Image2.1int8, text qwen3vl8bint8, VAEbf16,25steps1024² (85,90s/82,27s/82,66s/82,58s según Comfy). Fuente https://github.com/Luissalet/ProsperosHoard revisiónad45e84, workflows qwen21_edit; Faustus actualb2ce39f1.

Cuatro resultados reales vía do_edit_image action=instruction→adapter→Prospero→Comfy→galería propia, sin máscaras ni mocks: sombrero.png/anime.png/personaje.png(DarthVader)/famoso.png(KeanuReeves), mismo original.png ficticio. Inspección visual: instrucciones cumplidas, persona original reconocible/ropa/encuadre razonablemente conservados; detalles faciales redibujados, no fidelidad exacta prometida. Herramienta image_job por execute_tool_block real recuperó4 mismos IDs/URLs, sin aumentar jobs5(original+4), galería5 filas propias. Evidencia técnica results.json/recovery-checks.json en directorio QA, no informe general nuevo. No certifica interacción completa de chat/LLM/navegador; sí motores reales, tool/adapter/galería/recuperación. No GPUinpaint probado aquí.

FALLO MATERIAL VISITADO/PENDIENTE: gate VRAM bloquea siguiente render por caché DynamicVRAM fueraTorch. Antes free tras anime: vramfree6108488620, torch_total33554432, torch_free22553516; despuésfree15840313344,torch_free23592960. Estimador5836→15116MiB; no sumarTorch dosveces. Liberación manual exclusivamente del Comfy propio de QA permitióseguir. AgenteH12 diagnosticó además max(devices) admite por GPU distinta (repro helper synthetic), no causó casoendpointunGPU. Próximo piloto: admisión por endpoint/dispositivo primario y opción explícita de gestión memoria Comfy dedicado con cola libre, sin free automático global. SÓLO EVALUADO, no reparado ni múltiplesediciones desatendidas certificadas. Fuente backend.py vram_free_mb y engine check_vram_or_wait, Comfy DynamicVRAM aimdo0.5.5. No cerrar pendiente ni repetir diagnóstico sin nueva pregunta.

Docs API actualizados con evidencia/límite. Ambos servicios propios de QA cerrados después de todos jobsdone; imágenes/resultados preservados. Sin modelos nuevos, API externa, push o cambios ajenos. Prioridad imágenes→Codex→repos sigue vigente.

### VISITADO / IMPLEMENTADO / QA REAL — memoria y prioridad, 30-09-2026

Prospero `1fefe06` corrige selección de GPU (primaria, no max de otras tarjetas) y añade admisión explícita comfy.manage_memory=true sólo para URL fijada y mismo cliente/cola vacía/capacidad válida. No falsea free ni manda /free; endpoint de pool distinto no hereda permiso. Fuente https://github.com/Luissalet/ProsperosHoard; docs/COMFY_MEMORY_ADMISSION.md; Comfy local comfy/cli_args.py y comfy/model_management.py. 454 pruebas completas correctas220,38s, npm build correcto; coordinador26 focales4,04s. No vendor/UI editado.

QA REAL: modo default con opt-in admitió segundo render pero falló OOM (job_01M3QYXR979N90VQM4K6FJ0YVB, recibo accepted/prompt_id conservado, sin reenvío). No ocultar este límite: gate arreglado no equivale a OOM resuelto. Comfy0.37/aimdo0.5.5 propio arrancado con --disable-smart-memory SOLO, DynamicVRAM activo, permitió sombrero azul y anime consecutivos98,03s/81,96s sin /free/manualfree. Dos1024²PNG inspeccionados, done+receipts+galería propia; antessegundo15844507644bytesfree, así que esta secuencia no necesitó overridebajafree (validado sintéticamente, primerOOM loejerció). Evidencia D:/LocalAI/tmp/prospero-live-edit-20260930/smart-sequence-results.json y smart-sequence-{sombrero,anime}.png. ServidoresQA8189/8815 propios cerrados; configuraciónpersonal intacta. Perfilvalidado para estosmodelos/versión, no garantía todosworkflows/GPUs.

Prioridad actual por petición directa: cerrar este piloto memoria → pendientes Codex → adaptaciones/exploración. Piloto memoria y perfil dedicado probados; consolidación total de imágenes sigue PENDIENTE (transportes, rembg/upscale/harmonize, cancelación scoped y reconciliación incierta). Automatización activa actualizada. H22 revisión de configuración/calibración tardía EN IMPLEMENTACIÓN, no cerrar todavía. No repetir memoria/revisiones de41proyectos sin pregunta nueva/versión. Cambios ajenos preservados; uso85% consumido, ordinaryUsageAllowed=true.

### VISITADO / IMPLEMENTADO — H22 versión de conexión, 30-09-2026

Faustus `4f096bfd`: revisión UUID opaca y persistida del endpoint. Cambios ORM de base_url, api_key, provider_auth_id o endpoint_kind la rotan en la misma transacción; nombre/caché/preferencias no. Migración aditiva conserva registros y revisiones existentes. Scopes v3 incluyen revisión; artefactos v2 y legacy se conservan pero no verifican la conexión actual. Writer local captura revisión antes del primer await; completion tardía guarda en A aunque un PATCH real haya cambiado a B. El response tardío declara scope A, no una lectura actual de B. Calibración sin endpoint persistido devuelve 409 antes de IO. Capabilities y fit usan revisión actual capturada.

Pruebas offline: agente 172 correctas en 11,38 s y 13 skipped-probes correctas en 0,93 s (185); coordinador 55 correctas en 2,70 s. Incluyen PATCH registrado real, SQLite temporal, calibración concurrente, migración idempotente y rollback. El hook también cubre modificaciones ORM del alta que reutiliza un endpoint, sin prueba específica de esa ruta en este lote. Sin inferencias/modelos externos ni cambios UI. Fuente original https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; implementación propia, no revisión upstream repetida. Métodos ya visitados marcados; no repetir sin pregunta nueva/versión.

PENDIENTE: propagar revisión a provider_policy, cambios de modelo/runtime, explorer/deployment; sin revisión esos lectores quedan conservadores. Mutaciones internas de credenciales ProviderAuthSession y SQL bulk no atraviesan el hook ORM, requieren invalidación adicional; H22 completo no cerrado. Agentes implementan lotes separados de policy y snapshots runtime, sin incluir cambios ajenos.

VISITADO / SÓLO EVALUADO H02 runtime: CLI Docker 28.5.1 disponible, context desktop-linux y default fallan por ausencia de pipe; com.docker.service detenido. Probes reales propios devuelven backend_unavailable/kind=daemon. No inferir ausencia de imagen sin daemon. Cinco pruebas POSIX omitidas no certificarían confinamiento Windows required. Daemon/imagen disponibles son dependencia pendiente; no servicio arrancado ni configuración cambiada en esta evaluación.



### VISITADO / IMPLEMENTADO — H22 policy y cambios de modelo, 30-09-2026

Faustus `47d55c4c`: provider_policy captura ID/URL/revisión/digest explícitos del endpoint y usa la misma copia para el modelo solicitado y sus alternativas. No consulta una revisión nueva en DB, no adivina digest y no eleva probes de manifiestos suministrados por el caller. Registro exacto del store conserva autoridad, incluso false/vacíos; declaraciones siguen siendo declaraciones. 215 pruebas correctas en 12,66 s (185 anteriores + 30 nuevas). El caller auto de chat sin ID/revisión sigue deliberadamente sin observación; no afirmar cobertura runtime universal.

Faustus `fd3fb8fe`: resolver incluye revisión desde la misma fila que URL/headers; sesión captura revisión antes de cerrar DB; helper y fallback del bucle usan descriptores copiados antes de awaits. Primary activo y alternativa seleccionada tras deduplicar mantienen sus versiones; ya no se usa siempre requested_endpoint_id. Payload público conserva cuatro campos. 11 pruebas nuevas correctas en 4,60 s y 48 auxiliares en 16,51 s; prueba del bucle real con PATCH SQLite durante fallback y mutación de descriptores confirma que no se consulta la nueva revisión tras await. Coordinador integró los tres pilotos nuevos: 51 correctas en 5,70 s.

Regresión amplia inicial: 240 correctas y 4 fallos de fixtures antiguos que no admitían _usage_observer ya existente antes de H22. Faustus `afaaca50` corrige sólo tres firmas de fake_compact, sin producción: cinco casos afectados correctos en 8,50 s y archivo completo 106 correctas en 86,67 s. No sumar lotes solapados ni afirmar repetición completa de la selección amplia.

Fuente original para estos métodos: https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; adaptación propia de evidencia contextual y snapshots, sin revisión documental upstream repetida ni modelo externo. Ya visitados y cerrados esos pilotos. Pendiente H22: explorer/deployment snapshot en implementación; credenciales vinculadas modificadas internamente y SQL bulk fuera del hook, probes específicos. H22 completo aún no cerrado. Cambios ajenos preservados, automatización activa, uso permitido.

### VISITADO / IMPLEMENTADO — H22 deployments y cuentas vinculadas, 30-09-2026

Faustus `73f75a57`: ModelIdentity captura revisión y URL configurada junto al endpoint; conserva URL raíz operacional aparte. Copia opciones anidadas antes del await/I/O y evita releerlas después. Deployment con revisión conocida incluye endpoint/revisión en su ID; sin revisión mantiene exactamente el algoritmo histórico. ModelSpec no cambia. Migración SQLite aditiva conserva JSON/IDs/evidencia y permite coexistencia A/B. Explorer lee sólo revisión y transporte persistidos, sin rellenar históricos desde DB actual. URL nativa /api permite match v3; /v1 o históricos sin revisión siguen conservadores. 75 pruebas correctas en 6,13 s y coordinador 11 nuevas en 1,08 s. Incluye ruta FastAPI con SQLite y cambio de clave durante fetch simulado, copia de opciones, lectura histórica A tras cambiar DB a B, migración secuencial DELETE y concurrente WAL. Apertura inicial simultánea de DB DELETE puede fallar en PRAGMA WAL preexistente: observado, no cerrado. Consultas tags/show/version no son snapshot atómico del proveedor.

Faustus `29b444f3`: reconexión explícita, incluso mismos tokens, y cambios semánticos/tokens fuera de OAuth refresh invalidan todos los ModelEndpoints vinculados al auth.id en la misma transacción ORM. Nombre/timestamps no invalidan. Refresh exitoso existente usa marca privada de intención de un flush; sólo exime tokens, jamás provider/owner/base_url/auth_mode. Marcadores consumidos al flush y limpiados al rollback; no vienen de petición ni se guardan. 123 pruebas del agente correctas; coordinador 24 correctas en 2,48 s. Varias referencias, rollback, reauth tokens iguales, refresh estable y resultados históricos. Marca es confianza en el callsite, no prueba criptográfica de identidad de cuenta. SQL arbitrario externo no cubierto; migración de cifrado de representación no invalida credenciales semánticas.

Fuentes: https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y contratos locales ModelIdentity/ProviderAuthSession; sin revisión upstream repetida ni modelos/API externos. Pilotos marcados visitados. Próximos lotes en implementación: guarda que detecta mezcla revisión A/credenciales B durante resolución y evita fallback silencioso en consumidores conocidos; quitar userinfo/query/fragment del campo NUEVO endpoint_scope_url antes de persistir, preservando protocolo explícito. No declarar eliminación universal de carreras ni saneamiento de URL operacional legacy. H22 global no cerrado; probes específicos y SQL externo siguen pendientes. Cambios ajenos preservados.


### VISITADO / IMPLEMENTADO — H22 guarda de coherencia y URL de referencia, 30-09-2026

Faustus `ee863d66`: EndpointConfigurationChanged detecta cambios al resolver credenciales usando snapshot privado y lectura nueva de DB sólo para comparar. Si el resolver de credenciales falla, también se verifica deriva antes de permitir el error anterior. Cambios/desaparición/deshabilitado/propietario o imposibilidad de verificar producen error tipado sin adoptar B ni fallback silencioso a otro destino. Propagado por helpers/resolvers y consumidores conocidos subagent_tools, vision_routing, ai_interaction y fanout. 63 pruebas SQLite/reales de routing nuevas correctas (coordinador 3,06 s), selección final 116 correctas en 8,24 s. Amplia anterior 254 correctas en 97,98 s tenía 49 casos nuevos; no sumarla ni afirmarla como freeze final. Documento docs/adaptations/CODEX_H22_ENDPOINT_RUNTIME_COHERENCE.md. Comparación no es lease ni elimina cambios posteriores; API-key directa sin I/O de credenciales fuera de esta guarda.

Faustus `77532b11`: nuevo campo endpoint_scope_url elimina userinfo/query/fragment al construir el manifest, preserva host/puerto/ruta/IPv6 y deja URL malformada desconocida. No cambia URL operacional preexistente, IDs ni red. 84 pruebas del agente correctas en 6,39 s; coordinador 20 snapshot correctas en 1,49 s. No afirmar saneamiento global de URLs legacy.

Fuente original: https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9, sin repetir revisión cerrada. Nueva pregunta de usuario exige pruebas GPU: en curso LLM real llama.cpp Qwen2.5-3B q8 GPU3 + Comfy Qwen-Image 2.1 GPU1 propios. Primer stream real falló por GalleryImageID inventado por modelo y fue rechazado sin render; pendiente native-tool/fullstream QA, no afirmar éxito. Hallazgo nuevo READONLY con SQLite y funciones reales: refresh OAuth tardío A puede sobrescribir reauth B aun con mismos tokens; invalidación de endpoints no protege esa escritura. Lote CAS de credenciales en implementación y aún no cerrar. Uso87% consumido, ordinaryUsageAllowed=true; automatización activa. Modelos existentes, no downloads, configusuario preservada.

### VISITADO / PROBADO — inferencia GPU y protección OAuth, 30-09-2026

IMPLEMENTADO Faustus `24f3685c`: revisión opaca credential_revision y actualización optimista de ProviderAuthSession impiden que refresh OAuth tardío A sobrescriba reconexión B, incluso con mismos tokens. Conflicto revierte la escritura y devuelve error tipado/409; resolver propaga EndpointConfigurationChanged sin adoptar B ni fallback silencioso. Refresh ordinario conserva revisión del endpoint pero cambia versión de la fila auth. Migración aditiva idempotente. `ee4b0411` adapta fixtures research a endpoints SQLite reales. Coordinador: 102 pruebas integradas correctas en 5,92 s (8 CAS +14 invalidación vinculada +72 coherencia +8 research), sin GPU ni API externa. Fuente metodológica https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y contratos locales. H22 global sigue parcial: SQL externo y probes específicos pendientes; no lease universal.

PROBADO CON INFERENCIA REAL: Qwen2.5-3B-Instruct q8_0 en llama.cpp GPU3 y Qwen-Image 2.1 en Comfy GPU1, modelos instalados, Prospero no demo. Primera prueba stream sin descriptor tools soportado produjo ID inventado y rechazo de ownership sin render: fallo conservado. Piloto native posterior seleccionó edit_image automáticamente y completó; telemetría GPU1 pico100%, GPU3 pico92%. Piloto full stream_agent_loop con ModelEndpoint QA persistido supports_tools=true ofreció herramientas reales al modelo, ejecutó edit_image(instruction) por dispatcher→Prospero→Comfy→galería privada y emitió generated_image con URL correcta. 85,54 s totales, herramienta81,90 s; job_01M3R1B55R8G2W7Y5YX31ZSCRA, GalleryImage fb667e0e-f586-4d59-a380-577b4e4698bf. PNG1024² inspeccionado: sombrero verde y persona reconocible, detalles redibujados. Evidencia D:/LocalAI/tmp/prospero-live-edit-20260930/gpu-agent-1744f95029764b88bfbaeb4e2f04fcf5/{verification.json,stream.jsonl,result.json,telemetry.jsonl,fb667e0e-f586-4d59-a380-577b4e4698bf.png}. Observer fullstream inició tarde: GPU3 pico0 en sus10muestras; pico92 pertenece al piloto native, no mezclar mediciones.

PROBADO inpaint real por selección native del LLM→herramienta→Prospero→Comfy SDXL, máscara propia y strength0.85: 27,16 s, GPU1 pico100%, job_01M3R1MP651Q6ZR7W0GTDJT3CC, GalleryImage b3201cd9-1904-4394-964d-f9aefbeb0867. Original SHA conservado. Evidencia gpu-inpaint-4cf651186279480b86284bc988fbcc3c en mismo directorio QA. Inspección: sombrero azul estilizado con costuras visibles; funcionamiento probado, calidad fotográfica no certificada. No afirmar fullstream/UIinpaint.

PENDIENTE nuevo matiz visitado: Qwen3B inventa dominio/enlace en texto final aunque herramienta y evento SSE contienen URL correcta. No arreglado ni navegador/UI probado. Dos renders Qwen consecutivos y SDXL sin /free/liberación manual; perfil Comfy --disable-smart-memory sólo. No nuevos modelos ni cambios personales. Servicios propios8191/8189/8815 cerrados al acabar; listener ajeno8090 preservado. Fuente imágenes https://github.com/Luissalet/ProsperosHoard. No repetir estos pilotos salvo pregunta nueva/versión. Prioridad Codex→adaptaciones/exploración; extracción completa de imágenes sigue pendiente. Uso88% consumido, ordinaryUsageAllowed=true; automatización activa.

### VISITADO / IMPLEMENTADO — H17 recuperación acotada, 30-09-2026

Faustus `17b6bc8a`: sobrerrecuperación geométrica con candidate_filter recupera permitidos ocultos tras24 candidatos denegados; embedding una vez por lane, techo absoluto256 incluso k100/count400. Termina por suficientes nombres únicos permitidos/count/short response/score floor/cap. Sin filtro misma consulta legacy. Fusión no corta pool vector antes de permisos y pasa filtro al lexical antes de su corte; corpus BM25 y fórmula/pesos conservados, inputs RRF con filtro pueden cambiar deliberadamente. No política nueva ni ejecución de herramientas descubiertas.

Coordinador22 focales correctas1,27s; agente selección15suites231 correctas y2 fallos10,59s en test_lookup_tool_categories (MCP ficticio sin registro/schema). Los mismos2fallos reproducidos con src/tool_index.py de HEAD previo cargado sólo en proceso por plugin temporal (0,83s), sin revertir fuente compartida. Fallo baseline registrado, no reparado en este lote y no presentar suite amplia verde. Revisión independiente sin bloqueos. Sin modelos/GPU, red externa ni configuraciónpersonal modificada. Fuente original https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/registry.rs. Complementa residual de a2fba514; no repite su revisión cerrada.

Límites pendientes: ranking ordenado del backend para freno por floor; permitidos tras256 aún pueden quedar ocultos, adapters legacy conservan limitaciones; exposición por descriptor/snapshot de H17 sigue pendiente. Piloto acotado cerrado, H17 global parcial. Uso88% permitido; automatización activa. Cambios ajenos preservados. Siguiente: pendientes Codex, luego adaptaciones/exploración; consolidación de imágenes aún parcial.

### VISITADO / IMPLEMENTADO — H17 fixtures y H19 guards terminales, 30-09-2026

`2cc9f2ab`: fixtures MCP de categorías anuncian schemas reales coherentes con listado, validados por tool_registry.snapshot; sentinel prohíbe ejecución. Cuatro casos adicionales: disabled/policy antes de contar/promover, noadmin y ghost sin schema. Sólo test, sin producción. Categorías10 correctas1,05s; selección H17 antes231+2fallos ahora237 correctas10,77s. Coordinador32 correctas1,73s categorías/overfetch/cutoff. Fallos baseline de categorías cerrados con evidencia, no suite completa ni GPU.

`2b5b6a3f`: worker cierra accepts_steers antes del await fanout de rounds_exhausted/budget_exceeded/intent_nudge_exhausted. Repro previo con worker real y loopfixture: ACKtrue/tarea viva/steered0/cola1 durante guardterminal. Ahora rechaza ese intervalo, conserva cola previa y retry reabre; loop_breaker_triggered mantiene recepción porque recovery continúa. Coordinador16 lifecycle correctas2,16s; selección final agente86 correctas46,87s lifecycle/rutas/board/retries/causal/dispatch. Sin LLM/GPU ni servicios externos; pruebas de cola/lifecycle. Scope residual acotado cerrado; entrada antesúltimadrain, recibos durables/recuperación y otros eventos budget_exhausted/cancelled siguen pendientes. No concede rondas, modifica presupuesto ni difunde fuera ámbito.

Fuentes originales https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/registry.rs y https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; adaptación propia, no repetida revisión upstream. DocsH17/H19 actualizados. Uso89% permitido, automatización activa; siguientes pendientes Codex antes adaptaciones/exploración. Consolidación total de imágenes sigue parcial, memoria consecutiva real ya probada. Cambios ajenos preservados.

### VISITADO / IMPLEMENTADO — H19 autonomía y H21 reglas capturadas, 30-09-2026

Faustus `d9ae4eeb`: budget_exhausted ahora se ofrece como guard y cierra accepts_steers antes del await de fanout. Repro previo con _run_subagent real no emitía guard (observed_guards=[]); nueva prueba rechaza instrucción tardía con tarea viva, preserva cola previa y retry; métricas de budget no cierran recepción. Se inspeccionaron cuatro productores actuales que terminan roundloop. Coordinador19 lifecycle correctas2,48s; agente selección71 correctas32,57s lifecycle/rutas/board/retryusage. Sin LLM/GPU ni cuentas personales. cancelled SÓLO EVALUADO/PENDIENTE: recuperación agent_loop≈12187 sale sólo del asyncstream≈12004 y poststream≈12602 comprueba compaction, sin cierre universal probado; no cerrar por tipo. Preúltimadrain/recibos durables siguen pendientes. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; adaptación propia, no nueva revisión upstream.

Faustus `e62ad0eb`: caché de project_rules.block identifica proyección capturada por SHA256 del texto acotado/decodificado y metadatos ordenados/estado de lectura. Discovery único por llamada y misma tupla para firma/render/nota no confiable. Repro tempfile mismos byteslength y mtime preservado: antes stale=True/newpresent=False. Después coherente; pruebas cubren edición, altas/bajas, error/recuperación, orden, sin secondscan y nota sin contenido. Coordinador7 nuevas correctas0,70s; agente25 focales1,40s y selección165 correctas/1 omitida6,20s. Omisión POSIX de entorno, no certificar conducta omitida en Windows. No inferencias/GPU/API ni configuraciónpersonal.

LÍMITES H21: identidad de caché no aprobación. Digest de workspace sigue excluyendo rules/objetivos; no se añadieron permisos, aprobación ni veredicto nuevo. Hash de proyección acotada no todo archivo/directorio, captura secuencial no transacción. Biblioteca y sus TTL/rendercache previos fuera alcance. Objetivos H09 SÓLO EVALUADO/PENDIENTE: reuse_scope no declara versión de su fuente y guard de freshness comprueba sólo files; requiere recibo separado, no implementado. Fuente https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/agents_md.rs. Pilotos acotados visitados/cerrados; H19/H21 globales parciales. Cambios ajenos preservados, uso89% permitido y automatización activa; continúan pendientes Codex antes adaptaciones/exploración, consolidación imágenes aún parcial.

### VISITADO / IMPLEMENTADO — H21 biblioteca, continuación directa

`8ab116b9`: project_rules.block consulta snapshot de biblioteca antes de aceptar render cacheado. Firma incluye enabled y campos renderizados/orden/SHA256body; render usa esa misma tupla. Desactivado evita IO biblioteca. TTL5s existente se conserva; textoidéntico tras refresh reusa render. Repro anterior6casos:3fallos/3correctos (edicióntrasTTL, desactivar, borrartrasTTL conservaban bloque viejo). Después selección171 correctas/1 omitida6,78s; coordinador13 focales library+content correctas1,03s. Omisión exacta test_project_instructions_atomic_remember::test_permission_bits_are_preserved por Windows/POSIXbits. Sin aprobación nueva, no trustcoverageexpand ni cambios personales/modelos/GPU. Fuente original https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/agents_md.rs, no revisión repetida. Piloto biblioteca visitado/cerrado; aprobación de reglas/objetivos H21 global sigue pendiente.

H19 EN IMPLEMENTACIÓN nuevo repro real streamloop: tras cancelled durante recovery y borrar señal por consumidor, se hacía segunda llamada modelo (2vs1 esperado). Latchlocal porround antesevento evita revivir mismoturno; sólo tras prueba final cerrar. H09 EN IMPLEMENTACIÓN receiptconsultaobjetivos real, incluyendo0candidatos y strictreaderrores; JSONLstore, no atribuir SQLite alstoreobjetivos. Compilador/DBsession QA puedenSQLite; scopesarchivos separados. Uso90% permitido y automatización activa. Preservar cambios ajenos, seguir pendientes Codex.

`72694f76`: recuperación cancelada termina invocación incluso si se limpia después
la señal, sin impedir retry explícito nuevo. Coordinador2 correctas26,32s.
Piloto H19 acotado, no recibos durables/último drain completo. Fuente Codex fijada
https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9.

### VISITADO / IMPLEMENTADO — H19 cancelled worker, continuación directa

`0716884d`: tras latch72694f76, cuatro productores actuales de cancelled terminan roundloop. Worker añade cancelled a guard con puerta cerrada antesfanout; cola previa/retry intactos. No ACK aplicado ni durable. Selección final73 correctas33,24s lifecycle/rutas/board/retryusage; coordinador21 lifecycle correctas3,05s. Guard presupuesto AST se adapta a condición compactionORrecovery_cancelled conservando assertbreak. Sin modelos/GPU/redpersonal, agentloop no editado en este lote. Cancelled previo sóloevaluado queda implementado para productores actuales; preúltimadrain/recibosdurables/restart siguen pendientes. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9, revisión upstream no repetida.

`188bc6de` documenta biblioteca8ab116b9, `f770fb6c` documenta latch72694f76; coordinador2 recovery correctas26,32s sobre freeze. Ambos checkpoints conservan límites. Uso90% permitido, automatizaciónactiva. H21snapshotaprobado y H09objetivos siguen en implementación; no cerrarantespruebas. Próximo H19receipt sólo evaluaciónscope, no implementado aún. Cambiosajenos preservados.

### VISITADO / IMPLEMENTADO — H09 consulta objetivos

`42e6d978`: reutilización del contexto exige recibo privado de consulta ObjectivesSource real, capturado despuésgather del compilador incluyendo cero candidatos. Mantiene request/secciones/lanes/limit/query/scope y digest canónico de SourceResult.candidates salvo candidate_id aleatorio. Revalidación repite misma consulta/guardas/top; actualscope comparado, no ampliar ni adivinar ausencia desdepacket. Error/unavailable/timeout/corruptJSONL ⇒ unknown/recompila; strict reader sólo durantecapture/revalidate no mueve original corrupto. ContextVar calllocal restaurado; >16queries invalida sinretenerprefijo autoritativo. Legacy/manualobjective sinbacking recompila. Tresfixtures fakecompiler declaran noobjetivos mediantehook privado real, assertionsintactos.

Selecciónfinal286 correctas18,87s; coordinador15 nuevas correctas1,41s. QA JSONL real apply/save/load, ObjectivesSource/compilador/ledgerSQLite y sources: unchanged reutiliza mismo mensaje/1rowledger;0→goal, mismo timestamp/cambiotítulo/status, entrar/salirtop, borrado, corrupción/recuperación, readerror/unavailable, deadline/concurrencia/scope/overflowreset. Objetivos son JSONL, no atribuirlesSQLite. NoGPU/modelos/APIpersonal. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9, revisión upstream no repetida.

LÍMITES: recibo de resultado deconsulta antestransformación/budget, no entregauniversal. Nuevoobjfuera top estable no invalida; entrar al top sí. notplanned tuplevacía no reevalúa plannerconfig/availabilityepoch que cambieplan. No freshnessuniversal de memoria/documentos ni config, revalidación y compilación deadlinesseparados; mutación posterior aúnposible. Pilotoobjetivos visitado/cerrado; siguientes fuentes H09 sólo evaluaciónscope. `6b7de48e` documenta0716884d. H21approvalsnapshot/H19receipts siguenenimplementación. Uso90% permitido y automatizaciónactiva, cambiosajenos preservados.

### VISITADO / IMPLEMENTADO — H21 aprobación conjunta

`a327ccd3`: file_parts captura instrucciones y proyección de reglas locales/heredadas; digestv2 con reglas y metadatos/estado, v1exacto sinreglas. Snapshot frozen contiene ProjectRules capturados; caller real del prompt renderiza mismosobjetos aprobados, no relectura. Biblioteca de app fuera aprobación. Aprobaciónv1 de carpeta con reglas queda insuficiente/changed; rules-only requiere aprobación, ask no autoeleva esa cobertura aunque folderconocido. off/degraded/failopen históricos se conservan explícitos, no garantía de protección bajo esosmodos. No objetivosmutableapp en digest.

Revisión backend v2 muestra fuentescapturadas instrucciones+reglas y ancestorroots sin corte16; truncación32k explícita se conserva, no prometer todo contenido visible. v1reviewlegacy intacto. Selecciónfinal137 correctas5,54s trust/instructions/rules/cache/prompt/compactor; coordinador12 nuevas correctas1,20s: v1exacto, migraciónlegacy, rules-only ask/strict/cambio/readerror, snapshotrender y vacío, offlegacy, mdcprojection, ancestor18, capturedreview/truncación y promptreal con/sinaprobación. Sin LLM/GPU ni configuraciónpersonal. Compactor ya usa verdict conjunto para instrucciones, no renderiza reglas y no requirió edit.

LÍMITES: discovery máx40reglas/límite64KiB, se aprueba proyección acotada/decodificada y errores, no archivo íntegro ni transacción atómica del directorio. Jerarquía completa de instrucciones y objetivos con clave propia pendientes. Policy failopen/off preexistente no convertido en failclosed. Fuentes https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/agents_md.rs y contratoslocales; no upstreamrevisión repetida. Piloto visitado/cerrado. Uso90% permitido; automatizaciónactiva, cambiosajenos preservados. H09memory/H19receipt en implementación, no cerrarantespruebas.

### VISITADO / IMPLEMENTADO — H19 recibos de cola fase1

`bfd5992a`: _EffectRecorder del run padre exacto capturado antes dispatch registra queued/drained/dropped con UUID por aceptación y provenance de worker/delegación/attempt. JournalJSONL+fsync, sinbody/texto; no API AgentRunRecorderSQLite imaginaria. queuedobserva entry ya enlazada y se registra antesreturn sinawait; sidecarid(entry) identifica objeto encola sinmatchtexto/FIFO. Drained identifica retirada, no applied/read. Drop sólo finalcleanup común workers/reviewer despuésretries, vacía cola/sidetables; intento no borra pendientes. Historia livecap1024 conserva IDs de entradas pendientes; journal completo. Run padre reemplazado no migra recibos ni alimenta nuevos suscriptores. Sinrecorder/writerNone/closed/fsync/missingattempt: durabilidadunknown y boolTrue sólo queueadmission.

Final90 correctas45,42s incluyendo14nuevasreceipt+lifecycle/recorder/causal/dispatch/rutas/retry; coordinador14 nuevas2,72s. Coordinador catálogoSSE6 correctas0,75s tras añadir steering_receipt al contrato, fuenteJSONsólo raíz. SinLLM/GPU, mensajesexternos ni configuraciónpersonal. No HTTPnuevo, restoredqueue/applied/durableinstructiondelivery no implementado aquí. Applied exactID etapa2 sólo propuesta; aplicarse a mensajes no equivaldrá modelread/effectsuccess. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9, contratoslocales _RunLog yrun_causality, no revisionesupstream repetidas. Piloto fase1 visitado/cerrado; H19 globalparcial. Uso92% permitido, automatizaciónactiva, ajenos preservados; H05MCP/H09memoria siguenenimplementación.

### VISITADO / IMPLEMENTADO — H09 personal y H19 aplicación

`769e39a1`: consulta PersonalMemorySource real capturada/revalidada antes reuse, incluyendo ausencia/top/proyección/owner. Texto puede cambiar con mismotimestamp, ahora invalida. MemoryManager loader owner-filter estricto sin migración y constructor opt-in create_if_missing=False sólo validación: sin archivo/directorio nuevo/uses incrementados. APIs legacy defaults conservados; compile inicial fallbacklegacy conserva texto owner-filtered degradado y receiptunknown, no lenient-emptycertificado. Incognito/sourceoff antesstore. ContextVar reset/overflow desconocido y mismosdeadline/scope. Tresfixturescompat fakecompilers señalan no consulta personal por hook real, assertsintactos. Selección240 correctas13,65s y66 correctas3,64s con17solapadas; son289distintas, no ejecuciónúnica289. Coordinador17nuevas correctas1,33s. TempJSON real+MemoryManager+adapter/compilador/ledgerSQLite: mismosIDtimestamp/edit,0→new,borrado,topstable,ownerhidden noleak/invalidation,corruptpreservado/readerror/legacy/deadline/concurrentowners/missingfile/parent/uses0. NoGPU/APIpersonal. MemEngine/Chroma/docs/plannerconfigurationepoch y mutación posterior pendientes, no freshnessuniversal.

`4dc501a1`: IDUUID32 interno pasa al SSEsteer sólo después messages.append; callbackworker optin preserva publicpendinglegacyshape, no IDencontenido delmodelo. applied journal exige IDinflight exactworker/drained/currentattempt; duplicado/forged/crossworker/oldattempt/undrained noapplied. Historialevicted no elimina prueba inflight; attemptfinally limpia prueba nofalse dropped/requeue. Applied significa mensaje añadido, NO modelo leyó/ejecutó/éxito. Closedwriter observaciónapplied volatiledurabilityunknown, journalqueued/drainedsinfsyncclaim. Final104 correctas46,09s; coordinador28recipts correctas2,89s. SSEcatalog actualizadoapplied/attempt ysteer optionalID por raíz;6 correctas0,79s. No HTTP/UInew ni restauracióncola.

Fuentes https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 ycontratoslocales; no repetida revisiónupstream. Pilotos cerrados acotados, H09/H19 globalesparciales. Uso92% permitido, automatizaciónactiva y ajenos preservados.

### VISITADO / IMPLEMENTADO — H05 MCP preflight y argumentos

`d223b67f`: metadata/session/connectionscope privado capturado antesOAuthawait; después revalidar sesión/registro/owner-task/OAuthprovider/browserpolicy vigente. Drift predispatch devuelve denied/notdispatched sinadoptarB; fallodespuésdispatch conderiva conservaoutcome_unknown sin desconectar sesiónB. Replay de escriturasinciertas noejecutado; readonlyretry exige contratoconfiguración/política capturados. Repro anterior AwriteFalsehints→metadataBreadonly duranteOAuth terminaba A+B/éxito0; corregido. Coordinador28focales0,85s; selección100 correctas5,50s contenía27nuevas antescasoúltimo;18normalizer/STDIOfake correctas4,43s con1Playwright deselected, no browserreal.

`b18803cc`: argumentos deepcopy al entrar manager anteslazy/OAuthawait; copiacadaenvío desdecaptura, mutaciónnestedcalleroSDK no cambiacall/retry. ReproantesOAuth enviómutated_B pesecaptured_A. Final105 correctas5,57s (32bindingcasos); coordinador32focales0,87s. No effects externos ni modelos. Scope local manager: no snapshotglobalporpaso ni prueba advertisedschema; owner/disabledmap general permanececaller, configuraciónDB sinpropagación no cubierta/lease. Heurísticasreadonly/browserrecovery previas conservadas; OAuthproviderinstancianueva niegareplayconservador aunmismaaccount. Nuevoserrores no exponen secrets. Fuentes https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/registry.rs ycontratoslocales, sinrevisiónupstream repetida. Pilotoscerrados; próximosscopes fuentesdocumentales/schemaadvertising/receiptstatus sóloevaluación. `c2528aa0` documentareceiptsfase1bfd5992a; registrosactualizados.


### VISITADO / IMPLEMENTADO — H19 consulta de recibos tras reinicio, 30-09-2026

Commit `ca616048`: POST de steering acepta `return_receipt: true` para devolver el locator capturado antes del dispatch; el cuerpo legacy mantiene exactamente `{ok: true}`. GET `/api/chat/subagent/steering-receipts/{child_session_id}/{receipt_id}?parent_session_id=...&parent_run_id=...` verifica propietarios de sesión hija y padre antes de leer archivos. Consulta el journal del run exacto mediante prefijo de sesión y UUID, sin depender de `_RUNS`, seleccionar el más reciente ni adivinar desde transcripciones.

El lector limita a 8 MiB/100000 líneas y valida cabecera, IDs, secuencia queued→drained→applied o queued→dropped e identidad de worker/delegación/attempt. Archivo ausente, error, cola JSONL truncada, incoherencia o exceso devuelve `unknown`. Un recibo ajeno devuelve 404 sólo tras validar el journal completo. Evidencia `journal` y durabilidad `unknown`: bytes presentes no demuestran fsync. Applied significa añadido a mensajes, nunca lectura del modelo o éxito de efecto. No restaura colas, reenvía ni añade UI.

Pruebas finales: 101 correctas en 32,90 s, incluyendo 30 casos nuevos de API/lector con TestClient, propietarios SQLite reales y archivos reabiertos; autenticación antes de IO, aislamiento, pérdida/corrupción, límites y compatibilidad. Coordinador repite 30 nuevas: correctas en 2,57 s. Sin inferencia ni GPU. Fuente original https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y contrato local de `_RunLog`; no revisión upstream repetida. Este piloto queda visitado/cerrado; aceptación antes del último drain y recuperación operativa siguen pendientes. Uso 93% consumido, todavía permitido; automatización activa. Consolidación total de imágenes y restantes fuentes H09/H05 siguen parciales; cambios ajenos preservados.


### VISITADO / IMPLEMENTADO — H09 permiso de documentos personales, 30-09-2026

`d71a6b10`: DocumentSource comprueba `allow_personal_memory` y propietario antes de abrir/consultar su almacén, tanto en búsqueda async como llamada síncrona directa. Un manager ya abierto tampoco se consulta con política desactivada. Planner clasifica `documents` como fuente personal y la excluye cuando se deniega esa política. Conserva consulta, límite y filtro owner de la búsqueda permitida; no se incorpora filtro de proyecto inexistente.

Antes: 4 regresiones fallaban y una pasaba (apertura denegada/owner vacío y documentos ofrecidos con política personal desactivada). Después: 210 pruebas correctas en 13,67 s, incluyendo 8 nuevas. El fixture anterior que esperaba documentos personales permitidos en incognito se actualiza al contrato de privacidad y conserva asserts de causa y caso positivo. Coordinador: 8 nuevas correctas en 0,79 s. Fuente real adapter/planner con manager sintético; no prueba de Chroma ni inferencia/GPU. `available()` conserva imports previos: la garantía es no abrir/consultar el store, no ausencia universal de IO de imports.

Fuente original https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y contratos locales; revisión upstream no repetida. Piloto de permisos visitado/cerrado. Freshness documental y consulta estricta siguen PENDIENTES: collection.count con OSError puede acabar count=0/query_lanes=[] y healthy=True, reproducido con EmbeddingLane/query_lanes reales y collection sintética. Runtime Chroma instalado es cliente HTTP; PersistentClient temporal rechazado. No certificar vacío ni revalidación hasta resolver ruta estricta y QA aislada real. Sin activar servicios personales. Uso 94% permitido, automatización activa; preservar cambios ajenos.


### VISITADO / IMPLEMENTADO — H05/H06 argumentos del candidato que respondió, 30-09-2026

`1c854de1`: preparación copia profundamente las herramientas aun con slimming desactivado y captura parámetros serializados inmutables por candidato/ronda. Validación y reparación de builtins usan esa misma proyección preparada para el candidato que respondió; eliminar o cambiar su definición en el catálogo live no cambia el contrato de argumentos. Captura ausente, obsoleta, textual, duplicada o herramienta no ofrecida deja recibo explícito de compatibilidad legacy, sin sustituir candidato cero. PDF y MCP mantienen sus contratos separados; schemas suministrados para nombres desconocidos no crean autoridad ni un nuevo validador.

Repro: write_file ofrecía content:string, una mutación live durante espera lo convertía en boolean; antes se intentaba reparar 'false' a False y el convertidor fallaba. El test de fallback usa stream_agent_loop, preparación y handler real de escritura: candidato1 con contrato string escribe el texto literal false pese a mutaciones del origen preparado y catálogo global. Además cubre aislamiento anidado, definición live eliminada, round/candidato errado, duplicate/text-only/legacy y propagación del recibo a tool_output y metrics.tool_events sin parámetros, argumentos ni owner.

Final: 126 pruebas correctas en 11,42 s en 12 suites, 16 nuevas; coordinador repite 16 nuevas correctas en 3,09 s. LLM sintético/archivo temporal real, sin GPU ni proveedor externo. Recibo stage=candidate_prepared, scope=argument_validation: llm_core puede adaptar el protocolo después; no prueba universal del schema visto por el proveedor, lease de ejecutor ni autorización general por paso. No se encontró escritor UI actual del catálogo builtin: el repro ejercita mutabilidad expuesta y actualización durante await, no se atribuye un incidente UI observado. Referencia https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/registry.rs, ya visitada. Piloto cerrado; H05/H06 globales parciales.


### VISITADO / IMPLEMENTADO — H19 forma estricta del journal

`875a51e2`: reader rechaza seq boolean/negativo y provenance mal formada (worker/delegación/attempts escalares, source user/supervisor). Antes, `seq=True` y source objeto podían presentarse como queued válido y filtrar una estructura inesperada a la API. Ahora devuelve unknown/durability unknown; no añade requisitos UUID a aliases de productores que no los tenían. Final 86 pruebas correctas en 7,07 s, incluyendo 47 API/reader (17 nuevas), receipts y efectos. Sin cambios de cola/budget/rutas ni promesa de fsync.

EVALUADO último drain: loop y worker reales con LLM/tools sintéticos aceptan una entrada después de última lectura, model_calls=1/steered=0; cleanup final vacía cola y journal reabierto informa dropped. Accepted significa queue admission y el descarte queda explícito; no se añade una ronda ni se eleva presupuesto. No declarar aplicado ni cerrar recuperación operativa global. Fuente original https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y _RunLog local, sin revisar upstream otra vez.


### VISITADO / EVALUADO — H21 clave de objetivos y verificaciones finales

La ruta real services.projects.instructions_for_session→ProjectStore.system_block→services.objectives.objectives_block→load_state relee JSONL bajo su guarda. Repro con ProjectStore temporal: título AAAA→BBBB conservando tamaño, mtime_ns y updated_at; nuevo visible y antiguo eliminado. No hay caché del render de objetivos en esa ruta: ProjectStore._cache es catálogo projects.json. No implementación ni commit de código justificados; no cerrar snapshots/atomicidad universales ni volver a revisar ObjectivesSource ya cerrado.

Coordinador confirma 47 API/reader de875a51e2 en3,83s y catálogoSSE6 en0,78s tras añadir schema_validation_receipt opcional. Checkpoint documental9b7f8971 registra1c854de1/875a51e2 y contrato. Referencia original https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9, método local existente; sin revisión upstream repetida. QA Chroma estricta en implementación aislada, no certificarla antes resultado. Automatización activa, cambios ajenos preservados.

Verificación conjunta del coordinador: 188 pruebas correctas en19,77s (candidate_argument_schema_snapshot, tool_schema_receipts, document_source_policy, context_engine_compiler, steering_receipt_status, worker_steering_receipts, sse_catalog). Comprueba convivencia de pilotos cerrados; no suite universal ni certificación del nuevo backend estricto en implementación.


### VISITADO / IMPLEMENTADO — H09 consulta documental estricta, 30-09-2026

`4c69f252`: API opcional strict en RAGManager/VectorRAG y query_lanes_strict. Captura referencias de lanes/clientes/colecciones por llamada, count directo una vez por lane y formato de respuesta validado. Count cero conocido omite query; ausencia de lanes, fallo count/encode/query o respuesta mal formada de cualquiera lanza error, incluso si otra lane respondió. No interpreta excepción como colección vacía ni recurre al fallback keyword en strict. Conserva los defaults legacy y cálculo de límite/fusión/owner vigentes; el wrapper no añade nuevo keyword en llamadas legacy.

Selección Faustus: 67 correctas/2 omitidas en7,17s (una requiere Chroma full frente al cliente HTTP local; otra omisión legacy preexistente). QA aislada Chroma1.5.9 full: 19 correctas en0,65s, con PersistentClient temporal y RAGManager/VectorRAG reales, propietario/ausencia/edición/borrado, colección inválida y fallo de segunda lane tras respuesta real válida. Coordinador repite script:19 correctas en0,66s, tres avisos de opciones pytest de plugins no cargados; no fallos. No servicio HTTP ni cambios en Faustusenv.

QA reproducible: D:/LocalAI/tmp/chroma-strict-qa-20260930/run_strict_qa.py con venv/Scripts/python.exe de esa carpeta; evidencia strict-qa-result.json fija runtime/version/ruta y resultados. Final usa vectors explícitos3D y encoder determinista. Un intento anterior de fixture omitió embeddings en update, ejecutó MiniLM por defecto con cache existente y falló dimensión384vs3; corregido antes resultado final. No afirmar ausencia total de inferencia en aquel intento; no progreso de descarga observado.

Límites: referencias capturadas no convierten count→query en transacción atómica ni sellan namespace global. Metadata None se considera desconocida conservadoramente. DocumentSource todavía NO usa recibos de freshness ni strict por defecto; este tramo es prerequisito implementado/probado, no cierre documental universal. Referencias https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y código local lanes/RAG; no revisión upstream repetida. Scope4files sólo3fuentes+testnuevo, cambios ajenos preservados. Checkpoint361effbc registra objetivos evaluados y188integradas. Automatización activa mientras uso permitido.


### VISITADO / EVALUADO — H04 dato MCP success:false

Repro con Session sintética→McpManager._do_call→execute_tool_block→run/journal temporal reales: JSON success:false con isError:false termina exit0/succeeded y efecto confirmado tras un único dispatch. Esto no demuestra fallo del protocolo: success puede ser dato de negocio e isError:false indica ejecución MCP correcta. Sin productor concreto cuyo contrato dé a ese campo semántica de fallo, no se cambia clasificación global ni se leen keywords del texto. Piloto sólo evaluado, sin implementación/commit de código; futura adaptación exige contrato por backend. No servicios externos ni modelos contactados. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y contratos MCP locales, no revisión upstream repetida.

Checkpoint56061921 documenta4c69f252 y QA Chroma. Siguiente1C freshness documental EN IMPLEMENTACIÓN: recibos de consulta estricta actual con identidad privada runtime capturada y revalidación sin constructor/migración. No declarar cierre hasta QAfinal. H17 error autoridad lookup EN IMPLEMENTACIÓN, H14 auxiliares sólo evaluación. Uso permitido y automatización activa, imágenesconsolidaciónglobalparcial y ajenos preservados.


### VISITADO / IMPLEMENTADO — H17 autoridad de permisos no disponible

`a1b488e1`: is_permitted devuelve False cuando tool_policy.blocks lanza excepción. Antes la trataba como permiso concedido: lookup real ofrecía schema/promoción de read_file mientras dispatcher real con la misma policy fallaba antes del handler. Ahora schema/catalog/categories/hints/audit/fallback y aliases no ofrecen decisiones no verificables; otras herramientas con permiso resuelto siguen disponibles. Policy None deliberada y ToolPolicy normal permisiva mantienen compatibilidad.

Antesfix:9 nuevas fallaban/2 positivas pasaban. Final11 correctas0,91s y43 integración lookup/index/alias/audit correctas3,02s; coordinador11 correctas0,87s. Selección ampliada119nodes/7suites interrumpida con Ctrl-C sólo en sesiónQA propia tras76dots sin avance visible, exit1 sin traceback: NO se certifica suite completa ni fallo de assertion o deadlock del producto. Nodo activo inferido por orden en-live-failure de offer_execute_coherence; ese parametrizado ejecuta un stream sintético por cada herramienta anunciada, potencial cientos. Pruebas originales intactas.

Scope sólo tool_discovery.py+test nuevo. Autoridad fallida sintética y dispatcher/lookup reales; perfil normal puro/frozen sin incidente UI demostrado. Sin LLM/GPU/servicios externos ni nuevo flujo de aprobación. Referencia https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/registry.rs y contratos locales; no revisión upstream repetida. Piloto cerrado, H17 superficies generales y permisos cambiantes todavía parcial. Checkpointfe1779bc registra evaluación MCP success:false; documentosfreshness/recoveryusage siguen en implementación, uso95% permitido.


### VISITADO / IMPLEMENTADO — H11 lectura acotada del log en segundo plano

`dad52508`: bg_jobs._read_output usa un descriptor con tamaño inicial y ventanas de principio/final; conserva límite16000caracteres y marcador. Antes Path.read_bytes decodificaba archivo completo en cada get/followup. ChildPython temporal real escribió8MiB/exit0: salida16015caracteres con peak16.778.061bytes antes. Después misma salida,2lecturas total<=128033bytes/max64017 y peak<1MiB. Mantiene _read_job_text para código de salida.

Final70 correctas7,13s en5suites,36nuevas; coordinador36 correctas1,33s. UTF8sig/UTF16LE-BE, BOM integrado como contenido en tail, emoji pequeño15999caracteres, fronteras/surrogates, malformed y crecimiento/reducción durantelectura. Errores iniciales de nombres de fixtures demasiado largos en Windows corregidos con IDs cortos; no bugdelproducto. SinLLM/GPU/procesosajenos. No limita archivo en disco ni garantiza snapshot atómico frente escritor externo, no stdoutdrain nuevo ni solución stdin/handles durables.

Fuente original https://github.com/autonomous-ai/openharness y https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9, métodos/localcontratos previamentevisitados; adaptación propia, no revisión documental repetida. Piloto visitado/cerrado, H11global parcial. Scope2filespropios, cambiosajenos preservados, uso permitido y automatización activa.


### VISITADO / IMPLEMENTADO — H09 recibos de consulta Documents y adaptador vigente

`0f710cf9`: compiler captura en task padre el resultado normalizado de consultas DocumentSource estrictas reales, incluyendo vacío; recibo privado liga request/query/top/owner/proyección al manager y runtime de lanes/colecciones/clientes. Validación usa el manager capturado sin RAGconstructor/migración/reindex/uses. Captura identidad antes/después: URL/credenciales sólo hashesprivados, model/dim/bounds y referencias del transporte/colección materializadas; no getters que infieran/carguen modelos. Error/degraded/timeout/cuerpo manual sin backing/overflow>16 ⇒ unknown/recompila. Fuera de capture legacy igual; initial strictfailure puede render fallback existente degradado y sin recibo válido. Gates política/owner siguen antesstore. Tresfixtures fakecompiler explicitan fuente no planeada por hook real.

Before: compilador+SQLiteledger+Chroma real con wiring4c69f252 conservaba documento viejo tras cambiar cuerpo mismoID. Final286 correctas/2omitidas16,04s (ambas pruebas Chroma full no disponibles en Faustusenv thin); QA40 correctas1,42s. Coordinador40 correctas1,41s. Consulta real prueba reuse sin cambios/0→nuevo/update y borrado colección; unit/integración añade topmembership/otroowner/fallo parcial/configidentidad/timeouts/legacy/noIO/manual/concurrencia.

Pregunta nueva y followup `169c5bd2`: cambiar originaladapter._manager podía revalidar manager antiguo aún intacto. Reprocompiler+SQLite confirmado stale reuse. Recibo ahora conserva adaptador original; verifica su manager antesparent/workerquery/afterquery/aftergather sin abrir store para guard. Reemplazo manager por otro (mismoowner/otroowner), cambio antesencode y despuésquery ⇒ unknown/no oldquery o resultado descartado. Final133 correctas/1omitida5,79s; QA44 correctas1,47s; coordinador44 correctas1,45s/2avisos de opciones timeoutplugins no cargados. 25Documentcases+19strictRAG en QA. Script D:/LocalAI/tmp/chroma-strict-qa-20260930/run_document_qa.py y document-qa-result.json, repro previo test_document_before.py/document-before-result.json. Scope8filesprimerlote+3followup sólopropios.

Límites: snapshot de consulta y selección, no versión universal de toda colección. Documento fueraquery/top/owner puede no invalidar. Pool/factories reemplazados y planificación/configDB no recargada siguen sin epoch universal; no atomicidad entre checks y encode/consulta ni proveedorinternals. Owner-only de Documents conservado, projectid sólo scopecompilador, no filtrotabla añadido. Sin servicios HTTP ni GPU, QAfinal vectors3D explícitos. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y contratoslocales, upstream no re-revisado. Pilotos visitados/cerrados, H09MemEngine y entrega/transversal pendientes. Uso95% permitido y automatización activa.


### VISITADO / IMPLEMENTADO — H14 consumo de recuperación

`28385388`: observer privado opcional step_completion→ladder→ambos callers captura último valor válido por campo/step, sin sumar snapshots acumulativos repetidos. Error/cancel conserva observación en finally; partial/cost-only no borra tokens previos ni coste conocido. Retorno4tuple legacy intacto. Recovery_usage separado de buckets principales/compactor y cargo al ledger presupuestario del turno; coste desconocido no se inventa. Gate antes de same-model/utility usa política vigente y presupuesto observado; agotamiento termina caller/round sin tools ni nueva inferencia. ASTlifecycle incorpora dos nuevas emisiones (6total) conservando4anteriores y comprueba latch antesfanout/outerbreak.

Repro real loop/ladder/helper con proveedor sintético: usage800input/200output descartado antes (ledger0), ahora ledger1000/receipt1000. Ambos callers degenerate/ctx_ack con step2 de100tokens alcanzan cuota: utilidad/roundposterior/tools no despachan. Final53 correctas17,25s (30nuevas+lifecycle21+cost2); coordinador30 nuevas14,37s. Amplia previa98 correctas/1 fallo197,85s: único fallo era assert estructural de4emisiones desactualizado, corregido y repetido final; selección71 correctas103,29s tenía26nuevas antescontrolesfinales, no freeze global. Scope3filespropios.

Límites: recuperación local excluida de cargo/receipt según política existente; turno originalmente local conserva grant ilimitado aunque utility sea remota (su consumo remoto sí observado/cargado, no límiteinventado). No reserva previa, unión de cuentas ni factura universal para retries internos sin identidad. Métricas principales mantienen su significado, consumo recovery aparte. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y auxiliareslocales ya visitados. Nuevo timing de uso principal antes recovery sólo evaluación, no cerrado. Sin modelos/GPU reales ni cambios personales.


### VISITADO / IMPLEMENTADO — H11 cancelación recoge hijo CodeMode

`da7412f6`: CancelledError en run_code_mode ya no salta limpieza posterior. Termina sólo proceso hijo directo capturado, cancela/espera pump con límite y reap acotado; propaga CancelledError. Repro childPython temporal propio leyendo config y sleep30: antes taskcancelled=True/childalive=True; después child recogido, pump no huérfano. Sin kill de procesos ajenos ni árbol de PIDs supuesto, sin cambio de cuotas/protocolo/drenajes.

Final2nuevas0,78s con warnings-as-errors y gc explícito,32regresiones14,33s con1PytestUnraisableExceptionWarning WindowsProactor en approval_pause de causa no determinada (sin causalidad baseline certificada; no esconderla). Coordinador2nuevas-Werror0,80s correctas. Errorfixture accidental importgc enchild en vezjson produjo readiness timeout duranteQA; fixturecorregidoantesfinal, no bugproducto. Sólo runner.py+testnuevo. No garantías sobre descendientes, cancelacionesrepetidas o callbacks que ignorencancelación; stdin/handlesdurables generales pendientes. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y runtimehostlocal; métodosvisitados no repetidos.


### VISITADO / IMPLEMENTADO — H09 prerequisito MemoryEngine standing read-only

`7e13c7f6`: engine.context_snapshot(owner,project,statuses,now) abre SQLite mode=ro/query_only/BEGIN y obtiene publicprojections de filas y conflictos en una misma transacción. Strict conflicto usa conexión del caller y propaga errores; defaultslegacy conservados. SchemaSELECT incluso vacío evita certificar tabla ausente/incompatible. No _db/_open/_connect de writer, mkdir,DDL/DML,migrate,quarantine,uses ni vector enstrict. Filtrosactuales owner=? OR global'' y project=? OR global'' conservados; no filtro de sesión inventado.

Final211 correctas18,63s en6módulos,15nuevas; coordinador15 correctas1,19s. SQLite real: DELETEfixture bytes/archivosintactos; corrupt/missingstore/tabla/conflicts/errores noemptycertification; fila/conflicto coherentes bajo otrocommit durante lectura. WALwriter abierto con nuevo texto commit sincheckpoint: reader veúltimocommit, DBprincipalbytesintactos. WALwriter cerrado sin sidecars: reader puedecrear operativos -wal/-shm; se ajusta requisito anteriornojournals a evidencia. No immutable=1, que podría omitir WAL y leer estado obsoleto. Garantía de no escrituras de estado de app, NO ceroactividadfilesystem/archivosauxiliares. Sin limpiar sidecars personales manualmente.

Repro anterior de staleMemoryEngine válido: fuente standing, compilador/SQLite reales, regla procedural original→changed mismoID/updated_at; second reused=True/texto viejo. Intento previo code_change sin fuente renderizada descartado como prueba inválida. Este commit implementa sólo APIprimitiva; adaptador/recibo standing/hybrid freshness NOcerrados. Siguiente standingreceipt en evaluación, hybridestricto vectorstore pendiente; cliente fullChromaQAaislado ya disponible no servidoHTTP. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y memoryengine/conflictslocales; no revisión upstream repetida. Piloto prerequisito visitado/cerrado, globalH09parcial. Checkpoint8629ce96 recovery28385388/cancelda7412f6, uso96% permitido y automatización activa, ajenos preservados.


### VISITADO / IMPLEMENTADO — H09 vigencia y secretos en memoria standing

`c4de4930`: selección de reglas/anti-patterns sin consulta ya excluye sensitivity=secret y filas fuera de valid_from/valid_until, con instanteUTC actual compartido por selección y score. Sigue los guardas de la ruta híbrida y conserva anti-patterns sin filtro de score, ownerblank global-only y políticaoff antesDB. Beforeactualgather+SQLite:3fallos secret/expired/future entraban en contexto y2positivos correctos; final183 correctas17,68s en5módulos,5nuevas. Coordinador5nuevas0,88s correctas. No reloj congelado para revalidation, no cambio de permisos del usuario ni filtro de sesión inventado.

Este lote2filesadapter/test cierra guard de selección, NO standing freshness aún: recibo enimplementación. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y contratos MemoryEngine híbridoslocales; no revisión upstream repetida.


### VISITADO / IMPLEMENTADO — H14 admisión incluye consumo principal pendiente

`2d32dcf8`: gate antes recuperación usa copia del ledger más uso principal observado aún no liquidado, por invocación outerstream/round. Últimos campos válidos acumulados sin duplicar snapshots; max(total,componentes) evita infravalorar total obsoleto. Settlement descuenta sólo créditos efectivamente añadidos por el cargo legacy, por dimensión tokens/spend; total-only1000 con cargo230 deja residual770. No muta ledger/buckets/métricas principales ni fabrica factura de reintentos internos.

Repro loopreal con proveedor sintético en amboscallers: mainusage1000 conbudget500 permitía recuperar viendoledger0; ahora no despachaaux ni efectos. Caso bajo límite admite; roundliquidado30+pending20 bajo65 no duplica30. Final73 correctas20,63s (20nuevas+30observer+21lifecycle+2cost); coordinador20nuevas3,57s. Source sóloagent_loop+newtests, aliases/localgrant vigentes. Sólo gate antes recovery: cargo principal omitido en algunas rutas, admisión de primeros reintentos principales e identidad wrapper interna siguen pendientes/en evaluación. No reserva global ni ledgergeneralcompleto. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 ya visitada; notupstreamrerun.


### VISITADO / IMPLEMENTADO — recibo de consulta MemoryEngine standing

`99d27ee5`: la rama standing captura el resultado normalizado de la consulta (incluido vacío) después del worker y revalida filas/conflictos con context_snapshot estricto antes de reutilizar. Cambio de texto conservando ID/updated_at y vacío→nuevo ya fuerzan recompilación. Captura y valida ruta de DB antes/después; vigencia usa reloj actual. No writer, vector, migración, reindexación ni uses durante revalidación. Fallo inicial puede conservar respuesta legacy degradada, pero el recibo queda desconocido; manual mem, error, timeout y >16 consultas no certifican reutilización.

Final del agente: 300 correctas y 1 omisión en18,37s, 11 módulos, 16 nuevas. Coordinador: standing16 + mainretry18, **34 correctas en29,57s**. SQLite/compilador reales temporales; sin modelos ni DB personal. Tres fixtures declaran fuentes no planificadas y referencias genéricas usan fixture: para no fingir recibos mem:. Fuente normativa/default MemoryEngineSource, sin promesa para subclases arbitrarias ni epochs globales de pools/configuración. SQLite puede usar sidecars WAL/SHM; no escritura de estado de aplicación.

Piloto standing cerrado; **consulta híbrida sigue desconocida**. Nueva evaluación real: MemoryVector devuelve parcial/vacío ante error de una lane; petición lanes=('lexical',) consulta vector store igualmente. Primitiva strict opt-in en implementación separada; política lexical-only pendiente. H09 general parcial (versiones/configuración universales y entrega efectiva pendientes). Fuente original ya visitada: https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; adaptación propia, sin repetir radar41.


### VISITADO / IMPLEMENTADO — fase explícita de auxiliares en trazas

`8889446f`: ContextVar limitado a compaction/recovery y step2/3 identifica únicamente llamadas efectivamente realizadas. Scope alrededor de inferencia; reset ante excepción/cancel, anidación y concurrencia. JSONL, listado y detalle conservan phase/step opcionales. Históricos sin fase quedan desconocidos; no se infiere foreground ni se inventan llamadas para compactación determinista. Uso/coste no se suman aquí ni se copian arrays por turno a todas las trazas.

13 nuevas correctas1,02s; selección final65 correctas43,52s con las13; coordinador13 correctas1,23s. Selección previa168 correctas78,64s incluía11 nuevas antes de las2 últimas: no se declara final congelada. JSONL flush/reopen real; compactador/ladder reales con inferencias sintéticas. Evaluación previa SQLite confirma que recovery_usage/compaction_usage persisten en metadata del chat; no se encontró truncamiento de esos arrays. Vista/agrupación causal y facturación universal siguen pendientes. Fuente original ya visitada: https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; adaptación propia.


### VISITADO / IMPLEMENTADO — admisión de reintentos principales

`ff0b10a3`: el gate de inicio de ronda usa ledger más observación principal pendiente. Una sola línea de producción, suite nueva18 y expectativa anterior ajustada de varios intentos a uno al denegarse más pronto. Bloquea retry ordinary/reasoning, nudge ctx_ack y tercer intento tras dos respuestas300tokens con límite500. Respeta bajo límite, cancelación entre evento/retry, local bypass y cargo previo liquidado30 sin duplicarlo.

Final91 correctas47,56s (18nuevas+20pending+30recovery+21lifecycle+2cost); coordinador34 correctas29,57s junto standing16. Sin cambios a métricas/buckets/pricing/grants ni helper de fases. **Cargo persistente principal omitido en algunas rutas e identidad de intentos internos siguen pendientes**, ahora en evaluación distinta; admisión no equivale a factura completa ni reserva global. Fuente original ya visitada: https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; no nueva revisión upstream.

Precisión del lote99d27ee5: única omisión test_document_reuse_freshness.py::test_real_chroma_compiler_freshness (requires isolated full Chroma QA runtime; entorno principal chromadb-client). Ninguna prueba standing omitida. QA documental real ya44 correctas enruntime aislado tras169c5bd2. Checkpoint documental69b1918b.


### VISITADO / IMPLEMENTADO — MemoryVector strict opt-in

`c0514371`: MemoryVectorStore.search(strict=True) reutiliza query_lanes_strict: count una vez por lane capturada, vacío sólo si count conocido0; cualquier error count/encode/query/shape rechaza resultado entero en vez de certificar parcial. Query/k válidos requeridos incluso vacío. Distancia→score, prioridad custom, ranking/dedupe y defaultlegacy conservados. Sin initialize/reconnect/migrate/reindex durante consulta strict.

Final126 correctas/1 omitida15,23s en6módulos. 24 nuevas:23 enruntime principal y1 Chroma real omitida por cliente thin (test_memory_vector_strict_queries.py::test_real_chroma_query_absence_and_partial_failure). QA aisladafullChroma1.5.9:24 correctas0,69s; coordinador24 correctas0,67s con3 warnings config asyncio_mode/timeout/timeout_method por plugins no cargados. PersistentClient temporal real, vectors3D explícitos/encoder determinista, consulta/vacío/colección borrada y lane válida junto lane fallida. Sin modelos, servicio ni DB personal. Script/artifact D:/LocalAI/tmp/chroma-strict-qa-20260930/run_memory_vector_qa.py y memory-vector-qa-result.json.

Primitiva cerrada; aún no conectada a recibo híbrido ni lectura SQLite estricta híbrida. Política lexical-only en incremento separado. Fuente original ya visitada: https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; implementación propia, no revisión upstream/radar repetida.


### VISITADO / IMPLEMENTADO — ámbito y vigencia al reabrir memoria por ID

Nueva pregunta by-ID frente a búsqueda: fetch_ref real→MemoryEngineSource.fetch→SQLite permitía owner vacío leer privado, proyecto diferente/vacío recuperar fila de otro workspace, secret y ventana no vigente. Ranking posterior no recupera scope: candidate usa proyecto del request y no lleva sensibilidad/ventanas completas. Repro corregido de fixture:6 fallos reales y6 positivos antes cambio; dos intentos previos usaron nombre de columna inexistente last_used_at/last_used y no certifican esos positivos.

`9dc7eeed`: _fetch rechaza storedowner no vacío distinto del request (también ownerblank), storedproject no vacío distinto de scope actual, secret y !is_valid_now. Globales blank siguen permitidos; workspace y fallback project_id conservados. Gate incognito antes get_item. Sin touch ni cambio acceso/last_accessed. Lote2filespropios,12nuevas; selección98 correctas3,73s (sources/standing/freshness), segunda selección con recall real + deferredphase **48 correctas2,72s**. Una selección inicial tenía test_context_engine_expand.py inexistente:0 tests, sustituida por recall existente. DB temporales reales, sin GPU/DB personal.

Se cierra guard de reabrir por ID, NO lectorreadonly de get_item (usa _db legacy), atomicidad snapshot ni epochs universales. Fuente original: https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9, contratos locales de memoria/contexto; nueva pregunta no repetición de revisión cerrada.


### VISITADO / IMPLEMENTADO — fase de trazas con cierre diferido

Nueva pregunta tras8889446f: wrapper streaming podía cerrar generador en shutdown_asyncgens después de salir scope recovery. Repro real helper/error503 con usage7/3 retenía tokens pero phase/step ausentes. `19167960`: ambos wrappers llm_core capturan tupla inmutable al entrar y la pasan explícita a record_call. Sentinel privado conserva compatibilidad registro directo, snapshot(None,None) no adopta fase de otro contexto. Sin cambio de uso/coste.

Final127 correctas44,89s en8 suites,7 nuevas (selección previa42 correctas2,18s). Helper pasos2/3+asyncgenfinalizer, earlyclose en contextoB conservaA; llamada iniciada desconocida sigue desconocida; nonstream cambia contexto dentroimpl; JSONL actualflush/list/detail y uso observado. Coordinador selección48 correctas2,72s con7 nuevas+recall+fetch12. No modelo/provider real ni UI; agrupación causal/factura general pendiente. Fuente original ya visitada https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; adaptación propia. Matiz diferido visitado/cerrado, no repetir sin regresión/version.


### VISITADO / IMPLEMENTADO — petición léxica no consulta embeddings

`1e9bca1f`: MemoryEngine.search añade semantic_enabled=True opcional. False omite tanto _semantic_scores como vector_store lazygetter, semantic0/wsem0/lexical.90/degradedFalse: omisión por política no simula avería. DefaultTrue conserva híbrido y degraded=True ante ausencia/error. MemoryEngineSource pasa sólo False cuando semantic no está permitido; llamada habilitada conserva API legacy de adapters inyectados. Sin cambios graph ni by-ID9dc7eeed.

Repro previo gather/SQLite real lanes=('lexical',): vectorstore.search1 y resultado no degradado pese vía no permitida. Final183 correctas17,76s,0omitidas: semanticpolicy7+sources+fetch12+standingfreshness16+engine. Coordinador19 correctas1,43s (7nuevas+12fetch). Recordingstore/failgetters reales de prueba comprueban cero consultas/inicialización cuando deshabilitado, positivo semantic/default, absent/error legacy y gateincognito. Sin modelos/DB personal enpytest aislado. Híbrido readonly/freshness aún pendiente; primitivastrictc051 no certifica APIengine completa.

Fuente original ya visitada https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y contratos locales del planificador; adaptación propia, no revisiónradar repetida. Piloto lexical-only visitado/cerrado. Checkpoint98272203. Uso99% pero ordinaryUsageAllowed=true; sigue autorizado, automatización activa.


### VISITADO / IMPLEMENTADO — run capturado antes de trazado diferido

Nueva pregunta análoga a phase: stream real iniciado run-A/session-A, cerrado en contexto run-B, persistía run-B/session-A. `33c845da`: ambos wrappers capturan currentrun al entrar y pasan snapshot privado explícito al registrar. Ausencia capturada no adopta run futuro; record_call directo conserva currentcontext por defecto y explicit run_id tiene precedencia legacy. No añade validación nueva de run_ids legacy ni autoridad de ejecución.

Final95 correctas4,10s en7suites,14 nuevas; coordinador14 correctas0,91s. JSONL real flush/reopen/list/detail, earlyclose y shutdown_asyncgens sobre error/partial; knownA→B, unknown→B, wrapper nonstream con contextos diferentes. Sesión explícita permanece correcta y otra sesión no recibe registros. Sin modelos ni facturas reconstruidas. Fuente original ya visitada https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; adaptación propia. Piloto run diferido visitado/cerrado, H23generalpartial.


### VISITADO / EVALUADO — H05 prepared→builder de proveedor

Builders reales con schema nested boolean/array/required/additionalProperties: Harmony conserva parameters y nombres con roundtrip; Ollama conserva contrato; Anthropic input_schema igual; OpenAIchat tools sin rewrite. Original sin mutación. No se encontró bug nuevo de tipo/required para corregir. Smalltalk puede omitir tools; subscriptionResponses también: omisión deliberada, no wireauthority certificada. Recibo actual declara candidate_prepared/shadow/notcomparable; no promesa request_sent. Sin fakePOST benchmark ni nueva implementación. Autoridad de schemas efectivamente enviados requiere piloto distinto y sigue pendiente. Fuente https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y builderslocales ya visitados. No repetir esta comparación sin nueva pregunta/version.


### VISITADO / IMPLEMENTADO — liquidación y admisión antes de herramientas

`e119cec5`: residual observado principal se liquida una vez en el ledger capturado al cerrar wrapper, y antes de métricas en cierre normal. Créditos tokens/spend separados compensan cargo legacy; fallos por dimensión no duplican otra dimensión ya añadida. Callback en _TURN_FINALIZERS existente, nestedfinally ejecuta todos aun gen.aclose con error ordinary y conserva excepción previa. Final109 correctas60,45s (19nuevas+18retry+20pending+30recovery+21lifecycle+1disconnect); nueva19 también11,86s. Terminal, error, cancel, consumerclose y cost-only/local conocidos. **Ledger sólo memoria del turno**; no journal durable ni cambio de buckets/metrics públicas, ni factura de providerattempts internos. CancelledError secundario al cerrar no garantía universal nueva.

`1f66cd71`: gate antes de cada tool usa ledger más mainobservado pendiente, sin cambiar límites/localbypass ni cargo. Repropytest aislado2FAIL2PASS13,11s: main1000 con límite500 ejecutaba read_file y paraba sólo después; now noefecto/tool_output, una sola llamada main y checkpointvacío. Control100 y local1000 aún ejecutan; snapshotrepetido1000 no duplica. Source1line funcional+comentario, nueva4tests; selección final61 correctas54,13s (4nuevas+19finalización+18retry+20pending). Selección adicional autonomía/preflight/lifecycle aún en curso al registrar: no afirmar final correcta hasta resultado.

Primer prototipo inline anterior fuera pytest intentó inicializar MemoryVector enlocalhost8100 (servicio no disponible); no certifica ausencia de acceso a datos personales. Repro decisivo/finalpytest usa conftest aislado; no herramienta real ejecutada, efectos sintéticos. No revertir datos ajenos por inferencia. Fuente original ya visitada https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; adaptaciones propias.

Nueva evaluación active_seconds: 3repros pytest/fakeclock realwrapper terminal/warmretry/ctx_ack mantienenledger0 tras120–240s proveedor, retry bajo límite100. Prototipo por-await __anext__ 3correctas2,06s:120bloquea,20+20=40admite; consumerpausas1000entreSSE no añaden gasto (techo pared independiente elevado sólofixture). Piloto timepending implementándose por dimensión con crédito elapsedlegacy, no cargo universalwall ni duraciónrecoveryaux cerrada.


### VISITADO / EVALUADO — descendientes de Code Mode en Windows

Nueva pregunta trasda7412f6: guest temporal crea nieto30s; cancelar recogechild directo pero nieto sigue vivo. Limpieza manual sólo handle Windows obtenido antescancel sobre proceso propio. process_ownership/terminate_tree actuales psutil/PIDctime best-effort, sinJobObject: no garantizan contener spawn/reparentrace. Este límite permanece; no declarar H11global cerrado.

Prototipo aislado JobObject parentowned UUIDLocal +KILL_ON_JOB_CLOSE, bootstrap Open/Assigncurrent ANTESguest y cierra su handle, parent soleholder. Closeparentjob termina child+grandchild (handlesseñalizados/directreaped), también stdinclosed. Open trasparentclose falla2/exit76 sinusercode. Assign con query-onlyhandle ennestedjob falla5/exit77 sinusercode; anidación normal estehost sí permitida. No claim fallo Windowsnested general. No modificación productos en esta evaluación.

Implementación específica runner+bootstrap+tests ahoraautorizada, pendiente commit y regresiones. JobObject no aislamientofilesystem/red ni frontera contra otros procesos del mismo usuario; jobname nosecret. No Docker ni PIDwalk como sustituto. Fuente original https://github.com/autonomous-ai/openharness y https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9, contratoslocales ya visitados; nueva pregunta/prototipo, no repetir radar41.

Verificación ampliada final1f66cd71: autonomía + preflight + steeringlifecycle, **94 correctas420,08s (7min)**, sin omisiones/fallos. No repetir tras pasar salvo cambio relevante. Checkpoint475fc3fb.


### VISITADO / IMPLEMENTADO — API híbrida de contexto sin inicialización ni escritura de estado

`85058e46`: context_search es API separada estricta con owner/project explícitos. context_snapshot(include_items=True) aporta rawitem y proyección pública desde misma transacción filas/conflictos; defaultAPI standing intacta. Query léxica/graph usa texto raw, resultado usa markerconflict público. Scoring/ranking puro compartido con legacy evita duplicarlo o alterar relevancia por markers. No _db/scoped_items writer, DDL/migrate/quarantine/touch; conserva sidecars operativos SQLite posibles.

Vector sólo runtime MemoryVectorStore/_EngineVectors ya instalado y saludable (no getter/constructor). Captura antes SQL y verifica binding, lanes/client/collection/namespace/fields antes/después de encode/query; strict porlane, unknown/fallo/cambio no certifica parcial. EmptySQL conocido requiere ningún proveedor. False semantic no consulta vector. Sin transacción atómica SQLite↔Chroma ni epochconfigglobal, sin permiso nuevo ni filtroowner Chroma inventado (filtra wanted IDs SQL como legacy).

Identidad extraída a embedding_runtime_identity.py, reutilizada por Documents con exactkeys/equality y binding de adaptador conservados. Retiene referencias incluyendo client._model, weakrefprueba de no reciclado de IDs mientras vive captura; no backend→context_engine import ni SimpleNamespacefalso ni copia de guards.

Final182 correctas/2omitidas19,90s en7módulos. Omisiones thinclient: test_memory_engine_strict_query.py::test_real_chroma_and_sqlite_read_only_hybrid y test_document_reuse_freshness.py::test_real_chroma_compiler_freshness. QA fullChroma1.5.9 realSQL+PersistentClient:70 correctas2,53s, coordinador70 correctas2,49s,2configwarnings timeout/timeout_method conocidos. 26nuevas +25Documents +19RAGstrict enQA. Script/artifact D:/LocalAI/tmp/chroma-strict-qa-20260930/run_memory_engine_query_qa.py y memory-engine-query-qa-result.json. Pruebas paridadlexical/semantic/graph/conflictmarkers, ausencia/deletecorruptstore, unknownnamespace/drift/binding, readSQL noDDL/uses, weakrefs y Documentsregresión. Sin GPU/modelos/proveedores externos ni DB personal.

**Primitiva cerrada; adapter y recibo híbrido siguen pendientes.** No afirmar freshnessglobal MemoryEngine ni sourceversionuniversales. Fuente original ya visitada https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9 y contratoslocales, adaptación propia sin nueva revisión41proyectos. Checkpoint832ba4b4. Uso100%redondeado pero ordinaryUsageAllowed=true; todavía no límite real. Agentes cerrando timeawait y JobObject, no nuevo piloto hasta QA de pendientes.


### VISITADO / IMPLEMENTADO — espera de inferencia principal en presupuesto

`6974f6c8`: wrapper main mide cada await __anext__ del proveedor y registra antes de yield/error/cancel en recibo privado por outerround. Consumerpausas entre SSE y aclose quedan fuera de ese nuevo span. Ledger/admission/flush reciben residual activo por dimensión; cargo wall legacy efectivamente añadido en la misma ronda se acredita, evitando doble conteo. Observeropcional None conserva elementoslegacy sin muestrear reloj. Cierre innerstream ordinaryerror conserva error previo.

Final113 correctas72,16s:22nuevas+19finalization+18retry+20pending+30recovery+4rootpretool intactas. Coordinador22 correctas5,03s. Fakeclock pytest aislado, proveedores sintéticos:120espera bajo100 deniega retries warm/ctx_ack ytools;20+20=40 bajo100 admite, aunque consumidor espera1000entreSSE; legacyround200incltools + terminal20=220, no240; cancel/error/close conserva37observados; créditos parciales por misma ronda y dobleflushsin duplicar.

**Alcance estrecho:** contador activo del turno en memoria; no durabilidad/invoice ni clockwall universal. Cargo legacy por ronda conserva comportamiento previo y puede incluir intervalos que éste ya contaba; piloto nuevo sólo excluye pausas del span de espera del proveedor. Recuperaciónauxiliaryduration y overhead protocol/providerinternalidentities pendientes, tampoco añade reserva previa ni cambia grantslocales. Fuente original ya visitada https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; implementaciónpropia. Piloto visitado/cerrado en esealcance, no repetir sin pregunta nueva/regresión.

Checkpoint5e89941b. JobObjectproducto en implementación, NOcerrado todavía. Uso100%redondeado y últimaordinaryUsageAllowedtrue; no se pausa antes de bloqueo real.


### VISITADO / IMPLEMENTADO — descendientes de Code Mode contenidos en JobObject propio (cierre de registro)

`58c8b7c9`: el runner de Code Mode en Windows crea un JobObject propio (nombre UUID local, `KILL_ON_JOB_CLOSE`) antes de lanzar el guest; `src/code_mode/windows_job_bootstrap.py` arranca con `python -I`, abre el job por nombre, se autoasigna y sólo entonces ejecuta el guest; si la apertura, la asignación o el ACK fallan, el código del usuario no llega a ejecutarse. El padre es el único titular del handle (no heredable). La limpieza cierra primero el handle nativo —mata hijos y nietos sin recorrer PIDs registrados—, recoge el hijo directo, cierra stdin y drena stdout/stderr sin retener salida para que el transporte no se quede esperando EOF. Cancelación temprana (antes del pump o del ACK), fallo de creación, fallo de spawn y fallo de configuración cierran el job.

Pruebas re-ejecutadas 30-09-2026 sobre `58c8b7c9` en este PC: `tests/test_code_mode_windows_job.py` **13 correctas con `-W error`, 2,71 s**; todas las `tests/test_code_mode*.py` (7 ficheros) **41 correctas, 17,96 s**; selección conjunta de los tres commits pendientes de registro (`test_code_mode_windows_job`, `test_main_inference_active_admission`, `test_memory_engine_strict_query`) **60 correctas / 1 omitida (Chroma real, thin client), 9,02 s**. Sin modelos, GPU ni datos personales (`ODYSSEUS_DATA_DIR` temporal).

Límites: JobObject no es aislamiento de filesystem/red ni frontera frente a otros procesos del mismo usuario; el nombre del job no es secreto. En POSIX no cambia nada (fixture nativo omitido fuera de Windows). Stdin/handles durables y gestor general de procesos (H11) siguen pendientes.

Registro definitivo de la tanda: `85058e46` (primitiva híbrida read-only, **cerrada**), `6974f6c8` (espera de inferencia principal medida, **cerrada en su alcance**), `58c8b7c9` (JobObject Code Mode Windows, **cerrado**). Los estados «en implementación / NO cerrado» anteriores sobre JobObject quedan sustituidos por este bloque.

Fuente original ya visitada https://github.com/autonomous-ai/openharness y https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; adaptación propia. No repetir.

Checkpoint`58c8b7c9`. Siguiente: adaptador `context_search` → recibo de reutilización híbrida; después decisión sobre duración auxiliar de recuperación; después imágenes en Prospero y hoard de diapositivas.


### VISITADO / IMPLEMENTADO — context_search con recibos de reutilización híbrida, 30-09-2026

`5aada5d7`: mientras se captura un paquete, `MemoryEngineSource` responde la consulta con texto (léxica/híbrida) con la primitiva estricta `context_search` y guarda un recibo híbrido: ruta de la BD, identidad del runtime vectorial instalado (`None` si el carril semántico está vetado) y el **reloj de puntuación** de la captura. La revalidación repite la misma consulta estricta con ese reloj y con la vigencia evaluada **ahora**: el decaimiento por tiempo no invalida el paquete; sí lo invalidan cambios de texto, un miembro nuevo que coincide, marcas de conflicto, expiración, BD movida, runtime de embeddings cambiado o retirado y cambios en los aciertos semánticos. Fallo estricto al capturar → búsqueda legacy sin recibo (sin reutilización); fallo al validar → nunca repara ni inicializa almacén.

Hallazgo propio: sin fijar el reloj, un `effective_score` que decae de forma continua cambia su redondeo a 6 decimales en segundos y el recibo no reutilizaría nunca (comprobado por mutación: 4 pruebas caen).

Pruebas: 16 nuevas (`tests/test_memory_engine_hybrid_reuse.py`); con freshness/strict/wiring 89 correctas/2 omitidas en árbol aislado del commit; QA Chroma real 1.5.9 (PersistentClient, venv QA aislado) **83 correctas, 0 omitidas** (`D:/LocalAI/tmp/chroma-strict-qa-20260930/run_hybrid_reuse_qa.py`, `hybrid-reuse-qa-result.json`). Selección amplia context/memory/reuse/rag: 1330 correctas/5 omitidas.

Límites: sin transacción atómica SQLite↔Chroma ni epoch de configuración global; el carril semántico sin almacén instalado sigue sin recibo (legacy degradado). Fuente original ya visitada https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9; adaptación propia. No repetir.

### VISITADO / IMPLEMENTADO — duración medida en recuperación auxiliar (decisión: sí), 30-09-2026

`984ce0e6`: los pasos 2 y 3 de la escalera de recuperación usan el mismo span «solo esperas del proveedor» que el stream principal y registran en el recibo pendiente de la ronda. La admisión del paso siguiente ve la duración medida del anterior (un paso 2 de 70 s bloquea un paso 3 que superaría el presupuesto de 100 s); el crédito legacy de pared de la ronda liquida todo una sola vez; las pausas del consumidor quedan fuera; sin observador no se envuelve el stream. 6 pruebas nuevas; 48 con las suites de admisión activa/pendiente; selección de presupuesto 168 correctas. Mutación: 5 de 6 caen sin el cambio. Pendiente H14 que sigue abierto: otros auxiliares (compactación), reservas previas y cuentas transversales.

### VISITADO / IMPLEMENTADO — imágenes desde el chat: recorrido completo probado por navegador, 30-09-2026

Prueba real (no mocks): Studio en un Faustus aislado (7002, datos QA copiados en `D:/LocalAI/tmp/prospero-ui-e2e-20260930`), Qwen2.5-3B q8 en llama.cpp GPU3 (8191), Prospero no demo (8815) y ComfyUI 0.37 propio en GPU1 (8189, `--disable-smart-memory`), Qwen-Image 2.1. Adjunto por arrastre de un PNG real, petición en castellano, aprobación en la tarjeta, trabajo GPU de ~80 s, resultado en directo y al reabrir. Nueve fallos reales encontrados y corregidos:

`a756c1f5` (entrega):
1. `/api/chat_stream` en modo agente no dejaba pasar `generated_image`: la imagen nunca llegaba al navegador en directo. Auditoría automática (test) de tipos emitidos por el bucle y decodificados por Studio: añadidos también `strategy`, `context_receipts`, `plan_tracker`, `rewrite_policy_triggered`, `system_notice`.
2. La ruta de imágenes generadas rechazaba con 400 los nombres UUID de la galería de Prospero.
3. Al reabrir, Studio ocultaba resultados con `exit_code` null (Prospero no informa exit code).
4. Enlaces con host inventado por el modelo (`https://api.gallery.example.com/...`, `https://api.generated-image/...`) → reescritos a la URL real de la herramienta, también cuando el evento aprobado guarda la URL sin ID (ahora guarda el ID también).

`3cb3f911` (petición):
5. El ID de galería del adjunto no llegaba a un modelo sin visión (la metadata guardada del upload no lo conserva): se recupera por hash+propietario; solo bytes que decodifican como imagen se promueven a galería.
6. El aviso «[No vision model configured — set one in Settings → Vision]» activaba el toolset de admin (palabras «configured/Settings») y desplazaba las herramientas de imagen; el bloque de imagen ya no cuenta como instrucción (enrutado y idioma de respuesta).
7. `edit_image` quedaba en el catálogo (lookup_tools) aunque el dominio media estuviera detectado; ahora es schema caliente si hay imagen propia en este mensaje o en los 6 anteriores; «genera/crea/dibuja una imagen» → `generate_image`.
8. Render duplicado: una ronda posterior repetía el mismo trabajo (con `mask_id` espurio) → segundo render de 80 s y segunda imagen. Ahora devuelve la imagen ya producida (clave semántica del trabajo; iguales en la misma ronda siguen siendo lote).
9. «Se ha añadido un bigote» sin herramienta aceptado como respuesta: en peticiones de imagen se rechaza la afirmación sin imagen, se da un reintento y, si persiste, se sustituye por un texto veraz.

Evidencia visual: sombrero vaquero marrón, estilo anime, bufanda roja (directo y al reabrir, un solo render, `[media] identical edit_image ... served, not re-run` en el log), gafas. Capturas en la sesión. Pruebas: 5 referencia de galería, guard de decodificación, 12 enrutado, 12 trabajo repetido, 13 afirmaciones, ruta + auditoría, UUID, restore null (check Studio), enlaces. Árbol aislado por commit: 37 y 180 correctas.

Límite honesto: el 3B a veces copia una respuesta falsa anterior y no llama a la herramienta en seguimientos; ahora el usuario recibe «No he editado ni generado ninguna imagen…» en vez de la mentira. Calidad fotográfica no certificada. Servicios QA propios cerrados; listener ajeno 8090 intacto.

### VISITADO / IMPLEMENTADO — armonizar vía Prospero; rembg/upscale BLOQUEADO / DEPENDENCIA REAL, 30-09-2026

`bcfe6e8e`: armonizar (`edit_image action=harmonize`) con estudio Prospero seleccionado → img2img SDXL existente de Prospero (`/api/assets/{id}/edit`, `operation=img2img`, `strength` = denoise, 0.4 por defecto; prompt por defecto de armonización si viene vacío). Recibo con huella propia (operación+fuente+strength). Con `configured` se conserva el servicio anterior. Pruebas: `test_prospero_images.py` + `test_chat_prospero_image_tools.py` 121 correctas.

BLOQUEADO / DEPENDENCIA REAL: `rembg` y `upscale` no pueden consolidarse en Prospero sin modelos: `ComfyUI/models/upscale_models` y `background_removal` solo tienen el marcador vacío, y ni el venv de Faustus ni el de Prospero tienen `rembg`/`realesrgan` instalados (las acciones actuales de Faustus devuelven su error «no instalado»). Siguiente paso concreto: autorizar la descarga de RealESRGAN_x4plus (~64 MB) y de un modelo de recorte (RMBG/BiRefNet); con ellos, Prospero añade dos operaciones con nodos core de ComfyUI (ImageUpscaleWithModel) y el adaptador las delega igual que inpaint/img2img.

### VISITADO / IMPLEMENTADO — cancelación acotada y recolección tras reinicio, 30-09-2026

Prospero `6a01d9d`: cancelar un trabajo de imagen ya no usa el `/interrupt` global de ComfyUI. Lee `/queue`: interrumpe por su propio `prompt_id` solo si es el que se ejecuta, lo saca de la cola si espera y no hace nada si ya salió. `hoard_link` vendorizado intacto. `tests/test_scoped_comfy_cancel.py` 4; suite completa Prospero 458 correctas (238,69 s).

Faustus `d86f9726`: `image_job` acepta `action=cancel` (mismo propietario, sesión y conexión; en cola → `cancelled`; en ejecución → `cancel_requested`; ya terminado → se recoge la imagen). Al arrancar, `reconcile_pending_images` sondea los recibos `waiting` con job conocido bajo su propio propietario/sesión/conexión y publica lo terminado; nunca crea, importa ni reenvía; conexión cambiada → se deja. Un job observado como cancelado informa `cancelled`, no `failed`. 8 pruebas nuevas; 130 correctas en las dos suites de imagen; selección imagen/media/tool_index/schema 471 correctas + 1 fallo ajeno (abajo).

Prueba real (ComfyUI 0.37 GPU1, Prospero, Faustus QA 7002): con un prompt ajeno ejecutándose y el de Faustus en cola detrás, `cancel_image` dejó la cola solo con el ajeno, que terminó `execution_success`; el trabajo quedó `cancelled`. Recolección: petición enviada, Faustus reiniciado a mitad de render → log `reconciled studio image receipts: checked 1, done 1`; la imagen (bosque nevado) aparece en Biblioteca → Imágenes en el navegador y la cancelada no. Script `D:/LocalAI/_claude_tmp/e2e_cancel.py`. Servicios QA cerrados; 8090 ajeno intacto.

`32d6e2ad`: `test_tool_index_language` fallaba de forma intermitente porque el embedder de prueba usaba `hash()` (salado por proceso): reproducido en HEAD con `PYTHONHASHSEED=22`; ahora crc32, 26 semillas seguidas correctas.

Límites: parar el turno del chat no cancela el trabajo remoto (decisión: la imagen se recoge luego; cancelar es explícito). Los envíos sin job conocido siguen necesitando reconciliación manual. Con ComfyUI antiguo sin interrupt por id, el interrupt solo se envía si el prompt propio es el que se ejecuta. No repetir.

Checkpoint`d86f9726` (Faustus) / `6a01d9d` (Prospero): registrados `984ce0e6`, `5aada5d7`, `a756c1f5`, `3cb3f911`, `bcfe6e8e`, `32d6e2ad`, `d86f9726`. Pendiente real: descarga de modelos para upscale/rembg (requiere autorización) y el hoard de diapositivas (nombre y motor por decidir).
### VISITADO / IMPLEMENTADO — ampliar y quitar fondo en el estudio de imagen; Cicero publicado, 30-09-2026

Autorizado por Luis: descargados `RealESRGAN_x4plus.safetensors` (models/upscale_models, Comfy-Org/Real-ESRGAN_repackaged) y `birefnet.safetensors` (models/background_removal, Comfy-Org/BiRefNet), sha256 comprobado. Prospero `6bc194d`/`8b08591`/`ae0c533`/`17bbd7a`/`3b6a09d`: plantillas `esrgan_upscale` y `birefnet_remove_background` (nodos del núcleo de ComfyUI), operaciones `upscale` (x2/x4, `too_large` > 8192 px) y `remove_background`, `model_missing` accionable, botones en la biblioteca, 483 pruebas en Windows. Prueba real: UI 8825 x2 en 12 s; recorte en ~5 s. Fallos reales encontrados y corregidos: una escena sin sujeto devolvía una imagen totalmente transparente como éxito → `warning: no_subject_found` + `foreground_share`; ampliar un recorte le devolvía el fondo escondido bajo el alfa → plantilla `esrgan_upscale_alpha` (LoadImage MASK → JoinImageWithAlpha), elegida sola si el origen tiene transparencia. Esos dos commits están en la rama `qa-alpha` de Prospero (rebasada sobre `3aebb49`), a la espera de fusionar en `main` cuando la otra sesión deje limpios README/docs/MCP.md (no se tocó su árbol).

Faustus `512bf6d4`: `edit_image` upscale/rembg van al estudio seleccionado con recibo durable (huella = operación + escala + imagen); el recorte conserva el alfa en la galería. Prueba en Studio (7002, 27B en 8081, Prospero 8825 desde la rama): «quítale el fondo … y amplía el recorte a x2» → recorte 2,5 s, aprobación, 2048×2048 RGBA con esquinas transparentes; respuesta final correcta con el ID de galería. Con el estudio caído el resultado queda «sin confirmar» con el ID para `image_job`, sin reenvío.

Cicero `c32ca5f`: icono sin borrones (el dragón se reconstruye de los iconos de la familia en vez de rellenar a ciegas la caja del glifo). Publicado a petición de Luis en https://github.com/Luissalet/CiceroHoard (público, temas mcp/fastapi/react/presentations) y añadido a `plugins/marketplace.json`.

Checkpoint `512bf6d4` (Faustus) / `c32ca5f` (Cicero) / `qa-alpha 69353f9` (Prospero). No repetir.
