# Pendientes de cierre

Actualizado: 09-09-2026 (23:10). Sólo trabajo vigente; quitar cada entrada al cerrarla.

## Comprobaciones pendientes

- **Voz física:** conversación completa con micrófono en español e inglés. No activar grabación ni permisos para cerrar esta casilla sin intervención del usuario.

- **Docker Desktop y el entorno del puente MCP.** Toda la noche del 08-09 se cayó al arrancar con `unable to get 'ProgramData'` cuando lo lanzaba una sesión a través del puente (cuatro vías probadas: directa, con el entorno repuesto, vía `explorer.exe` y como tarea programada interactiva). Lanzado por Luis a las 22:12 arrancó a la primera. El puente entrega un PowerShell **sin `ProgramData` ni `ALLUSERSPROFILE`**; anotado por si vuelve a aparecer con otro programa.

- **Discover marca «installed» sólo por nombre exacto.** `qwen3.8:27b-q4_K_M` está instalado pero la etiqueta `27b` del catálogo sale con «Pull»; sólo `27b-q8_0` aparece como instalada. Cosmético; la comparación debería tolerar el alias de cuantización por defecto.

- **La puerta de admisión de VRAM aún no cubre el turno de chat.** Cubre research y el botón Cargar de Modelos locales (OBJ-1). Si el chat elige un modelo no residente con otro dentro, Ollama sigue decidiendo por su cuenta. Está en OBJETIVOS.md como ampliación de OBJ-1.

- **«Waiting for the model» (punto 4 de Luis) no reproducido en vivo.** Con `q4_K_M` residente un turno de chat tardó 2,3 s sin aviso. La línea ahora dice cuántos tokens de contexto está leyendo el modelo tras 8 s, para que un contexto de 131k con spill no parezca un cuelgue; la causa de fondo del caso de Luis (35,3/44 GB con *PCIe spill* a la 01:28) era el `q8_0` derramando a RAM, no un modelo sin cargar.

## Lista de Luis, 09-09 de madrugada (estado a las 23:10)

1. ~~Research con Docker levantado → 0 fuentes.~~ Cerrado. No era la búsqueda: `rp-…` de las 22:13 corrió con `q8_0` a 131k de contexto derramando a RAM, todas las llamadas al modelo caducaron a los 90 s, 0 rondas, y el handler lo registró como «completed successfully». Ahora un run sin rondas ni fuentes es un **fallo con causa** (`ResearchFailed` en `src/deep_research.py`: «el modelo no respondió», «la búsqueda no devolvió nada», «cancelado»). SearXNG devuelve 20 resultados por consulta; el 0 de la noche anterior era el contenedor calentando.
2. ~~El Retry de una research no responde bien.~~ Cerrado: `launch()` no limpiaba `sessionId` y el seguidor se reenganchaba al stream ya terminado. Ahora aborta el seguidor viejo y parte de cero.
3. ~~El texto del cuadro del chat se pierde al cambiar de chat.~~ Cerrado: borrador por sesión (y uno para «conversación nueva») en `localStorage`; se vacía al enviar. Comprobado por pantalla: dos chats, dos borradores, cada uno vuelve intacto.
4. **«Waiting for the model» con el modelo cargado.** Ver Comprobaciones pendientes; mitigado, no reproducido.
5. ~~Descargar modelos sólo llega a Qwen 3.5.~~ Cerrado: catálogo con `qwen3.8` (27b 16,5 GB; 27b-q8_0 27,9 GB) en cabeza y `qwen3-coder-next` (q4_K_M 48,2 GB; q8_0 79 GB); tamaños de los manifiestos de registry.ollama.ai. Comprobado por pantalla.
6. ~~Cargar un modelo desde Modelos locales no enseña nada.~~ Cerrado: el botón pasa a «Loading…» con spinner, los demás Cargar se deshabilitan, aviso de inicio y de fin con segundos. Comprobado por pantalla con `qwen3.8:27b-q4_K_M` (solo, según la regla).
7. ~~Un proyecto con el mismo nombre que su carpeta no se deja crear.~~ Cerrado: sí se dejaba; el mensaje «Folder 'X' already belongs to project 'X'» hacía creer que no. Se refería a la carpeta de chats del panel (que toma el nombre del proyecto). Ahora: «A project called 'X' already exists — open it, or choose another name.» Comprobado por pantalla: creado `Nombreigual` en `…\Nombreigual`; el segundo intento enseña el mensaje nuevo.
8. ~~Personalización de modelos.~~ Cerrado en lo que Ollama permite: en Opciones de cada modelo hay un cuadro **Other options** (JSON) que se envía como `options` en cada petición (`num_batch`, `num_thread`, `min_p`, `top_k`, `repeat_penalty`, `seed`, `stop`, `use_mmap`, `low_vram`…; lista blanca en `src/model_load_options.EXTRA_OPTION_KEYS`). Los flags de llama-server (`-jinja`, `--spec-*`, `--cache-type-*`, `-np`) **no son opciones por petición en Ollama**: el formulario lo dice y el servidor los rechaza nombrándolos. Comprobado por pantalla: `{"-jinja": true}` → error en línea; `{"num_batch": 512, "min_p": 0.05}` → guardado, «+2 options» en la fila, y vuelve al formulario.
9. **Verificar todo por MCP y por pantalla.** Hecho para 2, 3, 5, 6, 7 y 8 en la instancia 7001 con el bundle recién compilado. Queda: una research completa de principio a fin con `q4_K_M` solo (la de WAD de Luis) y el flujo del 4.

