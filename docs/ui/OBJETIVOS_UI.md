# Objetivos de UI — backlog vivo

Rama: `feat/studio-ui`. Estado a 04-09-2026.
Decisiones que mandan: `DECISIONES_UI.md` (stack React, §1). Detalle de
pantallas: `PLAN_CODE_UI_FAUSTUS_STUDIO.md` y `UI_OVERHAUL_FAUSTUS_STUDIO.md`.

Marca `[x]` sólo cuando el ticket cumple la Definition of Done: estados
completos, teclado y foco, tres viewports, dark/light/reduced-motion, tests
proporcionales, auditoría de la guía Vercel pasada o excepción documentada,
**abierto en el navegador del 7001** y capturas antes/después.

## Incremento 1 — cimientos y primera pantalla

### Lote A — autoridad visual, evidencia y toolchain

- [x] **UI-000** Journeys y capturas baseline de la UI **actual**
      `tests/e2e/test_studio_baseline.py`, `docs/ui/journeys.md`.
      1400×900, 1024×768 y 390×844. Cinco journeys: crear proyecto, trabajo
      creativo, tarea de código, encontrar un resultado, resolver aprobación.
      Con clics, tiempo y puntos de confusión anotados. Es la vara de medir:
      sin esto, «la nueva mola más» no es comprobable.
- [x] **UI-001** `DESIGN.md` + `docs/ui/component-contracts.md`
      Bloqueante duro de todo lo demás.
- [x] **UI-002** Toolchain Vite y arranque integrado
      `package.json`, `vite.config`, salida a `static/studio/`, servida con el
      nonce existente. Build reproducible sin red. `Start-Faustus.ps1` construye
      si el bundle falta o está obsoleto, y falla diciendo por qué antes que
      servir uno viejo en silencio. Presupuesto de bundle declarado aquí.

### Lote B — tokens, primitivos y guardas

- [x] **UI-010** Tokens CSS y puente con el tema legacy
      Tokens de `DESIGN.md` como variables. Aliases vivos (`--bg`, `--panel`,
      `--fg`, `--red`) para que los temas personalizados actuales no se rompan.
      Foco global visible, `color-scheme`, skip link, reduced motion.
- [x] **UI-011** Primitivos sobre Radix
      Button, IconButton, StatusBadge, EmptyState, Menu, Dialog, Popover,
      Skeleton. `data-testid` estable en todos desde el primer día.
- [x] **UI-012** Guardas y auditoría adaptadas a JSX
      Lint: nada de `<div>` con `onClick`, ningún control sin nombre accesible,
      ningún color, radio o duración fuera de tokens, ningún `transition: all`.
      Accesibilidad comprobada sobre la página renderizada en Playwright.
      `docs/ui/audit-baseline.md` con fecha y hash de la guía Vercel.

### Lote C — shell y navegación

- [x] **UI-020** Router, store y fallback SPA
      React Router con las siete rutas canónicas y filtros en query string.
      Store Zustand con la forma que define el plan. En `app.py`, lista blanca
      de siete rutas, **nunca** comodín, con test de que un 404 de API sigue
      siendo JSON. Los hashes de sesión siguen funcionando.
- [x] **UI-021** AppShell React bajo flag `faustus_studio_shell`
      Montado en el shell nuevo, DOM legacy intacto al lado. Apagado por
      defecto, `?shell=studio` / `?shell=legacy`, acceso «Interfaz anterior»
      visible durante el piloto.
- [x] **UI-022** Command palette con `cmdk`
      `Ctrl/Cmd+K`. «Buscar conversaciones» pasa a ser un comando y deja de
      competir por el atajo. Navegación esencial completable sólo con teclado.

### Lote D — primera pantalla real

- [x] **UI-030** Inicio
      Continuaciones, aprobaciones pendientes, quick starts, y salud sólo si
      bloquea. Estados loading, vacío, error, offline y éxito. Sin modelos,
      temperatura ni GPU como contenido principal. Pasa por `impeccable` antes
      de cerrarse.

## Incremento 2 — Studio

- [ ] **UI-031** ContextBar: proyecto, memoria, referencias, skill, backend y
      presupuesto legibles y editables antes de ejecutar.
- [ ] **UI-032** Studio de código: modos explorar/planificar/implementar/revisar,
      workspace, diff, tests y checkpoint como paneles. El E2E de agentes sigue
      pasando. Retira su equivalente legacy al cerrarse.
- [ ] **UI-033** Studio creativo: referencias con rol, variantes, máscara y
      receta. Aquí entra el prototipo que proponía el documento de producto.

## Incremento 3 — proyectos y artefactos

- [ ] **UI-040** Projects fuera del modal: `/projects` y `/projects/{id}`.
- [ ] **UI-041** Biblioteca federada, filtros en URL, TanStack Virtual por
      encima de 50 entradas, imágenes con dimensiones explícitas.
- [ ] **UI-042** ArtifactViewer y lineage. Requiere cerrar antes la deuda de
      identificadores (`PENDIENTES_UI.md`).

## Incremento 4 — actividad y automatización

- [ ] **UI-050** Esquema común de run: queued, running, waiting-approval,
      paused, succeeded, failed, cancelled.
- [ ] **UI-051** RunTimeline y ApprovalCard.
- [ ] **UI-052** Automatizaciones como recetas legibles; el editor de nodos es
      inspección avanzada, no la vista principal.

## Incremento 5 — retirar el sistema antiguo

- [ ] **UI-060** Migrar settings y herramientas restantes.
- [ ] **UI-061** Borrar los puentes muertos, el DOM legacy de cada pantalla ya
      migrada y el propio flag. Empezar a dividir `style.css`, a solas y sin
      mezclarlo con features.
- [ ] **Skill propia `faustus-ui-studio`**: inputs `screen`, `user_job`,
      `project_type`, `existing_components`; outputs `design_brief`,
      `component_plan`, `implementation`, `visual_qa_report`.
