# Pendientes, deuda y cosas a vigilar — UI

Rama: `feat/studio-ui`. Estado a 04-09-2026.
Compañero de `OBJETIVOS_UI.md`. Aquí va lo que puede romper algo, no lo que
falta por construir.

## Deuda legacy medida (baseline de UI-012)

Cifras del análisis estático previo, pendientes de confirmar con el auditor:

- ~92 apariciones de `transition: all`.
- >110 eliminaciones de `outline` sin comprobar reemplazo de foco.
- >300 lecturas de `getBoundingClientRect`; legítimas en ventanas y editor,
  peligrosas si se intercalan con escrituras.
- Controles interactivos construidos como `div` en menús y cabeceras.
- Iconos que dependen de `title` sin `aria-label`.
- 262 `<svg>` inline dentro de `index.html`, más estilos inline repetidos.

Regla: no se arregla la deuda antigua en bloque. Cada pantalla migrada limpia
su área y baja el baseline. Ningún módulo Studio nuevo la aumenta.

## Riesgos abiertos

1. **Fallback SPA.** Es el único cambio del incremento 1 que puede romper la
   API. Lista blanca de siete rutas, jamás `{path:path}`, y test de que un 404
   de API sigue siendo JSON.
2. **Identificadores de artefacto.** Sin espacio de IDs común entre galería,
   documentos y outputs de run, `Usar en…` entre subsistemas será frágil. Hay
   que decidirlo antes de UI-042, no durante.
3. **`style.css` de 1,52 MB.** Cualquier tentación de reescribirlo mezclada con
   features es cómo se pierde el trimestre. Se divide en el incremento 5 y a
   solas.
4. **Coste y presupuesto en ContextBar.** El backend expone coste por run en
   `agent_loop.py` y `external_worker.py`, pero falta confirmar que hay
   estimación *previa* a ejecutar. Si no la hay, el chip de presupuesto no se
   promete en UI-031.
5. **Doble atajo de teclado.** `Ctrl/Cmd+K` de la paleta contra el buscador de
   conversaciones existente. Se resuelve en UI-022 convirtiendo la búsqueda en
   comando; si algo sigue capturando la tecla, se localiza antes de cerrar.

## Entorno de pruebas

- `Start-Faustus-Dev.ps1` sigue apuntando a `D:\LocalAI\odysseus-dev`, que ya no
  existe. Para el 7001 se usa la raíz real con `ODYSSEUS_DATA_DIR` apuntando a
  `D:\LocalAI\odysseus-dev-data`, `APP_PORT=7001`, `LOCALHOST_BYPASS=true`,
  `AUTH_ENABLED=false`. Arreglar el script es un pendiente propio.
- E2E: `ODYSSEUS_E2E=1 python -m pytest tests/e2e -q` desde `venv`. Playwright
  1.62 instalado. En Windows hay un error de colección conocido en
  `tests/test_history_import.py` que no es nuestro.

## Riesgos que trae la decisión de React (04-09-2026)

6. **`[!]` Dos sistemas vivos.** Es la forma en que mueren estas migraciones:
   medio repo en React, medio en el DOM antiguo, y nadie se atreve a borrar.
   Mitigación acordada: cada pantalla migrada retira su equivalente legacy en
   el mismo incremento. Un incremento que termina sin haber borrado nada es una
   alarma, no un detalle.
7. **`[?]` Build reproducible sin red.** La primera instalación de dependencias
   necesita registro. Hay que decidir en UI-002 si se versiona el lockfile con
   caché de npm, se vendoriza `node_modules` o se acepta que clonar exige una
   instalación con red una vez. Node instalado: v22.19.0 (Vite lo soporta; la
   CLI de `skills` pide >=22.20.0 y avisa, sin romper).
8. **`[!]` Bundle obsoleto servido en silencio.** Si `Start-Faustus.ps1` sirve
   un `static/studio/` viejo sin avisar, se depuran fantasmas durante horas.
   Tiene que construir o fallar diciendo por qué.
