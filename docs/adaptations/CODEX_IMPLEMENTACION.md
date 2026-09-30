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
| H17 | `03432131`: lookup y categorías filtran permisos antes de anunciar schema/promoción; fallback permitido respeta pool vacío, 42 pruebas finales. `634de422`: filtro antes del corte final del catálogo, 50 pruebas. `a2fba514`: filtro del índice antes de corte; `17b6bc8a`: ventana adaptativa hasta256 y filtro lexical en fusión, 22 focales correctas. Adapters legacy, candidatos tras256 y exposición por descriptor/snapshot pendientes |
| H22 | `639ef1e6`: probes omitidos conservan observaciones anteriores. `358f2f34`: clave scoped y writer nativo/capabilities/fit/explorer, 115 pruebas. `1f3bfcbd`: provider_policy y alternativas usan almacén contextual, 106 pruebas. `0e696621`: router offline sólo declara sin contexto, 94 pruebas finales. `50ce7888`: cambios de modelo/sesión contextualizan hints, 65 pruebas finales. `4f096bfd`: revisión UUID de configuración y writer capturado, 185 pruebas; lectores/snapshots y credenciales vinculadas implementados; `ee863d66` guarda de coherencia; `24f3685c` actualización optimista de credenciales evita refresh obsoleto; 102 pruebas integradas finales. SQL externo y probes específicos pendientes |
| H23 | `7575dbc5`: listado de trazas conserva run y uso observado, cero/parcial y coste desconocido sin inventarlo; 53 pruebas finales. Vista y agrupación causal por fase pendientes |
| H07 | `4cac9054`: primer gate de extensión gobernado por acción pura, fallback legacy ante error/malformed; grants y cierre intactos. 30 pruebas finales. Controlador común transversal pendiente |

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
