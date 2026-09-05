# Estado del lote AM — Se va la interfaz anterior

Fecha: 05-09-2026. Rama `feat/studio-ui`. Cierra UI-061: fuera el DOM, el CSS
y el JavaScript de la interfaz que Studio sustituye, fuera el flag, y fuera
las salidas de emergencia hacia ella. Después de este lote **Studio no es una
alternativa: es la interfaz**.

## Lo que se ha borrado

| | |
|---|---|
| `static/app.js` | 196 KB |
| `static/js/` | 120 ficheros, 7,2 MB |
| `static/style.css` | 1,5 MB |
| `static/index.html` | 256 KB → **84 líneas** |
| `static/lib/` | katex, mermaid, highlight, html2pdf, qrcode (solo los usaba la anterior) |
| sandboxes | `modal-control-variants.html`, `wave-variants.html`, `whirlpool-variants.html` |
| el flag | `shell/flag.ts`, `faustus_studio_shell`, `?shell=studio`, `?shell=legacy` |
| tests | 75 ficheros de contrato sobre el JS que ya no existe |

`static/lib/` conserva `docx.umd.min.js`, `xlsx.full.min.js` y
`mammoth.browser.min.js`, que Studio carga bajo demanda para exportar e
importar documentos.

### El nuevo `index.html`

84 líneas: los metadatos, el manifiesto, el icono, **un** script de módulo
(`/static/studio/studio.js`, con su `?v=` de hash de contenido) y un bloque
en línea que aplica el tema **antes de la primera pintura** —el atributo
`data-theme`, las cinco variables de un tema propio, la densidad, el tamaño
de texto y el color de fondo—. Sin él la página parpadea al color del
sistema y luego cambia, que es lo peor de un tema oscuro en un SO claro.

### El service worker

Reescrito. La lista de precacheo eran **cincuenta y tantas** rutas a mano;
ahora son **dos**: el shell y su entrada. Los chunks llevan hash de
contenido, así que una lista escrita a mano estaría obsoleta el mismo día;
se recogen solas la primera vez que se abre una pantalla, que es también
cuando empiezan a importar sin conexión. Y la navegación devuelve el shell
para **cualquier** ruta, no solo `/`: recargar en `/calendar` ya abre al
instante y funciona sin red.

### `legacy-bridge.css` → `user-theme.css`

No era de la interfaz anterior: es el mecanismo por el que Studio soporta
los dieciséis temas de fábrica y los personalizados (misma clave
`odysseus-theme`, mismos cinco colores). Se ha renombrado y reescrito el
comentario, porque quien escribe esas variables ahora es `appearance.ts`.

### Rutas viejas que siguen vivas

`/gallery`, `/tasks` y `/brain` estaban en marcadores. El servidor las sigue
sirviendo y el router redirige cada una a la pantalla que se quedó con el
trabajo (`/library?type=imagen`, `/automations`, `/memory`).

## Lo que había que traer antes de borrar

Borrar la anterior destapó **funciones que nunca se migraron**. La regla es
que no puede haber ninguna función menos, así que se han portado, no
apuntado:

### 1. Salud de servicios · `adapters/services.ts` + sección en Vitales

`/api/diagnostics/services` sondea ChromaDB, SearXNG, ntfy, correo y los
endpoints de modelos, y explica cada uno en palabras con una pista y un
comando. **En Studio no lo llamaba nadie.** Es justo el fallo que no se
anuncia: se cierra Docker, el almacén vectorial cae a búsqueda por palabras,
las respuestas empeoran y nada lo dice. Ahora está en el panel de Vitales
con su botón **Reconectar**, que rehace los almacenes y vuelve a sondear sin
reiniciar. Si la cuenta no es admin, no se enseña nada (403 → se calla).

### 2. Catálogo de servidores MCP · `lib/mcpPresets.ts`

Quince servidores listos (Gmail, IMAP/SMTP, CalDAV, Google Calendar, Drive,
GitHub, Slack, Notion, Linear, Brave, Playwright, Filesystem, Memory,
Postgres, Todoist) con sus pasos de instalación. En Studio había que saberse
el nombre del paquete de npm, la lista de argumentos y la ortografía exacta
de cuatro variables de entorno: eso no es un paso de configuración, es una
investigación. El de correo trae además un desplegable de proveedor que
rellena las dos direcciones (IMAP y SMTP) que nadie recuerda, y el de Gmail
las rutas OAuth **contenidas** (`gmail/gcp-oauth.keys.json`, nunca
`~/.gmail-mcp`), que era además una fila de las pruebas de seguridad.

### 3. Limpieza de fences de herramientas en vivo · `lib/fences.ts`