9. **`[?]` CSP con nonce.** El bundle es un `<script>` externo y encaja, pero
   los estilos inline que inyectan algunas primitivas de Radix y las
   animaciones hay que comprobarlos contra la CSP real, no suponerlos.
10. **`[!]` Cobertura perdida al cambiar de markup.** La familia de tests que
    assertaba HTML dentro de `index.html` deja de valer. Ningún test se borra
    sin que su sustituto pase antes; lo que se quede sin cobertura se anota
    aquí con nombre y apellidos.
11. **`[~]` Peso y arranque.** Faustus es local-first: nada de CDN en tiempo de
    ejecución y presupuesto de bundle declarado en UI-002. Cada dependencia
    nueva fuera de la tabla aprobada es una decisión, no un `npm install`.

## Añadido tras el lote B (04-09-2026)

12. **`[~]` Herramientas de build en `dependencies`.** El entorno tiene
    `NODE_ENV=production` y `npm config omit=dev` globales, así que Vite,
    TypeScript y el plugin de React se declararon como dependencias normales
    para que se instalen. Es aceptable en un repo privado y local-first, pero
    cualquier despliegue futuro se los llevará puestos. Documentado en
    `docs/ui/toolchain.md`.
13. **`[~]` La galería entra en el bundle.** Los 307 KB medidos incluyen
    `studio/src/gallery/`, que ninguna pantalla real importará. Cuando el
    AppShell tenga su propia entrada (UI-021), la galería debe quedar fuera
    del bundle de producción o en un chunk perezoso; hasta entonces la cifra
    de presupuesto está inflada a nuestro favor, que es el sentido malo.
14. **`[?]` `npm install` falla en Windows.** `ERR_INVALID_ARG_TYPE` en el
    postinstall de esbuild con npm 10.9.3 y Node 22.19.0. Se arregla con
    `npm install --ignore-scripts` y luego `node node_modules/esbuild/install.js`.
    Nadie ha comprobado si se reproduce en una máquina limpia.
15. **`[!]` Accesibilidad solo verificada a ojo.** Las guardas de UI-012 son
    estáticas y las ratios de contraste están calculadas a mano. No hay
    comprobación automática sobre la página renderizada: orden de foco,
    trampas de teclado y contraste en vivo siguen sin cubrir. Va con UI-021.
16. **`[~]` El límite de uso de la cuenta puede parar el trabajo delegado.**
    El Claude Code local devolvió `success` con cero tokens y cero tiempo de
    API justo al acabar el lote A. No da error legible: hay que mirar
    `--output-format json` para verlo. Si un lote termina en segundos y sin
    escribir nada, es esto y no un fallo del encargo.

## Añadido tras Studio (04-09-2026, lote E)

17. **`[!]` Studio aún no sustituye al chat legacy.** Faltan adjuntos,
    documentos (`doc_update`, editor), comparar modelos, presets/personajes,
    incógnito, edición y borrado de mensajes, fork y compactar. Hasta que
    tenga eso, «Abrir en la interfaz anterior» se queda en la cabecera y
    `static/js/chat.js` no se toca (DECISIONES_UI.md §4).
18. **`[~]` Eventos del stream que Studio ignora a propósito.** `plan_update`,
    `browser_view`, `doc_*`, `ui_control`, `context_ledger`, `subagent_event`,
    `harness_*`, `round_info`, `progress_update`. Llegan y se descartan sin
    ruido. Cada uno es una pantalla o panel pendiente, no un bug.
19. **`[!]` Los tiempos del servidor son UTC sin zona.** `/api/sessions` y
    `/api/history` escriben `2026-08-31T15:35:30.170267` sin `Z`. El
    adaptador (`parseStamp` en `adapters/home.ts`) los lee como UTC. Si algún
    endpoint escribe hora local sin zona (`next_run` de tareas es sospechoso)
    saldrá dos horas desplazado: verificar Automatizaciones contra la hora
    real de una tarea programada.
