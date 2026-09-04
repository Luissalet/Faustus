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
