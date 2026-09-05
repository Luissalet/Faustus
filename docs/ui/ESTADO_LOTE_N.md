# Estado del lote N - Ajustes como pantalla

Fecha: 05-09-2026. Rama `feat/studio-ui`. Verificado en el 7001.

## Qué entra

- **Ajustes** (`/settings`, chunk propio de 38 KB; `adapters/settings.ts`
  ampliado sobre `/api/auth/settings`, `/api/agent/settings/schema` y
  `/api/model-endpoints`). Enlace en el pie de la barra (encima de
  «Interfaz anterior»), en la paleta, `/setup` y el atajo `settings`
  (Ctrl+,). `?s=<sección>` recuerda la sección en la URL.
  - **Modelos**: lista de endpoints con estado (punto verde en línea,
    modelos, clase, clave), releer modelos (`/models?refresh=true`),
    activar/desactivar (PATCH sin cuerpo), quitar (con confirmación);
    añadir con nombre, URL, clave, tipo y clase, y «Probar»
    (`/model-endpoints/test`).
  - **IA por defecto**: chat, tareas de fondo, utilidad y sus alternativas,
    visión y alternativas, workers, Deep Research (modelo, buscador,
    tokens), imágenes (activa, modelo, calidad), profesor, salida
    estructurada local, estilo de documentos, versiones del chat. Los
    selectores de modelo se llenan con los modelos del endpoint elegido.
  - **Voz**: TTS (activo, proveedor navegador/local/endpoint, modelo, voz,
    velocidad) y STT (activo, proveedor, modelo, idioma).
  - **Búsqueda**: proveedor, resultados, URL de SearXNG o Firecrawl, la
    clave que toque (Brave, Serper, Tavily, Google PSE + cx), SafeSearch,
    cadena de respaldo.
  - **Recordatorios**: canal (navegador, correo, ntfy, webhook) con sus
    campos, síntesis con IA y persona.
  - **Agente**: formulario generado del esquema del servidor (97 opciones
    en 19 grupos: bucle, verificación, contexto, sub-agentes, colas,
    navegador, escritorio, visión, guardia de comandos, procedencia…), cada
    grupo plegable, filtro por texto, la clave en monoespaciada, marca de
    «reinicio», resaltado de lo cambiado, números acotados a los límites
    del esquema.
  - **Atajos**: los 20 keybinds con etiqueta en castellano; «Cambiar» graba
    la combinación (Retroceso la vacía, Escape cancela), duplicados en rojo,
    «Valores por defecto»; al guardar se invalida la caché que usa Studio.
  - **Sistema**: URL pública, compartir valores por defecto, rutas extra,
    prompt de urgencia, preferencia de GPU, opciones de carga, skills
    (máximo inyectado, confianza mínima).
  - **En la interfaz anterior**: tarjetas para Modelos locales,
    Integraciones, Cuentas de correo, Herramientas y MCP, Cuenta, Usuarios,
    Apariencia y tema → `/?shell=legacy#settings/<tab>`.
  - Cada sección guarda solo las claves que difieren (`POST` parcial) y
    enseña «Hay cambios sin guardar»; el servidor devuelve el conjunto
    completo y la pantalla lo adopta.
- Servidor:
  - `GET/POST /api/auth/settings` aceptan el modo `AUTH_ENABLED=false`
    como administrador (misma regla que `core.middleware.require_admin`):
    antes el POST devolvía «Admin only» y el GET escondía las claves también
    a la anterior en el 7001.
  - `static/app.js`: `/?shell=legacy#settings/<tab>` (y `/settings#<tab>`)
    abren el modal de ajustes de la anterior en esa pestaña.
  - `app.py`: ruta `/settings` en la lista blanca.

## Verificado en el navegador

- Modelos: el endpoint `127.0.0.1:11434` en línea con sus 6 modelos.
- IA por defecto: utilidad → `qwen3.5:9b`, Guardar → «Guardado.», el
  servidor devuelve `utility_model: qwen3.5:9b` y la barra pasa a «Sin
  cambios».
- Agente: 97 opciones en 19 grupos; «Agent loop» abierto muestra Max steps
  (20), Tool call limit (0), Auto-continue (1), Reliability harness (on)…
- Atajos: «Abrir el Cookbook» → Cambiar → Ctrl+Alt+K → Guardar → guardado
  (`keybinds.open_cookbook = ctrl+alt+k`).
- Enlace «Integraciones» → la anterior abre Settings en Integrations.

## Sin verificar

- Añadir un endpoint nuevo de verdad (el formulario y «Probar» llaman a
  las rutas de la anterior; no hay otro servidor a mano).
- Voz, Búsqueda, Recordatorios y Sistema: guardan por el mismo camino que
  IA por defecto; los campos concretos no se han probado uno a uno.