20. **`[~]` `min-block-size: 0` del reset rompe filas con hijos `overflow`.**
    `base.css` pone `min-block-size: 0` a `button`/`a` para vencer a las
    alturas fijas del legacy; en Chrome, un enlace grid con hijos
    `overflow: hidden` colapsa a cero. Studio lo repone por clase
    (`.fs-studio__session`). Cualquier fila nueva con esa forma necesita lo
    mismo o cambiar el reset por algo más quirúrgico.
21. **`[~]` La paleta de modelos lee `/api/models` una vez por visita.** Sin
    `refresh`, así que un endpoint que se enciende después no aparece hasta
    recargar. Falta un «actualizar» en la paleta y respetar `models_extra`.
22. **`[?]` Las capturas dependen del modelo local.** `shot_studio.py` fotografía
    la sesión más reciente; con Ollama apagado el transcript de la captura
    enseña el error de conexión, que es honesto pero no representativo.
23. **`[~]` `ollama serve` lanzado a mano no lee `OLLAMA_MODELS` del usuario**
    si el shell viene del bridge: hay que exportarla antes
    (`_claude_tmp/ollama_up.ps1`). No es de Faustus, pero cuesta veinte minutos
    la primera vez.

## Añadido tras el lote F (04-09-2026, paridad del compositor)

24. **`[!]` Dos copias de React por una query de cache-busting.** Un chunk
    perezoso importa lo que comparte con la entrada como `../studio.js`
    (sin `?v=`), y el navegador lo trata como un módulo distinto de
    `studio.js?v=…`: dos React, «invalid hook call», el shell entero
    desmontado al abrir el primer diálogo perezoso. Arreglo: la entrada es
    diminuta (`main.tsx` enlaza la hoja y hace un `import()` de `app.tsx`),
    los chunks llevan hash y la hoja de estilos se importa `?url` y se
    enlaza desde la entrada. `index.html` ya no enlaza el CSS. Cualquier
    vuelta a nombres estables sin hash reabre esto.
25. **`[~]` Presupuesto en bruto superado: 373 KB / 118,7 KB gzip.** El
    gzip sigue bajo los 120 KB, que es lo que viaja; el bruto pasa de 350.
    Se propone dejar el bruto como aviso y el gzip como límite duro, con
    Proyectos, Biblioteca, Automatizaciones, Actividad, el diálogo de
    carpeta y el de sesión como chunks perezosos. Decisión pendiente en
    DECISIONES §1.
26. **`[!]` Adjuntos rechazados en el 7001, en cualquier cliente.** Con
    `AUTH_ENABLED=false` la subida guarda `owner=None` y la sesión tiene
    `owner='admin'`; `reserve_upload` exige igualdad y `document_processor`
    avisa «not found or not authorized», así que el modelo no ve el fichero.
    Studio manda exactamente lo que manda la anterior (`attachments` con los
    ids de `/api/upload`). Verificar en el 7000 con auth; si allí funciona,
    es un fallo del modo sin auth en el servidor, no de la UI.
27. **`[~]` El historial no guarda `metadata.attachments`** en este servidor
    (el mensaje del usuario vuelve sin ellos), así que tras recargar, un
    turno con adjuntos los pierde visualmente. Studio los enseña mientras la
    sesión está abierta.
28. **`[x]` `/versions` y restaurar.** Cerrado: `/versions` lista, `/restore
    <id>` restaura y `/checkpoints` lista los del workspace.
29. **`[~]` Comandos `/` que enrutan a la anterior** pierden el argumento:
    `/research tema` abre la interfaz anterior sin el tema.

## Añadido tras el lote G (04-09-2026, arnés y selector nativo)

30. **`[!]` El selector de carpeta era un diálogo propio dentro de la
    página** («esa ventana… es terrible»). Cerrado: `POST
    /api/workspace/pick` abre el diálogo del sistema en el escritorio del
    servidor desde un subproceso con tkinter (Explorador en Windows) y
    devuelve la ruta ya vetada; el chip de Studio y la pill de la anterior
    lo llaman primero y solo caen al diálogo en página si el navegador no
    está en la misma máquina (`request.client.host` fuera de loopback), no
    hay pantalla o no hay Tk (501). Un único diálogo a la vez (409), se
    cierra si el cliente se va, y queda fuera del `REQUEST_HARD_TIMEOUT`
    de 45 s porque espera a una persona. Los adjuntos ya usaban el
    `<input type=file>` nativo. Riesgo conocido: el diálogo aparece en el
    monitor donde esté la ventana activa, no siempre encima del navegador.
