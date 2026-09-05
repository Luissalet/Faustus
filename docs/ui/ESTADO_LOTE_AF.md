# Estado del lote AF — Tournament, Procedencia e Historial importado

Fecha: 05-09-2026. Rama `feat/studio-ui`. Cierra las filas «Tournament»,
«Procedencia (grafo)» e «Historial importado» de PARIDAD §1. Nada de
`tournament.js`, `provenance.js` ni `historyImport.js` sobrevive: la
lógica pura (normalizadores, prompt de síntesis, disposición de fuerzas,
etiquetas de fecha honestas) se portó a `adapters/*.ts` y `lib/graph.ts`;
las pantallas son de Studio.

## Brief (banco de trabajo)

**Trabajo.** Tres herramientas de auditoría y comparación que la anterior
tenía en modales: enfrentar modelos a la misma tarea y quedarse con lo
mejor (Tournament), ver de qué registros sale lo que el agente cree
(Procedencia) y traer conversaciones de otros sitios sin escribir nada
hasta haber visto qué pasaría (Historial importado).

**Lo que hacía mal la anterior.** Tres modales sin ruta, con casillas de
modelos por endpoint, un grafo de 1000×680 en un modal estrecho, un
formulario de importación con `alert` y tablas sin cabecera.

**Dirección.** Cada una donde pertenece: el torneo es una pestaña de
Agentes (quién hace el trabajo mecánico), la procedencia una pestaña de
Memoria (qué cree el agente y por qué) y el historial importado un tipo
de la Biblioteca (dónde está lo que hay). Las tres con su URL
(`?run=`, `?node=`, `?type=historial`), la anchura «wide» para el
tablero y el grafo, la paleta de modelos por ranura en vez de casillas,
el tablero en directo con reloj y estado por participante, el grafo con
leyenda-filtro, búsqueda que fija, panel lateral explicativo y vista de
huérfanos; la importación como diálogo con vista previa obligatoria y
cada omisión con su motivo.

**Revisión contra los defaults.** Nada que dependa del hover (los
tooltips repiten lo que ya se ve); «—» donde el juez no puntuó, nunca un
cero; «fecha desconocida» nunca se rellena con hoy; el aviso de grafo
recortado o truncado se dice en la página; teclado en los nodos
(`tabindex`, Enter); colores solo por token (siete tonos de tipo); el
SVG del grafo lleva `data-note="guard-ok"` porque es un dibujo, no un
icono.

## Qué entra

- `adapters/tournament.ts`: `tournamentConfig`, `listRuns`, `getRun`,
  `startTournament`, `cancelTournament`, `followRun` (SSE `?stream=1`
  sin nombre + `end`, más sondeo cada 4 s para reloj y progreso),
  `answersByEntry`, `winnerOf`, `stateOfEntry`, `mergePromptFor`,
  preparación en `localStorage` (`fs-tournament-setup`).
- `screens/agents/Tournament.tsx` (pestaña `tournament` en `Agents.tsx`);
  estilos `.fs-trn__*` en `agents.css`.
- `lib/graph.ts`: normalizadores, `viewModel` (filtro, búsqueda, tope,
  etiquetas), `layout` (Fruchterman–Reingold determinista con semilla
  FNV-1a), `nodeRadius`, `tooltip`, `encodeNodeId`.
- `adapters/provenance.ts`: `loadGraph`, `explainNode`, `nodeNeighbors`,
  `loadOrphans`, `stepWhere`, `terminus`, duplicados.
- `screens/memory/Provenance.tsx` + `provenance.css`; pestañas
  Recuerdos / Procedencia en `Memory.tsx`.
- `adapters/history.ts`: listar, leer, borrar, estadísticas, importar por
  ruta o subida (dry run), búsqueda en dos vías.
- `screens/library/History.tsx`; tipo «Imported» en `Library.tsx`;
  estilos `.fs-his__*` en `library.css`.
- `AppShell`: anchura «wide» también para `/agents?t=tournament` y
  `/memory?t=provenance`.
- i18n: 179 filas.

## Verificado en el navegador (7001)

- Torneo: «In two short paragraphs…» con qwen3.5:9b + qwen3.8:27b-q8_0,
  2 rondas, juez por defecto (27b) → tablero en directo (Answering, reloj,
  «Writing…» por tarjeta), respuesta a ciegas del 9b a los 137 s, tras
  8 m 56 s Finished: fichas Respuesta a ciegas / Híbrido 1, corona y #1 al
  9b, tabla (100/100/95 = 295 frente a 100/100/92 = 292, desempate 0.716 /
  0.321, «success» con nota del juez), «Ranked by the judge», «Ran all 2
  rounds», 17 eventos; **Merge into the composer** abre Studio con el
  prompt de síntesis en el compositor; la lista de recientes muestra
  Finished · 2 models · winner qwen3.5:9b.
- Procedencia: grafo de 4 nodos / 2 aristas del 7001 (memory, chat, file,
  checkpoint), leyenda con recuentos, clave `changed` / `evidence_of`,
  clic en el checkpoint → panel con metadatos (status, session_id,
  job_id, workspace, verdict), resumen, «Traced to file hola.txt», pasos
  1 y 2 declarados con destino pulsable; «What breaks if I touch this»
  sobre `file:hola.txt` → impacto (el checkpoint), saltos 1/2/3 y
  mini-grafo; vista Orphans & duplicates → 1 huérfano (memory) y 0 pares.
- Historial: ruta `D:\LocalAI\_claude_tmp\his_test` (dos exportaciones
  Faustus + un JSON roto) → vista previa «Faustus · 3 files · would import
  2 conversations and 4 messages — 2 new, 0 already here» y «1 skipped:
  broken.json#x export has no messages list»; «Import them» → 2 filas con
  origen, fecha (1 y 2 ago 2026), modelo y 2 msgs; desplegar → mensajes
  con metadatos; «Inside messages» + «torneo» → `hybrid · degraded`, 1
  hit con el acierto resaltado y la conversación desplegable; borrar una
  desde el menú → «1 conversation deleted», estadísticas actualizadas.
- Móvil (420 px): tablero del torneo y procedencia en una columna (panel
  lateral bajo el grafo).

## Sin verificar

- PENDIENTES 118.