Las tres GPUs son reales: RTX 4070 Ti (12 GB) + dos RTX 5060 Ti (16 GB) = 43,9 GB. La GPU 1 salía sin lectura por un `—` en lugar de «0 MB» cuando está vacía; corregido.

## Lo que rompió la máquina el 08-09 (regla, no anécdota)

Dos 27B dentro a la vez —`q8_0` residente de una prueba (33 GB, con spill) y `q4_K_M` cargado por una research (17 GB)— más un build de Vite, dos tandas de pytest y siete procesos de Docker Desktop, superaron el **commit limit** de la máquina (147,7 GB = 128 de RAM + 20 de pagefile). La cascada, en orden: `cudaMalloc failed: out of memory` en la ronda 2, `MemoryError` en el servidor, `can't start new thread`, y después ni PowerShell arrancaba (`0xC000012D`, STATUS_COMMITMENT_LIMIT). El escritorio se quedó en negro con una sola ventana de error.

**La regla: nunca dos modelos grandes cargados a la vez, y nada pesado corriendo mientras hay uno dentro.** `ollama ps` antes de cargar, `ollama stop` del anterior. La puerta de admisión que automatiza esto es OBJ-1 en OBJETIVOS.md.

Las carencias de backend del índice anterior están implementadas; se ha eliminado ese índice vacío.
Las ampliaciones acordadas viven en OBJETIVOS.md; ahora mismo, OBJ-1 (puerta de admisión de VRAM). Eliminados los índices de UI resueltos; se pueden recuperar del historial Git.
No contar planes de inspiración o notas de implementación como otra cola de tareas.

## Última evidencia

- 09-09, 23:05: `pytest` sobre local-models, model_load_options, projects, llm_core (ollama/streaming), deep_research y research_*: **561 correctas**. Nuevas: `tests/test_model_load_options_extra.py` (6), `tests/test_deep_research_empty_run_is_a_failure.py` (3), `tests/test_vram_admission.py` (14), `tests/test_research_model_load.py` (5), `tests/test_search_appliance.py` (7). `tsc` limpio, `scripts/i18n_es.py --check` limpio (5.730 cadenas), `build-studio --force` correcto. Instancia 7001 reiniciada con el bundle nuevo; `ollama ps` vacío al terminar.

- Cierre creativo/escritorio del 08-09: suite completa **13.236 correctas, 81 omitidas, cero fallos** (`logs/checkpoint-creative-full.xml`). Correcciones posteriores: **89 regresiones correctas**; transcripción y narración reales offline EN/ES: **4 correctas**. Adaptaciones de Diogenes: **471 regresiones correctas** (`logs/checkpoint-diogenes-regression.log`). Ventanas principal y secundaria, controles personalizados y propiedad del servidor comprobados en Electron real.

- 08-09: lanzadores web del repositorio probados con parada, arranque y reutilización sin duplicado. Ventana Electron real: minimizar, maximizar/restaurar, pantalla completa y cierre comprobados; cierra su backend propio y conserva el servidor web compartido. Barra integrada en temas y disponible también en el acceso. Regresiones de tareas/contexto: **68 correctas**; bloque creativo: **36 correctas**, incluido doblaje real local EN/ES; rutas de vídeo: **3 correctas**; controles de interfaz/escritorio/zonas horarias: **25 correctas**. TypeScript, scripts frontend, compilación y portfolio/CV correctos.

- Clientes oficiales en Brave (08-09, 02:07): Codex y Claude responden con sus sesiones de suscripción; Claude delega a un worker Codex, con resultado registrado, aprobación y cero cambios de archivos. Corregidos esquemas MCP incompatibles y descripciones ausentes de herramientas textuales. Bloque de **234 pruebas correctas**, controles de transporte de memoria y etiquetas de modelo, TypeScript y compilación correctos.

- Suite completa: **13.183 correctas, 81 omitidas y cero fallos** (`logs/checkpoint-context-client-full-20260908.xml`). Los cambios posteriores de edición/artefactos pasan **71 pruebas de regresión** (`logs/checkpoint-final-media.log`). Todos los scripts frontend, TypeScript y compilación correctos.
- Brave: Claude responde con skills automáticas desactivadas y presupuesto por turno; corregido el bloqueo de limpieza temporal de Windows. Editor→chat adjunta una copia sin enviar, conserva el borrador de capas/máscaras y abre adjuntos existentes. Compositor móvil corregido para mantener enviar/parar visible. Procedencia de artefactos comprobada con datos sintéticos, estados parciales y fallo recuperable de red.
- Navegador Brave y Qwen local: objetivo OBJ-1 creado en proyecto sin carpeta, conservado tras recarga; trabajo continuado fuera del chat. Respuesta inglesa tras herramienta y texto entre rondas completos tanto en vivo como tras recargar (08-09, 01:15).
- Objetivos y aislamiento: **225 correctas** (`logs/astra-objective-scope-focused.xml`).
- Integración: **203 correctas** (`logs/astra-objective-scope-integration.xml`).
- Servidor reiniciado el 08-09: HTTP 200.
- TypeScript y compilación de producción correctos. Separada la caché de React sin adelantar la carga del editor; ya no aparece el aviso de tamaño del bundle.