31. **`[~]` Al aprobar una herramienta el servidor cierra la llamada
    pendiente con un `tool_output` vacío y luego repite `tool_start`.** El
    reductor marca ese último paso como «esperando» al recibir `ask_user`
    para que la repetición lo reutilice en vez de duplicar la fila; y el
    texto de la pregunta, que el servidor también manda como `delta` y
    guarda como mensaje propio del asistente, se retira al aprobar y al
    leer el historial (mensaje corto del asistente acabado en `?` seguido
    de otro del asistente). Si el servidor cambia ese patrón, revisar
    `apply('ask_user')` y `turnsFromHistory`.
32. **`[~]` `changeset.verdict` llega como `partial` con 65 % en un turno
    de un solo fichero creado tal cual se pidió.** Es del revisor del
    servidor, no de la UI; Studio lo enseña sin maquillar.

## Añadido tras el lote H (04-09-2026, sub-agentes, panel lateral, documentos)

33. **`[~]` `qwen3.5:9b` no llama a `delegate_agents` con `/agents`.** El
    campo `delegate_tasks` viaja igual que en la anterior y el servidor
    sustituye el mensaje por la instrucción de delegación, pero el modelo de
    prueba escribió los ficheros él mismo y lo dijo. El tablero en vivo está
    verificado con el reductor (`studio/checks/model.check.mjs`, secuencia
    sintética de eventos `subagent`) y el restaurado con sesiones reales
    (`bench t3v2_q35b …`, 3 workers). Probar en vivo con un modelo que
    delegue.
34. **`[~]` Ningún `browser_view` real en el 7001.** El panel «Navegador»
    se alimenta de `browser_view` y de las capturas de `tool_output` por el
    mismo camino (`safeFrameSrc`, misma lista blanca de data: URL que la
    anterior); solo se ha visto el estado vacío y la apertura por `/browser`.
35. **`[~]` El historial guarda la traza (`tool_events`) pero no los
    fotogramas del navegador ni el documento en streaming**: tras recargar,
    el panel vuelve vacío y el documento se abre por su id desde «Abrir el
    documento» en el carril o desde la Biblioteca. Igual que la anterior.
36. **`[~]` Editor de documentos básico.** Textarea con vista previa,
    versiones, PDF, archivar y sugerencias. Lo que no tiene: diff palabra a
    palabra al aceptar/rechazar cambios del agente, visor y anotación de PDF,
    firma, borradores de correo, ordenar y limpiar la biblioteca
    (`document.js`, 11 000 líneas). Son islas candidatas (DECISIONES §10).
37. **`[!]` Trampa de herramientas: `Get-Content | Set-Content` en
    PowerShell 5.1 destroza el UTF-8** (mojibake en tildes y emoji). Para
    tocar fuentes desde el puente, `edit_block` o `[System.IO.File]` con
    `UTF8Encoding($false)`; nunca los cmdlets sin `-Encoding`.

## Añadido tras el lote I (04-09-2026, sesiones y compositor)

38. **`[!]` Perdida una conversación de prueba (`6742a8f7…`) por el
    incógnito.** Studio compartía con la anterior la clave de sesión
    `ody-incognito-sessions`; `sessions.js` sigue corriendo debajo del
    piloto y borra lo que hay en esa lista salvo la sesión que ÉL tiene en
    pantalla, que nunca es la de Studio. Además la limpieza propia listaba
    la sesión normal actual. Arreglado con clave propia
    (`faustus_studio_incognito`) y `all.filter(id => id === keep)`. Regla:
    ningún estado mutable de `sessionStorage`/`localStorage` se comparte con
    la anterior mientras siga cargada debajo.
