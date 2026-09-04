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
