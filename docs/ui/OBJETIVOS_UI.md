# Objetivos de UI — backlog vivo

Rama: `feat/studio-ui`. Estado a 04-09-2026.
Decisiones que mandan: `DECISIONES_UI.md`. Detalle: `PLAN_CODE_UI_FAUSTUS_STUDIO.md`.

Marca `[x]` sólo cuando el ticket cumple su Definition of Done (§DoD del plan):
estados completos, teclado y foco, tres viewports, dark/light/reduced-motion,
tests proporcionales, auditoría Vercel pasada o excepción documentada, y
capturas antes/después.

## Incremento 1 — cimientos + Inicio

### Lote A — autoridad visual y evidencia

- [ ] **UI-000** Journeys y capturas baseline
      `tests/e2e/test_studio_baseline.py`, `docs/ui/journeys.md`.
      1400×900, 1024×768 y 390×844. Cinco journeys: crear proyecto, trabajo
      creativo, tarea de código, encontrar un resultado, resolver aprobación.
      Con clics, tiempo y puntos de confusión anotados.
- [ ] **UI-001** `DESIGN.md` + `docs/ui/component-contracts.md`
      Bloqueante duro de todo lo demás.
- [ ] **UI-010** `static/css/studio/tokens.css`, `base.css`, `legacy-bridge.css`
      Cargados después de `style.css`. Aliases legacy vivos, temas custom
      intactos, foco global visible, `color-scheme`, skip link, reduced motion.

### Lote B — primitivos, guardas e iconos

- [ ] **UI-011** Primitivos accesibles
      Button, IconButton, StatusBadge, EmptyState, Menu, Dialog, Skeleton.
      Test estático que prohíbe HTML interactivo crudo en módulos nuevos.
- [ ] **UI-012** Auditor incremental de la guía Vercel
      `tools/audit_ui_guidelines.py` + `docs/ui/audit-baseline.md` con fecha y
      hash de la guía. Separa `legacy-known` de regresión nueva. CI falla sólo
      por deuda nueva.
- [ ] **UI-013** Inventario y extracción de iconos
      `static/js/shell/components/icon.js`. Sin librería externa. Prohibido SVG
      inline en módulos Studio.

### Lote C — shell y navegación

- [ ] **UI-020** Store + router + fallback SPA
      Lista blanca de siete rutas en `app.py`, nunca comodín. Test: 404 JSON de
      API sigue siendo 404 JSON. Filtros en query string. Hashes de sesión
      siguen funcionando.
- [ ] **UI-021** AppShell bajo flag `faustus_studio_shell`
      `localStorage` + `?shell=studio` / `?shell=legacy`. Apagado por defecto.
      Acceso "Interfaz anterior" visible durante el piloto.
- [ ] **UI-022** Command palette
      `Ctrl/Cmd+K`. "Buscar conversaciones" pasa a ser un comando, no un atajo
      rival. Navegación esencial completable sólo con teclado.

### Lote D — primera pantalla real

- [ ] **UI-030** Inicio
      Continuaciones, aprobaciones pendientes, quick starts y salud sólo si
      bloquea. Estados loading, vacío, error, offline y éxito. Sin modelos,
      temperatura ni GPU como contenido principal.

## Incremento 2 — Studio

- [ ] **UI-031** ContextBar: proyecto, memoria, referencias, skill, backend y
      presupuesto legibles y editables antes de ejecutar.
- [ ] **UI-032** Studio de código: modos explorar/planificar/implementar/revisar,
      workspace, diff, tests y checkpoint como paneles. El E2E de agentes sigue
      pasando.
- [ ] **UI-033** Studio creativo: referencias con rol, variantes, máscara y
      receta. Aquí entra el prototipo que proponía el documento de producto.

## Incremento 3 — proyectos y artefactos

- [ ] **UI-040** Sacar Projects del modal: `/projects` y `/projects/{id}`.
- [ ] **UI-041** Biblioteca federada por adapters, filtros en URL,
      virtualización por encima de 50 entradas.
- [ ] **UI-042** ArtifactViewer y lineage. Requiere cerrar antes la deuda de
      identificadores (ver `PENDIENTES_UI.md`).

## Incremento 4 — actividad y automatización

- [ ] **UI-050** Esquema común de run: queued, running, waiting-approval,
      paused, succeeded, failed, cancelled. Normalizado en frontend primero.
- [ ] **UI-051** RunTimeline y ApprovalCard.
- [ ] **UI-052** Automatizaciones como recetas legibles; el editor de nodos es
      inspección avanzada, no la vista principal.

## Incremento 5 — retirada del sistema antiguo

- [ ] **UI-060** Migrar settings y herramientas restantes.
- [ ] **UI-061** Eliminar puentes muertos y empezar a dividir `style.css`, sólo
      tras confirmar cero imports y E2E equivalente.
- [ ] **Skill propia `faustus-ui-studio`**: inputs `screen`, `user_job`,
      `project_type`, `existing_components`; outputs `design_brief`,
      `component_plan`, `implementation`, `visual_qa_report`.
