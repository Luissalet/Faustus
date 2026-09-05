# Estado del lote AD — Compare como pantalla

Fecha: 05-09-2026. Rama `feat/studio-ui`. Cierra la fila «Compare» de PARIDAD
§1 y la parte de Compare de «… desde el compositor» (§3). Nada de
`static/js/compare/*` sobrevive; los prompts de prueba (datos) están en
`adapters/compare.ts`; la votación usa la misma clave de `localStorage`
que la anterior para no perder el historial.

## Brief (banco de trabajo)

**Trabajo.** Poner el mismo prompt a varios modelos (o varios buscadores),
ver las respuestas lado a lado sin saber cuál es cuál, decidir cuál es
mejor, y con el tiempo saber qué modelo gana más. A veces seguir la
conversación en todos a la vez, o llevarse una a Studio.

**Persona.** Luis, que prueba modelos locales en una sola GPU: necesita
saber cuánto tarda cada uno, poder parar, y que la carrera sea justa (sin
memoria ni documentos que ayuden a uno).

**Lo que hacía mal la anterior.** Diez ficheros que reconstruían la
pantalla del chat a base de show/hide sobre el DOM del compositor,
selector modal con cuatro pestañas y pickers propios, confeti sobre el
ganador, `confirm()` al reiniciar, un menú de exportación pintado a mano,
timeouts en un input de texto, indicador de estado colgado de la barra
del chat, y el «shuffle pool» en otro modal.

**Dirección.** Una pantalla propia (ancha). Arriba la preparación en tres
filas: modo (segmentado, con una frase que dice qué se compara), huecos
como chips (letra, modelo, quitar) con Añadir / Barajar / Sondear / Fondo,
y las opciones (A ciegas, Todos a la vez / Uno detrás de otro, tiempo
máximo, conservar los chats). Debajo el prompt con los prompts de prueba
y el botón que cambia de nombre (Comparar → Enviar a todos). Los paneles
en rejilla (1–4 columnas), cada uno con su letra, su orden de llegada y
su tiempo; a ciegas de verdad (los chips también se esconden mientras la
carrera está abierta). La votación aparece solo cuando todos han
terminado y revela los nombres. El marcador es un diálogo con filtro por
modo.

**Revisión contra los defaults.** Sin confeti (un aviso «Gana X» basta);
botones con nombre en la preparación y en la votación, iconos solo en los
paneles con `aria-label`; el texto del error del servidor tal cual; los
timeouts como opciones con sentido (y «ninguno»); el modo Agente respeta
la aprobación de herramientas dentro del panel con el mismo `AskCard` de
Studio; colores por token.

## Qué entra

- `adapters/compare.ts`: modos y su ayuda, `EVAL_PROMPTS` (22 prompts de la
  anterior), votos (`odysseus-compare-votes`, mismo formato), marcador por
  modo, fondo de exclusiones, opciones por modo (`fs-compare-options`),
  `searchWith` (`/api/search/query`), `synthesisPrompt`, `gradeAnswer`,
  `probeRoutes` (`/api/probe-selected`), `metricsLine`.
- `adapters/chat.ts`: `SendOptions.compare` → `compare_mode`,
  `no_documents`, `no_memory`.
- `screens/compare/Compare.tsx` + `compare.css`; `AskCard` exportado de
  `Transcript.tsx`; `ModelPalette` reutilizado como selector por hueco.
- Rutas: `/compare` en `app.py`, `SERVER_ROUTES`, `TOOLS` (Columns3), ancho
  en `AppShell`; `/compare` y `open_compare` van a la pantalla.
- i18n: 85 filas.

## Verificado en el navegador (7001)

- Modo Chat con qwen3.5:9b (A) y qwen3.8:27b-q8_0 (B), a ciegas: Intro
  envía; A responde en 11,7 s (#1, 27 tok), B en 50,8 s (#2, 24 tok); la
  barra «Which answer is better?» aparece al terminar; votar A → «qwen3.5:9b
  wins · Names revealed» y los nombres aparecen en los paneles.
- Marcador: 1 voto, tabla con victorias/derrotas/empates y %, filtro por modo,
  «Clear the votes».
- Modo Búsqueda: Firecrawl (self-hosted) y SearXNG; SearXNG lista los
  resultados con título, URL y extracto (#2 · 30,5 s); Firecrawl sin
  resultados (#1 · 10,9 s).
- Selector de modelo por hueco (paleta con los seis modelos del endpoint
  local), Reset, Export en la cabecera.

## Sin verificar

- PENDIENTES 111: Agente, Investigación, secuencial, respuesta esperada,
  imprimir, cambiar de modelo en un panel con conversación, «Seguir en
  Studio».
