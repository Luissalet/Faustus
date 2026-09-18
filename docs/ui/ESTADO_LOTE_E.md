# Estado del lote E — Studio, y el segundo pase de carácter

Rama: `feat/studio-ui`. Fecha: 04-09-2026.

Con este lote **los seis destinos del carril están migrados**: Inicio, Studio,
Proyectos, Biblioteca, Automatizaciones y Actividad. `NotMigrated` ya solo
atiende rutas que no existen. Falta lo que dice `OBJETIVOS_UI.md` dentro de
cada pantalla, y falta retirar el legacy correspondiente (§4 de DECISIONES).

## Studio (`/studio`, `?s=<session>`)

Archivos: `studio/src/adapters/chat.ts`, `studio/src/screens/Studio.tsx`,
`studio/src/screens/ModelPicker.tsx`, `studio/src/screens/rich.tsx`,
`studio/src/screens/studio.css`.

Habla con **los endpoints de siempre**, ninguno nuevo:

| Qué | Endpoint |
|---|---|
| Lista de conversaciones | `GET /api/sessions` |
| Historial | `GET /api/history/{id}` → `{name, model, history[{role, content, metadata}]}` |
| Crear conversación | `POST /api/session` (FormData `name`, `endpoint_url`, `model`, `endpoint_id`, `skip_validation=true`) |
| Modelos | `GET /api/models?background=false` → `items[{url, endpoint_id, endpoint_name, models[]}]` |
| Turno | `POST /api/chat_stream` (FormData `message`, `session`, `mode`, `plan_mode`, `allow_bash`, `allow_web_search`, `workspace`, `selected_model`, `selected_endpoint_id`, `selected_endpoint_url`) con `X-Tz-Offset`/`X-Tz-Name` |
| Aprobación | mismo POST con `message=""`, `tool_approval_id`, `tool_approval_decision` (`approve` / `approve_task` / `deny`) |
| Parar | `POST /api/chat/stop/{id}` + abort del fetch |

El stream son líneas `data: {...}` con `event: error` delante cuando toca. Lo
que Studio traduce: `delta` (con `thinking: true` para el razonamiento),
`tool_start`, `tool_progress`, `tool_output`, `agent_step`, `ask_user`,
`metrics`, `web_sources`, `generated_image`, `fallback`, `agent_terminal`,
`chat_terminal`, `error`, `[DONE]`. El resto se descarta sin ruido
(PENDIENTES_UI.md §18).

### Verificado en el 7001 con `qwen3.5:9b`

- Chat: pregunta → razonamiento plegado → respuesta en streaming → métricas
  (`626 tok · 17.0 s · contexto 0%`).
- Agente con terminal: `ls -la` pide permiso → tarjeta «Necesita tu permiso» →
  Aprobar → el paso pasa de *esperando* a *ejecutando* a *completado* en el
  mismo nodo del carril → respuesta con el recuento. Sin duplicar el paso: el
  servidor reemite `tool_start` al reanudar y Studio lo reconoce.
- Error honesto: con Ollama apagado, «Cannot reach http://127.0.0.1:11434» y
  «local endpoint returned 404 (model not found)» aparecen como aviso en el
  turno, no como un spinner eterno.
- Historial de una sesión creada por la interfaz anterior (bench t10c) abre y
  se lee.

### Decisiones tomadas construyéndolo

- **El selector de modelos es una paleta, no un desplegable.** El `Menu` de
  Radix nunca se había usado en una pantalla y el tree-shaking lo dejaba
  fuera; al usarlo, el bundle subió **80 KB** (floating-ui). cmdk ya está en
  el bundle por Ctrl+K y además busca entre cuarenta modelos. 344 KB / 110 KB
  gzip después del cambio, dentro del presupuesto.
- **Las pantallas secundarias son chunks perezosos.** Proyectos, Biblioteca,
  Automatizaciones y Actividad cargan al abrirse. Inicio y Studio van en el
  bundle inicial porque son la aplicación.
- **Sin librería de Markdown.** `rich.tsx` entiende bloques de código, código
  inline, negrita, encabezados, listas y enlaces. Lo que no entiende lo enseña
  como texto, que es el fallo correcto en un transcript. Una implementación
  completa costaba 40–90 KB para tablas que un chat casi nunca trae.
- **El transcript no se re-monta al crear la sesión.** El primer mensaje crea
  la sesión, escribe `?s=` y sigue el stream: `freshRef` evita que el efecto
  de historial aborte el fetch en marcha (el primer intento sí lo hacía).
- **Fechas del servidor como UTC.** `parseStamp` en `adapters/home.ts`; antes
  una sesión de hace dos minutos decía «hace 2 h».
- **`<details>` legacy neutralizado.** `base.css` hace `all: revert` sobre
  `details`/`summary` dentro de `.fs-app`; el legacy los pintaba como un
  callout beige con ▶.

## Segundo pase de carácter: «le falta sauce, sobre todo en claro»

Diagnóstico: la capa de candy se afinó en oscuro, donde la luz **suma**
(`mix-blend-mode: screen`, halos). Sobre un lienzo casi blanco, `screen` no
hace nada y el ember al 22 % es una mancha rosa.

Lo que cambia, todo en `tokens.css` como tokens con receta propia para claro:

- Aurora con `multiply` y al 80 % en claro (`--fs-aurora-blend`,
  `--fs-aurora-opacity`); en oscuro sube de 0.55 a 0.7.
- Lienzo de **papel** con gradiente cálido (`--fs-paper`) y una **retícula de
  puntos** (nodos: el motivo, como textura) que se desvanece hacia abajo
  (`--fs-dots`, `.fs-shell::after`).
- Sombras de color en lugar de halos (`--fs-panel-shadow`, `--fs-lift-shadow`)
  sobre paneles, tiles, la traza, el compositor y el botón de enviar.
- Gradiente de acento coral→ámbar (`--fs-accent-gradient`) en el botón
  primario, en enviar y en el pulgar del segmentado Chat|Agente, con deriva
  lenta.
- La señal que recorre el carril de navegación recorre también la cabecera
  del Studio (`.fs-studio__head::after`).
- Burbuja del usuario con gradiente de ember y dos chaflanes.

Todo se apaga con `prefers-reduced-motion`, y ningún literal de color entra
en el árbol: las guardas siguen en verde (18/18).

## Capturas

`docs/ui/after/` (oscuro) y `docs/ui/after-light/` (claro), tres viewports,
nueve pantallas: se añaden `08_studio_empty` y `09_studio_session`.
`scripts/shot_studio.py` acepta `ODYSSEUS_STUDIO_SESSION` y
`ODYSSEUS_STUDIO_OUT`.
