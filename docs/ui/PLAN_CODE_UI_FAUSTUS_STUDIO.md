# Plan técnico para programar la nueva UI de Faustus Studio

> **Lee antes `DECISIONES_UI.md`.** Manda sobre este documento donde haya
> conflicto: fija el primer incremento (UI-000→UI-022 + UI-030), prohíbe el
> comodín de rutas en `app.py`, define el feature flag, añade UI-013 y declara
> la deuda de identificadores de artefacto.

Fecha: 04-09-2026  
Destino: agentes de código que trabajen sobre `D:\LocalAI\odysseus`.  
Documento de producto relacionado: `UI_OVERHAUL_FAUSTUS_STUDIO.md`.

Fuentes estudiadas:

- [Taste Skill](https://github.com/leonxlnx/taste-skill), commit [`ccbc156`](https://github.com/leonxlnx/taste-skill/tree/ccbc15639c97057cbfcf32ecebc38ef716e4bb37), licencia MIT.
- [Vercel Agent Skills — Web Design Guidelines](https://github.com/vercel-labs/agent-skills/tree/main/skills/web-design-guidelines), commit [`063bee9`](https://github.com/vercel-labs/agent-skills/tree/063bee94c3f4df8453406c830b0a7df0f2860278), y su [lista de reglas viva](https://github.com/vercel-labs/web-interface-guidelines/blob/main/command.md).
- [VoltAgent Awesome DESIGN.md](https://github.com/VoltAgent/awesome-design-md), commit [`8147538`](https://github.com/VoltAgent/awesome-design-md/tree/8147538b4226ae41e2487a9179e3bcc1f68e8554), licencia MIT.

## Decisión ejecutiva

La dirección de producto del plan anterior es correcta: **Faustus debe dejar de parecer un chat rodeado de herramientas y convertirse en un estudio personal organizado por proyecto, intención, ejecución y artefacto.**

Lo que cambia tras estudiar las skills es el método de diseño:

| Recurso | Papel correcto | Papel incorrecto |
|---|---|---|
| Awesome DESIGN.md | Biblioteca de anatomías visuales para redactar un `DESIGN.md` propio de Faustus | Copiar la identidad de Linear, Runway, Vercel o Raycast |
| Taste Skill | Director de arte y crítico anti-genérico en superficies expresivas | Norma global para dashboards, tablas o flujos complejos |
| Vercel Web Design Guidelines | Auditor final de accesibilidad, interacción, contenido y rendimiento | Generador de la dirección visual |

Taste Skill v2 indica explícitamente que su skill principal está pensada para landing pages, portfolios y rediseños, no para dashboards, tablas ni producto multipaso. Por tanto:

- `design-taste-frontend` se puede usar en Inicio, onboarding, estados vacíos y páginas promocionales;
- `redesign-existing-projects` es la variante adecuada para criticar el producto existente;
- sus principios de jerarquía, diales y anti-slop se adoptan selectivamente;
- sus elecciones por defecto de React, Tailwind, librerías de iconos o animación **no sustituyen el stack real de Faustus**, que hoy es HTML, CSS y módulos ES sin build.

## Estado real del frontend que condiciona el plan

La UI actual no es pequeña ni desechable:

| Pieza | Estado observado | Consecuencia |
|---|---|---|
| `static/index.html` | 2.868 líneas, unos 260 KB; contiene shell, chat y muchos modales | No reescribir de una vez |
| `static/style.css` | 44.682 líneas, unos 1,52 MB | Congelar crecimiento y crear una capa CSS nueva |
| `static/app.js` | 4.601 líneas, unos 200 KB; orquesta imports, rutas y eventos globales | Extraer router y shell, no sumar más responsabilidades |
| `static/js/` | Más de cien módulos ES, sin bundler | Mantener imports nativos durante la migración |
| Navegación | Sidebar completa, rail alternativo y rutas que abren modales | Sustituir por rutas de producto mediante adaptadores |
| E2E | Ya existe infraestructura Playwright opt-in en `tests/e2e/` | Ampliarla, no crear otro harness |

Una exploración estática con la guía de Vercel encontró deuda acumulada que el overhaul debe contener:

- unas 92 apariciones de `transition: all`;
- más de 110 eliminaciones de `outline` que necesitan comprobar reemplazo de foco;
- más de 300 lecturas `getBoundingClientRect`, legítimas en parte por ventanas/editor, pero peligrosas si se intercalan con escrituras;
- controles interactivos construidos como `div` en menús y cabeceras;
- iconos que dependen de `title` sin `aria-label`;
- estilos inline y SVG repetidos dentro de `index.html` y strings de JavaScript.

Esto no exige arreglar 44.000 líneas antes de avanzar. Exige que **todo componente nuevo nazca limpio** y que cada pantalla migrada elimine la deuda de su área.

## Identidad visual propia: “taller editorial técnico”

Awesome DESIGN.md se utilizará como anatomía, no como disfraz:

- de [Linear](https://github.com/VoltAgent/awesome-design-md/tree/main/design-md/linear.app): jerarquía por escalones de superficie, hairlines, densidad de producto y acento escaso;
- de [Runway](https://github.com/VoltAgent/awesome-design-md/tree/main/design-md/runwayml): tratamiento grande de vídeo e imagen y alternancia entre zonas de trabajo y lectura;
- de [Raycast](https://github.com/VoltAgent/awesome-design-md/tree/main/design-md/raycast): command palette, navegación por teclado y sensación de herramienta rápida;
- del Faustus actual: coral de marca, marca gráfica, Inter/Fira Code/OpenDyslexic ya autoalojadas y temas configurables.

El resultado no debe identificarse con ninguna de esas marcas. La lectura de diseño para los agentes será:

> Producto multipropósito local-first para crear y terminar trabajo técnico y creativo. Debe sentirse preciso y rápido en código/datos, amplio y visual en medios, y comprensible para una persona que no conoce sus subsistemas.

Diales base inspirados en Taste Skill:

```text
DESIGN_VARIANCE = 5
MOTION_INTENSITY = 3
VISUAL_DENSITY = 6
```

Variantes por contexto:

| Contexto | Variance | Motion | Density |
|---|---:|---:|---:|
| Inicio/onboarding | 6 | 4 | 3 |
| Studio creativo | 6 | 4 | 5 |
| Studio de código | 3 | 2 | 8 |
| Biblioteca visual | 5 | 3 | 6 |
| Automatizaciones/Actividad | 3 | 2 | 8 |
| Móvil | -1 | -1 | -1 |

## Tokens que debe definir `DESIGN.md`

Los valores son el punto de partida del prototipo. Deben validarse en capturas y contraste antes de congelarlos.

### Color oscuro

```css
--fs-canvas:       #0c0d0f;
--fs-surface-1:    #121418;
--fs-surface-2:    #191c21;
--fs-surface-3:    #22262d;
--fs-border:       #2b3038;
--fs-border-strong:#3b424d;
--fs-text-1:       #f2f1ed;
--fs-text-2:       #bdc1c9;
--fs-text-3:       #858b96;
--fs-brand:        #e06c75;
--fs-brand-hover:  #ec8189;
--fs-focus:        #8ab4ff;
--fs-success:      #54aa78;
--fs-warning:      #d9a441;
--fs-danger:       #df626d;
--fs-info:         #7198e5;
```

### Color claro

```css
--fs-canvas:       #f3f1ec;
--fs-surface-1:    #faf9f6;
--fs-surface-2:    #ffffff;
--fs-surface-3:    #ebe8e1;
--fs-border:       #d8d4cb;
--fs-border-strong:#bbb5aa;
--fs-text-1:       #242321;
--fs-text-2:       #55534e;
--fs-text-3:       #77736b;
--fs-brand:        #c9515d;
--fs-brand-hover:  #ad3f4a;
--fs-focus:        #315fa8;
```

Reglas:

- El coral identifica marca y acción principal, nunca estado de error.
- Azul solo comunica foco, enlace o ejecución activa.
- Verde, ámbar y rojo se reservan para semántica.
- No introducir gradientes morado-azul como decoración genérica.
- El tema personalizable actual sigue funcionando mediante aliases: `--bg → --fs-canvas`, `--panel → --fs-surface-1`, `--fg → --fs-text-1`, `--red → --fs-brand`.

### Tipografía

No añadir una fuente remota durante el overhaul:

| Token | Valor | Uso |
|---|---|---|
| `--fs-font-ui` | Inter autoalojada, system-ui fallback | Navegación, controles y lectura |
| `--fs-font-code` | Fira Code autoalojada, ui-monospace fallback | Código, rutas, IDs, recetas |
| `display` | 28–36 px / 650 / -0,025 em | Inicio y cabeceras de proyecto |
| `title` | 20–24 px / 620 | Pantalla o artefacto |
| `body` | 14–16 px / 400 / 1,5 | Contenido |
| `label` | 12–13 px / 550 | Metadatos y controles |

OpenDyslexic permanece como preferencia de accesibilidad. Los números en costes, progreso, tiempos y comparaciones usan `font-variant-numeric: tabular-nums`.

### Espacio, forma y elevación

```text
space: 4 · 8 · 12 · 16 · 24 · 32 · 48
radius: 6 control · 10 panel · 14 preview · 999 pill semántico
border: 1 px; 2 px solo foco/selección
shadow: únicamente overlays, menús y drag; nunca para separar cada tarjeta
```

Las agrupaciones normales usan separación, títulos y hairlines. No envolver cada bloque en una tarjeta.

### Movimiento

```text
rápido 120 ms · normal 180 ms · contextual 240 ms
easing estándar: cubic-bezier(.2,.8,.2,1)
propiedades permitidas: transform, opacity y color cuando proceda
```

- Nunca `transition: all`.
- Toda animación se puede interrumpir.
- `prefers-reduced-motion: reduce` desactiva desplazamientos, loops y parallax.
- El progreso puede animarse; una pantalla en reposo no debe moverse por decorar.

## Arquitectura objetivo sin cambiar de framework

### DOM estable

`static/index.html` debe recibir un único shell estructural cerca del comienzo del `body`:

```html
<div id="faustus-app" class="fs-app">
  <a class="fs-skip-link" href="#fs-main">Saltar al contenido</a>
  <nav id="fs-nav" aria-label="Navegación principal"></nav>
  <main id="fs-main" tabindex="-1"></main>
  <aside id="fs-inspector" aria-label="Inspector" hidden></aside>
  <div id="fs-overlay-root"></div>
</div>
<div id="legacy-ui-root"></div>
```

El DOM legacy se conserva durante la transición. Al migrar una pantalla se mueve su entrada al shell; el modal antiguo queda como adaptador temporal y después se retira.

### Estado mínimo compartido

Crear `static/js/shell/store.js` con un estado pequeño, serializable y sin dependencia:

```js
{
  route: { name, params, query },
  context: {
    projectId,
    intention,
    skillId,
    selectedArtifactIds,
    referenceRoles,
    memoryScope,
    backendId
  },
  layout: { navMode, inspectorOpen, inspectorTab, density },
  run: { activeRunId, approvalId },
  selection: { type, ids }
}
```

Reglas:

- El store guarda contexto de interfaz; proyectos, runs y artefactos siguen siendo autoritativos en backend.
- Cada cambio emite un evento único y se suscribe mediante `subscribe(selector, callback)`.
- Nada nuevo escribe directamente en cinco módulos o en variables de `window` para cambiar de pantalla.
- Persistir solo preferencias de layout. No persistir cachés ni permisos como verdad.

### Router

Crear `static/js/shell/router.js` basado en History API:

| Ruta | Vista | Estado en URL |
|---|---|---|
| `/` | Inicio | ninguno |
| `/studio` | Studio | `project`, `intent`, `run` |
| `/projects` | Lista de proyectos | `status`, `sort` |
| `/projects/{id}` | Proyecto | `tab`, `artifact`, `run` |
| `/library` | Biblioteca | `project`, `type`, `skill`, `q`, `sort` |
| `/automations` | Automatizaciones | `status`, `project` |
| `/activity` | Runs, avisos y aprobaciones | `status`, `project`, `run` |

- Navegación real con `<a>` para permitir abrir en pestaña y copiar URL.
- Filtros y tabs relevantes viven en query string.
- El hash `#session-id` se conserva durante compatibilidad y se traduce internamente a Studio/chat.
- `app.py` debe servir la SPA para cada ruta, incluyendo `/projects/{project_id}`.
- `startupShell.js` deja de ser un mapa de “ruta → clic en modal” y pasa a inicializar el router.

### Árbol de archivos nuevo

```text
static/
├─ css/
│  └─ studio/
│     ├─ tokens.css
│     ├─ base.css
│     ├─ shell.css
│     ├─ components.css
│     ├─ responsive.css
│     ├─ legacy-bridge.css
│     └─ screens/
│        ├─ home.css
│        ├─ studio.css
│        ├─ projects.css
│        ├─ library.css
│        ├─ automations.css
│        └─ activity.css
└─ js/
   └─ shell/
      ├─ index.js
      ├─ router.js
      ├─ store.js
      ├─ render.js
      ├─ navigation.js
      ├─ contextBar.js
      ├─ inspector.js
      ├─ commandPalette.js
      ├─ focus.js
      ├─ screens/
      │  ├─ home.js
      │  ├─ studio.js
      │  ├─ projects.js
      │  ├─ project.js
      │  ├─ library.js
      │  ├─ automations.js
      │  └─ activity.js
      ├─ components/
      │  ├─ button.js
      │  ├─ emptyState.js
      │  ├─ statusBadge.js
      │  ├─ artifactCard.js
      │  ├─ runTimeline.js
      │  ├─ approvalCard.js
      │  └─ virtualList.js
      └─ adapters/
         ├─ sessions.js
         ├─ projects.js
         ├─ skills.js
         ├─ artifacts.js
         ├─ runs.js
         └─ automations.js
```

No crear una segunda implementación de chat, proyectos o galería. Los adapters invocan APIs y funciones existentes mientras se extraen controladores puros de `sessions.js`, `projects.js`, `gallery.js`, `documentLibrary.js`, `agentHarnessUI.js` y `tasks.js`.

## Contrato de componentes

Todo componente nuevo debe aceptar datos, devolver DOM y exponer limpieza de listeners. Nada debe depender de IDs globales salvo el shell.

### Primitivos P0

| Componente | Contrato mínimo | Accesibilidad |
|---|---|---|
| Button/IconButton | variante, label, icon, loading, disabled, action | `<button>`, nombre accesible, foco visible |
| NavItem | href, icon, label, badge, active | `<a>`, `aria-current="page"` |
| ContextChip | tipo, label, value, remove/edit | botón explícito, no depender solo del color |
| StatusBadge | estado normalizado | texto + icono; color secundario |
| EmptyState | título, explicación, acción primaria/secundaria | heading correcto, acción específica |
| Menu/Popover | anchor, items, initialFocus | Escape, flechas, retorno de foco |
| Dialog/Sheet | título, contenido, close | `aria-modal`, focus trap, inert del fondo |
| Skeleton | forma final y label | `aria-busy`; sin loop en reduced motion |

### Objetos de producto P1

| Componente | Debe mostrar |
|---|---|
| ArtifactCard | preview, tipo, proyecto, estado, fecha, skill y acción contextual |
| ArtifactViewer | contenido, variantes, historial, procedencia y acciones `Usar en`, `Variar`, `Corregir`, `Receta` |
| RunTimeline | pasos estables, progreso, espera, evidencia, coste y cancelación |
| ApprovalCard | acción exacta, destino, riesgo, datos enviados y botones Aprobar/Rechazar |
| SkillCard | resultado, requisitos, tiempo/coste, backend, permisos y ejemplo |
| ContextBar | proyecto, memoria, referencias, skill, backend y presupuesto activos |
| Inspector | pestañas Contexto/Receta/Ejecución/Detalles según selección |

## Especificación de cada pantalla

### Inicio

Objetivo: decidir qué continuar o empezar en menos de un minuto.

Orden visual:

1. Saludo breve y compositor “¿Qué quieres terminar?”
2. Runs/aprobaciones que requieren intervención.
3. Tres trabajos recientes, no una cuadrícula infinita.
4. Acciones `Crear`, `Escribir`, `Programar`, `Investigar`, `Automatizar`.
5. Estado técnico solo si bloquea una intención.

No mostrar modelos, temperatura, GPU o catálogo completo como contenido principal.

### Studio

Es la pantalla de trabajo común:

- cabecera con proyecto y estado guardado;
- ContextBar editable encima del compositor;
- área central cambia por intención, pero conserva el mismo shell;
- timeline de run debajo del objetivo, plegable por pasos;
- inspector derecho para controles avanzados;
- los resultados se insertan como artefactos, no solo como mensajes.

Modos:

| Intención | Centro | Inspector |
|---|---|---|
| Crear imagen/vídeo | referencias, storyboard/canvas, variantes | estilo, roles, seed/modelo, receta |
| Escribir | esquema, documento vivo, fuentes | tono, audiencia, formato, procedencia |
| Programar | conversación/plan, archivos, diff, pruebas | workspace, modo, permisos, comandos |
| Investigar | pregunta, hallazgos, fuentes, informe | alcance, fuentes, citas, coste |
| Automatizar | receta legible y próximo run | trigger, pasos, permisos, entrega |

### Proyectos

- La lista actual de `projects.js` deja de vivir en `#projects-modal`.
- Cada proyecto es una ruta y no un diálogo.
- La cabecera conserva `Brief · Activos · Personajes · Documentos · Código · Automatizaciones · Actividad` y oculta tabs no aplicables.
- El botón primario es “Nuevo trabajo”, no “Abrir chat”.
- Contexto, instrucciones y memoria se editan en inspector o panel dedicado, con estado de guardado visible.

### Biblioteca

Unifica galería, documentos y outputs de runs mediante adapters, sin exigir unificar las tablas backend en la primera fase.

- lista/cuadrícula según tipo;
- filtros reflejados en URL;
- selección múltiple con acciones claras;
- virtualización o `content-visibility` por encima de 50 entradas;
- dimensiones explícitas en imágenes para evitar saltos;
- lineage y procedencia disponibles desde cada artefacto.

### Automatizaciones

La vista principal es una lista de recetas comprensibles. El editor de nodos, si existe, es avanzado.

Cada fila muestra trigger, próxima ejecución, proyecto, último resultado, estado y acciones Pausar/Ejecutar/Revisar. Una automatización que publica o envía expone su aprobación antes de activar.

### Actividad

Agrega runs, tareas, investigación, renders, avisos y aprobaciones con un esquema visual común:

```text
tipo · objetivo · proyecto · estado · progreso · duración/coste · requiere acción
```

No mezclar logs en bruto con la lista. Los logs, herramientas y evidencia viven en el detalle de run.

## Responsive

| Ancho | Layout |
|---|---|
| `≥1280` | nav 224 px + main flexible + inspector 320 px |
| `768–1279` | rail 64 px + main; inspector como panel superpuesto |
| `<768` | topbar + main + navegación inferior reducida; inspector como bottom sheet |

Reglas móviles:

- área táctil mínima 44×44 px;
- safe areas con `env(safe-area-inset-*)`;
- ninguna acción depende solo de hover, drag o swipe;
- composer visible con teclado y `100dvh`;
- paneles y diálogos usan `overscroll-behavior: contain`;
- las cinco acciones más frecuentes caben sin menú; el resto va a “Más”.

## Plan por fases y tickets

### Fase 0 — Contratos y evidencia

#### UI-000 — Congelar journeys y capturas

Archivos: `tests/e2e/`, nuevo `tests/e2e/test_studio_baseline.py`, nuevo `docs/ui/journeys.md`.

- Capturar escritorio 1400×900, tablet 1024×768 y móvil 390×844.
- Journeys: crear proyecto, iniciar trabajo creativo, iniciar tarea de código, encontrar un resultado, resolver una aprobación.
- Guardar conteo de clics, tiempo y puntos de confusión.

Aceptación: cada journey tiene estado inicial, pasos, resultado y captura; el harness se ejecuta con el servidor falso existente.

#### UI-001 — Crear autoridad visual

Archivos: nuevo `DESIGN.md`, nuevo `docs/ui/component-contracts.md`.

- Convertir los tokens y reglas de este documento en la fuente de verdad.
- Incluir ejemplos de dark/light, densidad creativa/código y estados completos.
- Registrar licencias de fuentes e iconos.

Aceptación: un agente nuevo puede diseñar un componente sin inventar color, radio, spacing o motion.

### Fase 1 — Cimientos sin cambiar UX

#### UI-010 — Capa de tokens y compatibilidad

Archivos: nuevos `static/css/studio/tokens.css`, `base.css`, `legacy-bridge.css`; `static/index.html`; `static/js/theme.js`.

- Cargar CSS Studio después de `style.css`.
- Mapear tokens legacy y conservar temas personalizados.
- Añadir foco global visible, `color-scheme`, skip link y reduced motion.

Aceptación: UI antigua se ve igual salvo mejoras deliberadas; dark/light/custom siguen funcionando.

#### UI-011 — Primitivos accesibles

Archivos: `static/js/shell/components/*`, `static/css/studio/components.css`, nuevos tests Node/Python.

- Button, IconButton, StatusBadge, EmptyState, Menu, Dialog y Skeleton.
- Prohibir HTML interactivo crudo en módulos nuevos mediante test estático.

Aceptación: teclado, Escape, foco devuelto, labels y estados loading/disabled verificados.

#### UI-012 — Auditoría incremental Vercel

Archivos: nuevo `tools/audit_ui_guidelines.py` o test equivalente, `docs/ui/audit-baseline.md`.

- Guardar fecha y hash de la guía usada.
- Separar `legacy-known` de nuevas regresiones.
- Fallar CI solo por deuda nueva al principio; reducir baseline al migrar cada pantalla.

Aceptación: ningún `transition: all`, `outline: none` sin reemplazo, icon button sin label o control sin label entra en código Studio nuevo.

### Fase 2 — Shell y navegación

#### UI-020 — Store y router

Archivos: `static/js/shell/store.js`, `router.js`, `app.py`, tests de rutas.

- Implementar rutas canónicas y serialización de filtros.
- Añadir fallback SPA backend.
- Compatibilidad con hashes de sesión.

Aceptación: recargar, atrás/adelante, abrir en nueva pestaña y deep links conservan vista y filtros.

#### UI-021 — AppShell bajo feature flag

Archivos: `static/index.html`, `static/js/shell/index.js`, `navigation.js`, `static/css/studio/shell.css`, `static/app.js`.

- Flag local/admin `faustus_studio_shell`.
- Renderizar seis destinos.
- Mantener acceso “Interfaz anterior” durante piloto.

Aceptación: activar/desactivar no altera datos ni backend; rollback es recargar con flag apagado.

#### UI-022 — Command palette

Archivos: `commandPalette.js`, adapter de `search-chat.js`, `keyboard-shortcuts.js`.

- `Ctrl/Cmd+K` abre acciones, destinos, proyectos y búsqueda.
- “Buscar conversaciones” pasa a ser un comando, evitando dos atajos rivales.
- Ranking por contexto y uso reciente.

Aceptación: navegación esencial puede completarse solo con teclado.

### Fase 3 — Inicio y Studio

#### UI-030 — Inicio útil

Archivos: `screens/home.js/css`, adapters de projects/runs/skills.

- Continuaciones, aprobaciones, quick starts y salud bloqueante.
- Estados loading, vacío, error, offline y éxito.

Aceptación: usuario nuevo inicia proyecto/trabajo en menos de 60 segundos en test moderado.

#### UI-031 — ContextBar

Archivos: `contextBar.js`, adapters `projects.js`, `skills.js`; integración con `memory.js`, `workspace.js`, `modelControls.js`.

- Proyecto, memoria, referencias, skill, backend y presupuesto legibles.
- Simple/Dirección/Experto mediante inspector, no otra página.

Aceptación: antes de ejecutar siempre se sabe qué contexto, backend y permisos se usarán.

#### UI-032 — Studio de código como primera migración funcional

Archivos: `screens/studio.js`, adapter sessions/runs, `chat.js`, `agentHarnessUI.js`, `chatRenderer.js`.

- Reusar el compositor y stream actuales.
- Añadir modos Explorar/Planificar/Implementar/Revisar.
- Mostrar workspace, diff, tests y checkpoint como paneles de trabajo.
- Tool output detallado vive en RunTimeline, no interrumpe cada párrafo del chat.

Aceptación: el E2E actual de agentes sigue pasando y el journey termina con diff + evidencia.

#### UI-033 — Studio creativo

Archivos: `screens/studio.js`, adapters artifacts/skills, `gallery.js`, `galleryEditor.js`, `editor/*`.

- Referencias con roles, variantes, máscara y receta.
- Preview domina el centro; controles técnicos van al inspector.

Aceptación: una variante vuelve al flujo mediante `Variar`, `Corregir` o `Usar en` sin buscarla en otro modal.

### Fase 4 — Proyectos y artefactos

#### UI-040 — Extraer Projects del modal

Archivos: `static/js/projects.js`, nuevos screen/adapter de proyectos, `static/index.html`.

- Separar fetch/estado/render de apertura/cierre del modal.
- Conservar contratos backend y IDs necesarios mientras migran tests.

Aceptación: `/projects` y `/projects/{id}` funcionan como páginas; chat y memoria del proyecto conservan comportamiento.

#### UI-041 — Biblioteca federada

Archivos: `library.js/css`, adapters artifacts, `gallery.js`, `documentLibrary.js`, `provenance.js`.

- Modelo de vista común sobre fuentes actuales.
- Filtros URL, lazy images, dimensiones y virtualización.

Aceptación: encontrar y reutilizar imagen/documento/output sin conocer qué subsistema lo creó.

#### UI-042 — ArtifactViewer y lineage

Archivos: components `artifactCard.js`, `artifactViewer.js`, adapter provenance.

Aceptación: todo output muestra origen, proyecto, skill, modelo/backend, fecha y acciones siguientes.

### Fase 5 — Actividad y automatización

#### UI-050 — Esquema común de run

Archivos: adapter `runs.js`, `agentHarnessUI.js`, `tasks.js`, `research/jobs.js`, gallery progress.

- Normalizar solo en frontend al principio.
- Estados: queued, running, waiting-approval, paused, succeeded, failed, cancelled.

Aceptación: cualquier trabajo activo aparece en Actividad con el mismo lenguaje visual.

#### UI-051 — Timeline y aprobaciones

Archivos: `runTimeline.js`, `approvalCard.js`, `activity.js/css`.

Aceptación: el usuario entiende qué ocurre y qué desbloquea un run sin leer logs.

#### UI-052 — Automatizaciones

Archivos: `automations.js/css`, adapter de `tasks.js`, calendario y workflows futuros.

Aceptación: pausar, ejecutar, revisar y aprobar desde una vista legible; editor técnico queda secundario.

### Fase 6 — Retirada del sistema antiguo

#### UI-060 — Migrar settings y herramientas restantes

- Settings permanece como destino secundario con búsqueda.
- Email, calendario, notas, compare, cookbook y workers se integran como páginas o herramientas contextuales según uso.

#### UI-061 — Eliminar puentes muertos

- Retirar listeners, IDs y CSS solo después de confirmar cero imports y E2E equivalente.
- Dividir gradualmente `style.css`; no hacer una reescritura mecánica masiva mezclada con features.

Aceptación: el flag legacy puede eliminarse y el bundle antiguo deja de inicializar paneles migrados.

## Orden de ejecución para varios agentes

Los agentes no deben editar simultáneamente `index.html`, `style.css` o `app.js` sin coordinación. Lotes seguros:

| Lote | Trabajo paralelo permitido | Integración |
|---|---|---|
| A | `DESIGN.md`; tests baseline; tokens CSS | Agente shell integra los tres |
| B | store/router; primitivos; auditor Vercel | Integrar antes de pantallas |
| C | Inicio; ContextBar; RunTimeline | Cada uno en módulo/CSS propio |
| D | Studio código; Studio creativo | Comparten screen contract, no archivo |
| E | Proyectos; Biblioteca; Actividad | Adapters independientes |

Cada ticket debe declarar dueño de archivos compartidos y usar un changeset pequeño. No mezclar migración visual, cambio de API y refactor masivo en el mismo ticket.

## Secuencia obligatoria de skills para cada pantalla

### 1. Contexto y referencia

1. Leer `DESIGN.md`, este plan y el journey de la pantalla.
2. Elegir como máximo dos documentos de Awesome DESIGN.md por patrón concreto.
3. Registrar qué se toma —por ejemplo surface ladder o media framing— y qué no se copia.

### 2. Crítica Taste

Para una pantalla existente usar `redesign-existing-projects`. Para Inicio/onboarding puede usarse `design-taste-frontend`.

El agente produce antes del código:

```text
Design Read
Diales
Trabajo principal
Jerarquía
Elemento firma
Riesgos de accesibilidad/rendimiento
```

El elemento firma de Faustus debe nacer de su función: contexto visible, timeline de ejecución o lineage de artefactos. No añadir blobs, glows o movimiento arbitrario para “hacerlo premium”.

### 3. Implementación

- Leer módulos afectados y contratos de tests.
- Reusar adapter y primitivos.
- Implementar todos los estados.
- No añadir una dependencia sin comprobar stack, licencia, peso y necesidad.

### 4. Auditoría Vercel

- Obtener la versión actual de la guía.
- Revisar todos los archivos tocados.
- Entregar hallazgos `archivo:línea`.
- Corregirlos o documentar excepción concreta.

### 5. Prueba visual

- Capturas en los tres viewports.
- Teclado completo y zoom 200 %.
- Dark/light/reduced-motion.
- Datos vacíos, normales y extremos.
- Comparación antes/después centrada en el journey, no solo estética.

## Matriz mínima de pruebas

| Nivel | Qué prueba | Herramienta existente/nueva |
|---|---|---|
| Unitario JS | store, router, adapters, normalizadores | Node desde pytest, como tests actuales |
| Contrato HTML/CSS | landmarks, labels, tokens, prohibiciones | pytest estático |
| Integración | rutas FastAPI y APIs actuales | pytest |
| E2E | journeys y teclado | `tests/e2e/` + Playwright |
| Visual | desktop/tablet/mobile, dark/light | Playwright screenshots |
| Accesibilidad | roles, foco, zoom, reduced motion | guía Vercel + Playwright; axe opcional después |
| Rendimiento | startup, listas y layout thrashing | Performance marks + revisión de lecturas/escrituras |

Presupuestos iniciales:

- navegación percibida < 100 ms cuando no requiere red;
- interacción primaria responde < 100 ms;
- ninguna lista de más de 50 elementos se renderiza completa sin `content-visibility` o virtualización;
- cero layout shift por previews sin dimensiones;
- cero error de consola en los cinco journeys;
- cero regresión nueva de la guía Vercel.

## Definition of Done de un ticket UI

Un ticket no está terminado hasta que:

1. Respeta `DESIGN.md` y usa tokens/primitivos.
2. Tiene loading, empty, error, disabled/waiting y success si aplican.
3. Funciona con teclado, foco visible y lector mediante nombres/landmarks correctos.
4. Funciona en 1400×900, 1024×768 y 390×844.
5. Respeta dark, light y reduced motion.
6. Sus filtros/selección persistente están en URL cuando corresponde.
7. Tiene tests proporcionales y no rompe E2E existente.
8. Pasa la auditoría Vercel o documenta excepción.
9. Incluye capturas antes/después y una nota sobre el journey mejorado.
10. No aumenta deuda legacy fuera del área migrada.

## Prohibiciones de arquitectura

- No migrar a React/Tailwind como condición del overhaul. Sería otro proyecto y bloquearía valor visible.
- No copiar un `DESIGN.md` de una marca dentro de Faustus como fuente final.
- No instalar las tres skills y pedir “hazlo bonito”; deben ejecutarse en fases con autoridad distinta.
- No crear más navegación primaria mediante modales flotantes.
- No esconder información crítica en hover.
- No duplicar APIs o stores autoritativos para acelerar una pantalla.
- No introducir un segundo sistema de temas ni otra familia de iconos sin migración decidida.
- No convertir Activity en un volcado de logs.
- No cerrar un ticket por una captura atractiva si el journey, teclado o estados fallan.

## Primer incremento que debe programarse

El primer incremento recomendable es UI-000 a UI-022:

```text
DESIGN.md + journeys/capturas
→ tokens y primitivos
→ auditor de regresiones
→ store/router
→ AppShell con Inicio, Studio y destinos placeholder
→ command palette
```

Todavía no migra datos ni elimina modales. Demuestra identidad, navegación, accesibilidad, rollback y arquitectura. El segundo incremento debe ser Studio de código, porque reaprovecha el flujo más probado de chat/agente y obliga a resolver ContextBar, RunTimeline, diff, tests y permisos antes de abordar medios.

## Comandos de referencia de las skills

No se han instalado durante esta investigación. Si se decide instalarlas para los agentes:

```powershell
npx skills add leonxlnx/taste-skill@redesign-existing-projects
npx skills add vercel-labs/agent-skills@web-design-guidelines
```

Awesome DESIGN.md no es una skill ejecutable: es una colección de documentos. Los agentes deben consultar referencias concretas y sintetizar el `DESIGN.md` propio del proyecto con atribución interna, no importar las 73 identidades al contexto.

