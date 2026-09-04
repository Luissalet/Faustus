# Paridad funcional — el libro que impide perder funciones

Regla (Luis, 04-09-2026): **redistribuir y cambiar, sí; tener menos funciones,
nunca.** Nada de la interfaz anterior se retira hasta que su fila de abajo
diga «Migrado» y esté verificado en el 7001. Mientras tanto, cada función
sigue accesible desde «Interfaz anterior» (`?shell=legacy`), que se conserva
**idéntica** durante todo el piloto.

Cómo leerlo: **Migrado** = existe en Studio con lo mismo o más; **Parcial** =
existe pero le falta algo concreto (dicho al lado); **Anterior** = solo en la
interfaz anterior, alcanzable desde Studio con un clic. Las herramientas del
agente (85 en `src/tool_schemas.py`) no aparecen: viven en el servidor y no
las toca ninguna pantalla.

## 1. Destinos y páginas

| Función | Interfaz anterior | Studio | Estado |
|---|---|---|---|
| Chat / agente | `/` + `chat.js` | `/studio` | **Parcial** — ver §3 y §4 |
| Proyectos (lista, detalle, contexto, memoria, objetivos) | `projects-modal` | `/projects`, `/projects/{id}` | **Parcial** — falta crear/editar/borrar proyecto y los mandos del agente por proyecto (workspace de confianza, propuesta, checkpoints, tests, revisor); pestaña *Agent activity* |
| Biblioteca de documentos | `/library`, `documentLibrary.js` | `/library` (federada con galería) | **Parcial** — abrir/editar/versionar un documento sigue en la anterior |
| Galería de imágenes | `/gallery`, `gallery.js`, `galleryEditor.js` | `/library?type=imagen` | **Parcial** — editor de imagen, borrar, descargar y tirar de una imagen al chat: anterior |
| Tareas programadas | `/tasks`, `tasks.js` | `/automations` | **Parcial** — crear, editar, pausar, ejecutar ahora: anterior |
| Actividad (runs de tareas, media, aprobaciones) | repartido en `/tasks`, `/gallery`, pill de aprobaciones | `/activity` | **Parcial** — aprobar desde la lista y abrir el detalle del run: anterior |
| Inicio | pantalla `welcome` | `/` | **Migrado** (y más: continuaciones, aprobaciones, quick starts) |
| Notas | `/notes` | — | **Anterior** |
| Calendario | `/calendar` | — | **Anterior** |
| Correo | `/email` | — | **Anterior** |
| Brain / memoria (`memory-modal`, `/memory`) | sí | — | **Anterior** |
| Cookbook (servir/descargar modelos) | `cookbook-modal`, `/cookbook` | — | **Anterior** |
| Deep Research | panel propio | — | **Anterior** |
| Compare (varios modelos a la vez) | `compare/` | — | **Anterior** |
| Tournament | `tournament-modal` | — | **Anterior** |
| Workers (dispatch) | `/workers` | — | **Anterior** |
| Expertos | `experts-modal` | — | **Anterior** |
| Procedencia (grafo) | `provenance-modal` | — | **Anterior** |
| Historial importado | `history-modal` | — | **Anterior** |
| Agent runners | `agent-runners-modal` | — | **Anterior** |
| Agent definitions | `agent-defs` | — | **Anterior** |
| Workspace de la sesión (elegir carpeta, `Change folder`) | pantalla de bienvenida y compositor | hereda la carpeta ya vinculada | **Parcial** |
| Skills (auditoría, publicar, importar) | `skills-audit-panel` | — | **Anterior** |
| Ajustes (todas las secciones, 81 opciones del agente, endpoints, modelos locales, atajos) | `settings-modal` | — | **Anterior** |
| Tema (editor de colores, importar/exportar) | `theme-modal` | tokens propios; los temas personalizados siguen aplicando vía `legacy-bridge.css` | **Anterior** (editor) |
| Fondos | `/backgrounds` | — | **Anterior** |
| Login | `/login` | — | **Migrado** (no cambia) |

## 2. Barra lateral y sesiones

| Función | Studio | Estado |
|---|---|---|
| Lista de conversaciones, buscar (Ctrl+K) | sí (`/studio`, filtro + paleta) | **Migrado** |
| Nueva conversación | sí | **Migrado** |
| Renombrar, archivar, borrar, favorito, carpeta, importante | — | **Anterior** |
| Selección múltiple (archivar/borrar) | — | **Anterior** |
| Ordenar sesiones | — (por recencia fija) | **Anterior** |
| Manage Chats (biblioteca de chats) | — | **Anterior** |
| Exportar conversación (md, txt, json, html, docx, pdf, lote) | — | **Anterior** |
| Fork, truncar, versiones, compactar (`/versions`, `/checkpoints`, `/compact`) | — | **Anterior** |
| Modo Nobody / incógnito | — | **Anterior** |
| Colapsar barra lateral | cajón en ≤1023 px | **Migrado** |