39. **`[!]` Los atajos de la anterior actuaban dos veces.** Ctrl+Alt+N creó
    una sesión vacía propia de `keyboard-shortcuts.js` además de la de
    Studio. Ahora Studio escucha `keydown` en `window` en fase de captura y
    detiene la propagación de cualquier combinación que reconozca. Ojo si
    se añade un atajo nuevo: hay que registrarlo en `DEFAULT_KEYBINDS`.
    Y el valor por defecto de la barra es `ctrl+b`, no `ctrl+alt+b`
    (`settings.js` manda).
40. **`[~]` `/api/sessions/archived` devolvía 403 con `AUTH_ENABLED=false`**
    (la anterior tampoco podía abrir su archivo en el 7001). Corregido en
    `session_routes.py` como ya hacía `/sessions/export`: sin usuario y con
    auth desactivada, sin filtro de dueño.
41. **`[~]` Micrófono y voz sin probar con hardware.** `startDictation`
    (MediaRecorder → `/api/stt/transcribe`, o `SpeechRecognition` si el
    servidor da 503) y `speak` (`/api/tts/synthesize` o `speechSynthesis`)
    están escritos contra los mismos endpoints que la anterior; el
    navegador del puente no concede permiso de micrófono. Probar a mano.
42. **`[x]` Bundle principal en 375 KB / 117,8 KB gzip** (presupuesto 350 /
    120). Resuelto haciendo `Studio.tsx` ruta perezosa que se precarga en
    `requestIdleCallback` tras el primer pintado: `app` 260,5 / 83,4 KB gzip y
    `Studio` 111,8 / 35,1 KB. Queda margen para las pantallas que faltan;
    cada una entra como chunk propio.
43. **`[~]` «Manage Chats» solo en parte.** Archivadas con búsqueda y
    recuperar, ordenar con IA y limpiar vacías; la biblioteca de chats con
    estadísticas y filtros por modelo y fecha sigue en la anterior.
44. **`[~]` Casillas de selección múltiple**: la fila es una rejilla de dos
    columnas (`minmax(0,1fr) auto`) pensada para enlace + botón; con la
    casilla delante hacía falta `data-selecting` para invertir las columnas.
    Cualquier otro elemento que se anteponga al enlace necesita lo mismo.

## Añadido tras el lote J (05-09-2026, Notas y «Herramientas»)

45. **`[~]` Dos bucles de recordatorios posibles.** Studio solo dispara los
    suyos si no encuentra `#notes-pane` (el `notes.js` de la anterior
    cargado debajo). El día que la anterior se retire, el bucle de Studio
    pasa a ser el único y hay que probarlo con una nota con hora
    (`fire-reminder`, `Notification`, avance de la repetición).
46. **`[~]` Notas: lo que sigue en la anterior.** Dibujar (lienzo → PNG por
    `/api/upload`), foto adjunta, fondo de imagen propio, selección múltiple
    con acciones en lote, edición táctil a pantalla completa, arrastre con
    pulsación larga en móvil, insignia de vencidas en el carril. Son islas
    o una segunda pasada; ninguna cambia el modelo de datos.
47. **`[!]` Contextos de apilamiento por la animación de entrada.** Todo
    hijo directo de `.fs-screen` se anima con `fs-rise … both`; un
    desplegable que sobresale de su tarjeta cae debajo de los hermanos que
    vienen después. Regla: el contenedor de tarjetas con menús lleva
    `position: relative; z-index: 1`, y la tarjeta abierta
    `:has(.fs-qmenu__list) { z-index: 5 }`. Vale para cualquier pantalla
    nueva con menús por fila.
48. **`[~]` Estilos compartidos entre chunks.** `.fs-qmenu` estaba en
    `studio.css` (chunk de Studio) y Notas lo usaba sin cargarlo. Ahora vive
    en `styles/components.css`. Cualquier componente de `components/` que
    use una pantalla fuera de Studio necesita su CSS en `styles/`, no en
    `screens/studio.css`. Lo mismo para `.fs-studio__chip` y `.fs-select`:
    Notas lleva copias (`.fs-chip`, `.fs-field`); si una tercera pantalla
    las necesita, subirlas a `components.css`.
