# Estado del lote AC — Deep Research como pantalla

Fecha: 05-09-2026. Rama `feat/studio-ui`. Cierra la fila «Deep Research» de
PARIDAD §1 y la parte de investigación de «… desde el compositor» (§3).
Nada del DOM de `research/panel.js`, `jobs.js` ni `researchSynapse.js`
sobrevive; la lógica (fases → texto, seguimiento SSE con sondeo de respaldo,
cola) está reescrita en `adapters/research.ts` y `screens/research/`.

## Brief (banco de trabajo)

**Trabajo.** Encargar preguntas que merecen varias búsquedas, dejarlas
correr (una o varias, ahora o en cola), ver por dónde van y, al terminar,
leer el informe, discutirlo en un chat, exportarlo o tirarlo.

**Persona.** Luis, con un modelo local de 27B que tarda: necesita ver que
algo pasa (fase, ronda, reloj) y poder irse a otra cosa (notificación al
terminar, adopción de lo que sigue corriendo al volver).

**Lo que hacía mal la anterior.** Un panel flotante sobre el chat con
una animación de «sinapsis» aleatoria como progreso, seis selects
siempre visibles, botones con solo icono, una pregunta de «¿paralelo o
secuencial?» pintada a mano, `confirm()` para borrar, y `/research-fit`
como comando de texto con HTML incrustado.

**Dirección.** Una pantalla de una columna: la pregunta arriba (grande,
con los ajustes resumidos en una línea y desplegables), y debajo tres
grupos que solo aparecen cuando tienen algo: en cola, en marcha,
terminadas. El progreso es honesto: la fase en palabras, la ronda, un
reloj y una órbita de puntos (una por ronda) con la barra de las cinco
fases. Las terminadas enseñan el informe plegado, las fuentes plegadas y
las acciones con nombre. «Ajuste a esta máquina» es una tarjeta al pie
con Comprobar → perfil, hardware, cambios y bloqueos → Aplicar.

**Revisión contra los defaults.** Borrar pide confirmación con la
consecuencia; nada solo por icono salvo cerrar/quitar; estados vacíos
que dicen qué hacer; motivo real cuando falla (el `error` del stream);
colores por token (`--fs-brand` en marcha, `--fs-danger` fallida,
`--fs-success` hecha); animación de la órbita con `prefers-reduced-motion`.

## Qué entra

- `adapters/research.ts`: `startResearch`, `cancelResearch`,
  `activeResearch`, `researchStatus`, `followResearch` (SSE `/stream` con
  `error` final; sondeo `/status` si el stream falla), `researchResult`
  (`/result` y `/result-peek`), `searchProviders`, `researchFit` /
  `applyResearchFit` (`/preset`, `/preset/apply`), `CATEGORIES`,
  `phaseLabel`.
- `screens/research/Research.tsx` + `research.css`: pantalla, cola en
  `localStorage` (`fs-research-queue`, ajustes en `fs-research-settings`,
  recientes descartados en `fs-research-dismissed`), `?q=` prellena.
- Compositor: chip «Investigación» (`knobs.research` → `use_research`,
  se apaga tras el turno, anula plan), eventos `research_progress` →
  línea `ResearchLine` (fase, ronda, reloj y media) y `research_sources`
  → fuentes del turno (y desde `metadata.research_sources` al recargar).
- Rutas: `/research` en `app.py`, `SERVER_ROUTES`, `TOOLS` (Telescope),
  `AppShell`; `/research [pregunta]` y `/mcp` → Ajustes → Integraciones.
- i18n: 74 filas.

## Verificado en el navegador (7001)

- Pantalla vacía con la pregunta, ajustes plegados («Rounds auto · Auto ·
  default search · default model») y desplegados (cinco selects con sus
  ayudas), tarjeta «Ajuste» con Comprobar → perfil «roomy», RTX 4070 Ti
  27.9 GB, dos cambios propuestos y Aplicar.
- Empezar con rondas 1 → tarjeta en marcha «Probing the model… · 3s» y
  luego «Planning the research… · 30s» con la órbita; el primer intento
  falló porque el modelo no estaba cargado (sonda 504) y la tarjeta pasó
  a fallida con Reintentar / Editar / Descartar (ahora con el motivo del
  servidor).
- `?q=` prellena la pregunta desde `/research pregunta`.

## Sin verificar

- El informe terminado en la pantalla (depende de que el buscador y el
  27b acaben; ver el resultado en el comentario de cierre del lote).
- Chip «Investigación» en el compositor con un turno completo (mismo
  motivo: minutos por turno con el 27b).
