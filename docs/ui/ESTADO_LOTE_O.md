# Estado del lote O — Agentes (`/agents`)

Fecha: 05-09-2026. Rama `feat/studio-ui`.

## Qué hay

Una pantalla con cuatro pestañas (`?t=workers|runners|defs|experts`), en la
barra bajo «Herramientas», en la paleta y con `/workers` y `/experts` en el
compositor. Cuatro modales de la anterior que eran un solo asunto: quién hace
el trabajo mecánico, bajo qué reglas y con qué conocimiento.

- **Workers** (`static/js/workers.js` → `screens/agents/Workers.tsx`):
  formulario completo, más dos campos que la anterior no tenía en pantalla,
  *agente* (slug de una definición) y *runner*; lista de trabajos con el
  desplegable completo (progreso vivo, cambios en disco, verificación,
  prueba, workers, tareas); SSE por trabajo vivo y sondeo de respaldo;
  Tablero abre el chat del trabajo en Studio; Cancelar.
- **Runners** (`agentRunners.js` → `Runners.tsx`): tabla con las tres
  reglas de honestidad de la anterior (licencia literal, instalado ≠ puede
  ser worker, la guardia arriba), Lanzar con salida en directo, copiar,
  «Usar en un trabajo».
- **Definiciones** (`agentDefs.js` → `Defs.tsx`): reglas resueltas, ficheros
  que no cargaron, techo de delegación, «Usar en un trabajo».
- **Expertos** (`experts.js` → `Experts.tsx`): galería, ficha con corpus,
  búsqueda y bloque, panel de revisión con aceptar/rechazar y desenlace.

Adaptador: `adapters/workers.ts` (el `adapters/agents.ts` que ya existía es
el del tablero de sub-agentes y se queda como está).

## Verificado en el 7001

- Workers: trabajo real («crea hola.txt») lanzado desde la pantalla;
  progreso vivo (ronda, herramienta, segundos), hecho con 1 fichero
  cambiado, verificación «sin verificar» con su motivo, prueba parcial 0,65
  con la duda nombrada, worker con rondas/tokens/reclamación/resumen;
  Tablero → `/studio?s=` con el SubagentBoard.
- Runners: 18 conocidos, 2 instalados, 1 usable; búsqueda; «Usar en un
  trabajo» rellena el runner. Lanzar no se ha pulsado (instala software).
- Definiciones: 3 integradas con sus reglas; «Usar en un trabajo» rellena
  `agent=planner`.
- Expertos: crear, guardar rúbrica, subir corpus (por API: el selector de
  ficheros no se puede pulsar desde el navegador automatizado), reindexar,
  buscar («adverbio» → 1 acierto), bloque del modelo, panel de revisión con
  un resultado pegado: tramos marcados, aceptar/rechazar, resultado aplicado,
  desenlace enviado (contadores del experto actualizados).

## Arreglos que salieron

- La hoja antigua pintaba de blanco las pestañas al pasar y el bloque de
  contexto del proyecto en modo oscuro (PENDIENTES 63).
- `Project.tsx`: un `?tab=` desconocido cae al Brief.
- `Button` acepta `title`.
- `.fs-switch` vive en `components.css` (antes duplicado en Memoria y
  Calendario).
- `app.py` sirve `/agents`; `app.js` abre el modal de Workers en
  `/agents?shell=legacy`.

## Pendiente

PENDIENTES 63–67.