Un bloque ```` ```read_file ```` ya ejecutado se quedaba en la burbuja como
código hasta recargar (el servidor sí lo quita del historial). La lista de
etiquetas viene de `GET /api/tools` en tiempo de ejecución: una copia a mano
se desincroniza el día que se añade una herramienta, y el síntoma —los
fences de *una* herramienta se quedan y los demás no— es imposible de
atribuir. `bash` y `python` quedan fuera a propósito, y solo se quita un
bloque cuyo contenido **parsea como JSON**.

### 4. Entrar con una suscripción · `adapters/deviceAuth.ts` + `settings/DeviceSignIn.tsx`

Copilot y la suscripción de ChatGPT no tienen clave que pegar: se entra como
en una tele —un código corto, una página, y el servidor sondea—. Las tres
rutas existían (`/device/start|poll|cancel`) y **no las llamaba nadie**. El
código es lo más grande de la tarjeta porque se teclea en otro sitio, a
veces en otro aparato; la pestaña la abre un clic, nunca sola; y un flujo
abandonado se cancela al salir en vez de quedarse en memoria del servidor.

### 5. ¿Cabe este modelo? · `adapters/fit.ts` + `ModelPalette`

`/api/models/fit` mide los pesos contra la VRAM libre y responde por modelo.
**Sin llamador.** Elegir un modelo local que no cabe no da error: Ollama lo
carga, empuja el resto a la CPU y va diez veces más lento sin que nada lo
diga. Ahora cada fila del selector lleva su veredicto —**cabe / justo / no
cabe**— en palabra y en color, con la frase del servidor al pasar por
encima. Cuando el servidor no puede saberlo, `state` no viene y **no se
pinta nada**: un veredicto inventado es peor que ninguno.

De paso, **dos etiquetas, un modelo**: `qwen3.8:27b-q4_K_M` y
`claude-sonnet-4-5:latest` pueden ser los mismos pesos con dos nombres. Se
marcan comparando **digests**, nunca el parecido del nombre —`q4_K_M` y
`q8_0` se parecen y son genuinamente distintos, que es la única razón para
abrir el menú—.

### 6. «Ajustar a la VRAM» · en las opciones de un modelo local

`/api/system/vram-fit` calcula cuánto contexto y cuántas capas caben.
Tampoco lo llamaba nadie. Ahora hay un botón que lo mide y **rellena
`num_ctx` y `num_gpu`**, con los pasos del servidor debajo. Cuando el
servidor devuelve `num_gpu: null` («que decida Ollama») el campo se deja
vacío, que no es lo mismo que escribir un número.

### 7. Cosas más pequeñas

- `tool_approval_resolved` / `ask_user_resolved`: la tarjeta de permiso
  desaparece cuando la decisión se toma en otra pestaña o el servidor la
  reproduce, en vez de quedarse pidiendo una respuesta ya dada.
- **Diffusers en un Windows remoto**: `backendChoices` lo ofrecía y el
  comando no puede funcionar allí. Bug real, encontrado por la prueba nueva.
- `/setup` escrito solo ahora despliega los proveedores, en vez de una fila
  repitiendo la palabra ya escrita.
- Borrar un mensaje pregunta antes (lote AL) y dos textos que se habían
  quedado en castellano a pelo (`Harness.tsx`, `chat.ts`) pasan por `t()`.

## Pruebas

Los 75 ficheros borrados eran contratos sobre texto fuente de un JavaScript
que ya no existe. Lo que valía se ha **reapuntado a Studio, y a comportamiento
en vez de a substrings** donde se podía:

- `studio/checks/serve.check.mjs` + `tests/test_studio_serve_js.py` (nuevo):
  construye comandos de serve y los lee. CPU-only sin banderas de GPU,
  `python` y no `python3` en Windows, el `llama-server` nativo en local
  contra el módulo de python en remoto, un swap vacío que no se convierte en
  una bandera vacía, la plantilla de Gemma 4, el proyector de visión
  escaneado, comillas y puertos.
- `studio/checks/panel.check.mjs` + `tests/test_studio_panel_js.py` (nuevo):
  la lista blanca de rásters (un SVG es un script), las ocho capturas como
  tope, el panel que se abre **una vez** por turno y no cada vez, el
  indicador «en vivo» y las capturas de escritorio.
- `studio/checks/markdown.check.mjs`: los fences de herramientas.
- `studio/checks/commands.check.mjs`: la expansión de subcomandos.
- `tests/test_device_sign_in_ui.py`, `tests/test_cookbook_local_windows_llama.py`,
  `tests/test_cookbook_task_completion.py`, `tests/test_truncate_failure_aborts.py`
  (nuevos o renombrados).
- Reapuntados a Studio: `test_browser_view`, `test_gpu_policy`,
  `test_dispatch_reliability`, `test_asset_versioning`, `test_security_regressions`,
  `test_blind_compare_redaction`, `test_tool_approval_task_scope`,
  `test_external_context_tool_gate`, `test_agent_defs`, `test_changeset_turn_ui`,
  `test_prove_surfaces`, `test_context_ledger_wiring`, `test_backup_wiring`,
  `test_research_presets_wiring`, `test_gpu_memory_wiring`, `test_faustus_mark`,
  `test_model_picker_vram_fit`, `test_workspace_entry_points`,
  `test_live_strip_email_tool_fences`, `test_upload_multifile`, `test_static_checks`.
- Nueva guarda en `tests/test_studio_guards.py`: `SERVER_ROUTES` de
  `routes.ts` contra los `@app.get` de `app.py`. Una ruta que el servidor no
  sirve es un 404 al recargar, que es justo el fallo que los enlaces
  profundos existen para evitar.

## Verificado en el 7001

Arranque sin nada de la anterior en la página (ni un `/static/js/`), consola
limpia, las 21 rutas sirviendo el shell y una inexistente devolviendo 404.
Salud de servicios con su aviso real («2/3 endpoints», con la pista y
`ollama list`). Catálogo MCP: Gmail y Correo, con el desplegable de
proveedor rellenando `imap.fastmail.com`/`smtp.fastmail.com`. Device flow
con un código real de GitHub (`0D67-B523`) y su página. Veredicto de VRAM en
el selector (cabe / no cabe) y el aviso de «igual que» entre dos etiquetas
con el mismo digest. «Ajustar a la VRAM» midiendo un modelo de 16,5 GB y
rellenando 32768 con su explicación.

## La segunda cosa que destapó el borrado: los tests recortados

Los ~110 ficheros de test de la anterior se trataron en tres oleadas:
borrar los que eran contrato puro sobre el JS borrado, y **recortar** los
mixtos dejando sólo lo que no dependía de él. El recorte de la tercera
oleada se hizo con un script, y el script quitó las líneas que **leían** el
fuente borrado (`_SOURCE = (…/static/js/x.js).read_text()`) pero dejó los
tests que las usaban. Resultado: 19 ficheros con `NameError` y ~100 rojos
que no existían antes. Salió en la suite completa, no en las tandas
dirigidas, que es exactamente para lo que sirve pasarla entera antes de
cerrar.

Cada uno se ha revisado contra Studio, no borrado en bloque:

| lo que vigilaba | qué se ha hecho |
|---|---|
| `:blush:` → 😊 (`emojiShortcodes.js`) | **faltaba en Studio**: portado a `lib/emoji.ts` con la tabla literal, más `replaceShortcodesInProse` (el código entre comillas no se toca) y 27 comprobaciones en `markdown.check.mjs` |
| chat.js adivinaba la causa de un fallo por el texto del error | `adapters/api.ts` lee el `detail` de FastAPI; guarda nueva en `test_studio_guards.py` para que la heurística no vuelva |
| sufijo ordinal («Monthly on 21th») | Studio escribe «Día 21 de cada mes»: el fallo no puede darse |
| `select option` con colores del tema | `color-scheme` en `tokens.css` lo resuelve como toca; el parche CSS sobra |
| zonas de anclaje de los modales flotantes | Studio no tiene modales flotantes |
| `_foldSignature` y `<br/>` | `lib/mail.ts` pliega por DOM, no por regex de `<br>` |
| lote leído/no leído sin escribir en el proveedor | `Email.tsx` llama a `markRead`/`markUnread` por correo y cuenta los que fallan |
| modelo elegido a mano que sobrevive a la recarga | `faustus_studio_route` en `localStorage` |
| preset recordado | `faustus_studio_preset` |
| `container_local` al registrar un endpoint del Cookbook | `adapters/cookbook.ts` |
| endpoint local vs de pago, etiqueta de proveedor por puerto | Studio enseña el nombre que el servidor guardó del endpoint, que es lo que el propio test defendía |
| resto (prewarm, toasts, arranque de sesiones, shell de ajustes, segmentador de streaming, provenance por ronda) | contrato sobre ficheros que ya no existen; el comportamiento vive en pantallas con sus propias pruebas |

Y dos guardas que fallaban por el borrado, arregladas de verdad y no
silenciadas: `test_faustus_brand` apuntaba al `placeholder` del HTML servido
y a `static/js/storage.js` (ahora al carril de `AppShell.tsx` y a las claves
de `composer.ts` / `appearance.ts`), y `test_docs_no_orphan_images` exigía
que bajo `docs/` no hubiera Markdown: `docs/ui/` queda declarado como lo que
es —el registro de ingeniería de este trabajo, no una página del sitio.
