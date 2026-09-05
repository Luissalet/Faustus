# Estado del lote K - Memoria como pantalla

Fecha: 05-09-2026. Rama `feat/studio-ui`. Verificado en el 7001 con datos
reales (9 recuerdos extraídos por la anterior).

## Qué entra

- **Memoria** (`/memory`, chunk propio de 27 KB; `adapters/memory.ts` sobre
  `/api/memory`, `/api/memory-engine` y `/api/prefs`, sin cambios de forma):
  - Recuerdos: añadir (texto + categoría; JSON como la anterior, el
    multipart da 422), editar en sitio (clic en el texto; Intro guarda, Esc
    cancela, cambio de categoría), fijar (va siempre al contexto), borrar,
    buscar, filtro por categoría con recuento, ordenar (recientes, antiguas,
    alfabético, más usadas; fijadas siempre arriba), seleccionar varias con
    «todas/ninguna» y borrado en lote con confirmación, exportar JSON.
  - «Ordenar con el modelo» (`/audit`): dice antes → después y cuántas
    fuera, o «Ya estaba limpia».
  - Sugerencias: «De una conversación» (lista de conversaciones →
    `/extract`) y «De un fichero» (`<input type=file>` nativo → `/import`);
    diálogo con casillas y «Guardar N».
  - Interruptores «Memoria activa» (`memory_enabled`) y «Extraer sola de las
    conversaciones» (`auto_memory`).
  - Reglas aprendidas: recuento y carril semántico, filtros
    todas/activas/antipatrones, fila con EVITAR, nivel, madurez, clase de
    confianza, barra de puntuación, % dañina, útil/dañina/borrar; añadir
    con nivel; «Ejecutar el curador» con informe; «Ver el paquete» (lo que
    ve el modelo, caracteres y presupuesto).
  - `session_id` de extracciones antiguas guarda el nombre de la
    conversación, no su id: se enseña como «de «…»» y solo se enlaza si
    parece un uuid.
- Servidor: `/api/memory/import` y `/api/memory/extract` exentos del
  `REQUEST_HARD_TIMEOUT` de 45 s (una pasada del modelo local tarda más;
  la anterior también recibía 504).
- `/brain`, atajo `open_memory` y «Herramientas» apuntan a la pantalla.
- `.fs-chip` y `.fs-field` suben a `styles/components.css` (los usan Notas
  y Memoria).

## Verificado en el navegador

- Añadir «A Luis le gusta…» como Preferencia → arriba, «tuya · ahora».
- Editar en sitio (añadir « y en castellano», categoría Preferencia) →
  guardado; fijar → «1 fijado»; filtro Objetivo · 1.
- Regla «Antes de editar un fichero, leerlo entero» (procedural,
  human_explicit, 0.85) → 👍 → 1.85; curador → informe; «Ver el paquete»
  → «## Learned rules … (candidate)», 83 de 1800 caracteres.
- «De una conversación» sobre «Prueba Studio v2» → el modelo no propuso
  nada (diálogo vacío honesto).

## Sin verificar

- Importar desde fichero por el navegador (el puente no puede adjuntar
  ficheros fuera de la sesión); probado el endpoint a mano tras la exención
  del timeout.
- «Ordenar con el modelo» de punta a punta (llama a `/audit`, que ya
  funcionaba en la anterior).