## 3. Compositor y turno

| Función | Studio | Estado |
|---|---|---|
| Enviar, Shift+Enter, parar, Escape | sí | **Migrado** |
| Modo chat / agente, plan | sí | **Migrado** |
| Web search, shell access | sí | **Migrado** |
| RAG por sesión | — | **Anterior** |
| Deep Research, Group chat, Persona, Compare desde el compositor | — | **Anterior** |
| Elegir modelo y endpoint, refrescar | paleta (sin refrescar) | **Parcial** |
| Carpeta de workspace (elegir, quitar, indicador) | hereda la de la sesión | **Parcial** |
| Adjuntos (ficheros, imágenes, pegar) | — | **Anterior** |
| Menciones `@fichero`, `@ruta:línea`, `@expert:` | — | **Anterior** |
| `#` regla permanente, `/remember` | — | **Anterior** |
| Comandos `/` (todos) | — | **Anterior** |
| Citar selección ❝ | — | **Anterior** |
| ↑ recuperar último mensaje | — | **Anterior** |
| Controles por chat (`/temp`, `/maxtokens`, `/topp`, `/think`, `/gen`) | — | **Anterior** |
| Presets / personajes / prompt del sistema | — | **Anterior** |
| Dictado (STT) y lectura (TTS) | — | **Anterior** |

## 4. Transcript

| Función | Studio | Estado |
|---|---|---|
| Streaming, razonamiento, métricas, fuentes web, imagen generada | sí | **Migrado** |
| Traza de herramientas con comando y salida | sí (en el carril) | **Migrado** (más visible que antes) |
| Aprobación de herramienta (aprobar / toda la tarea / denegar) | sí | **Migrado** |
| `ask_user` con opciones | sí (botones) | **Migrado** |
| Fallback de modelo, errores del servidor | sí | **Migrado** |
| Editar, regenerar, borrar mensajes, Undo | — | **Anterior** |
| Copiar mensaje / código | copiar texto del navegador | **Parcial** — falta el botón de copiar en bloques de código |
| Markdown completo (tablas, notas al pie) | lector reducido | **Parcial** |
| Tarjetas 🛡 del arnés (Turn summary, Verified/Unverified) | — | **Anterior** |
| Panel Progress (`todowrite`), plan en vivo (`plan_update`) | — | **Anterior** |
| Tablero de sub-agentes (`delegate_agents`), steer/stop por worker | — | **Anterior** |
| Vista en vivo del navegador (`browser_view`) | — | **Anterior** |
| Documentos del editor (`doc_*`), sugerencias | — | **Anterior** |
| Ficheros editados con diff y revertir, checkpoints | — | **Anterior** |
| Context ledger, pill de uso de GPU, salud de servicios | — | **Anterior** |
| Fichas de mención pulsables, visor lateral de ficheros | — | **Anterior** |
| Scroll to bottom | automático mientras sigues el stream | **Migrado** |

## 5. Atajos de teclado

| Atajo | Studio | Estado |
|---|---|---|
| Ctrl+K buscar / navegar | sí | **Migrado** |
| Escape cancelar | sí (parar el stream) | **Migrado** |
| Ctrl+Alt+N nuevo chat, Ctrl+Alt+B barra, Ctrl+Alt+F favorito, Ctrl+Alt+D borrar, Alt+Shift+T TTS, Ctrl+Alt+I incógnito, Ctrl+, ajustes, Ctrl+/ foco, Ctrl+Alt+C calendario, y los reconfigurables | — | **Anterior** |

## 6. Orden de cierre de la deuda

Lo que más se usa a diario va primero. Cada bloque cierra sus filas y solo
entonces se retira la parte legacy correspondiente (DECISIONES_UI.md §4):

1. Compositor: adjuntos, `@` menciones, `#`/`/remember`, comandos `/`,
   workspace elegible, RAG, presets.
2. Transcript: editar/regenerar/borrar con versiones, copiar código,
   tarjetas del arnés, plan y Progress, ficheros editados con diff.
3. Sesiones: renombrar/archivar/borrar/favorito/carpeta, exportar,
   fork/compactar, incógnito.
4. Sub-agentes, navegador en vivo, documentos del editor.
5. Ajustes completos, tema, atajos reconfigurables.
6. Notas, calendario, correo, brain, cookbook, research, compare, tournament,
   workers, expertos, procedencia, importación, runners, skills, fondos.

Hasta el punto 6 la barra de navegación de Studio no puede quitar «Interfaz
anterior», y `?shell=legacy` no puede dejar de servir la aplicación completa.
