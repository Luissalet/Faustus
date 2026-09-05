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

- [x] **UI-031 (parcial)** Barra de contexto: modelo (paleta cmdk con
      búsqueda), modo chat/agente, web, terminal y plan, persistidos.
      **Pendiente**: proyecto, memoria, referencias, skill y presupuesto
      editables antes de ejecutar (hoy el workspace llega solo desde la
      carpeta de la sesión).
- [x] **UI-032 (núcleo)** Studio en `/studio`: transcript en streaming SSE
      contra `/api/chat_stream`, historial, sesiones con filtro, razonamiento
      plegado, traza de herramientas viva en el carril (con comando y salida),
      tarjeta de aprobación (aprobar / toda la tarea / denegar) que reanuda el
      stream, parar, métricas por turno, fuentes web, imágenes generadas.
      Probado en el 7001 con `qwen3.5:9b` en chat y en agente con `ls`.
      Lote F: adjuntos, `@` menciones, `#` regla, comandos `/`, editar /
      regenerar / borrar, renombrar / archivar / exportar. Lote G: tarjeta
      del arnés (comprobaciones, veredicto, diff y revertir por fichero,
      volver al checkpoint, confirmar en git), progreso y plan en vivo,
      `/versions`, `/restore`, `/checkpoints`, selector de carpeta nativo
      (Explorador de Windows vía `/api/workspace/pick`, también en la
      anterior). Lote H: tablero de sub-agentes, panel lateral (navegador
      en vivo, documento del editor, visor de ficheros), traza y arnés
      restaurados del historial. Lote I: ordenar y agrupar en carpetas,
      selección múltiple, archivadas, fork, incógnito, presets, dictado y
      voz, citar, ↑, atajos de la anterior. **Pendiente**: Markdown
      completo, Deep Research / Group chat / Compare desde el compositor,
      fichas de mención, pill de GPU; retirar el chat legacy
      (`static/js/chat.js`) sólo cuando PARIDAD_FUNCIONAL §2–§5 no tenga
      filas «Anterior».
- [ ] **UI-033** Studio creativo: referencias con rol, variantes, máscara y
      receta. Aquí entra el prototipo que proponía el documento de producto.

## Incremento 3 — proyectos y artefactos

- [x] **UI-040** Projects fuera del modal: `/projects` y `/projects/{id}`.
- [x] **UI-041** Biblioteca federada, filtros en URL. **Pendiente**: TanStack
      Virtual por encima de 50 entradas, imágenes con dimensiones explícitas;
      verificar con datos reales (la instancia de desarrollo está vacía).
- [ ] **UI-042** ArtifactViewer y lineage. Requiere cerrar antes la deuda de
      identificadores (`PENDIENTES_UI.md`).

## Incremento 4 — actividad y automatización

- [x] **UI-050** Esquema común de run: queued, running, waiting-approval,
      paused, succeeded, failed, cancelled.
- [x] **UI-051** RunTimeline (Actividad sobre el carril) y ApprovalCard (en
      Studio). **Pendiente**: aprobaciones desde Actividad, no sólo desde el
      transcript.
- [x] **UI-052** Automatizaciones como recetas legibles; el editor de nodos
      sigue en la interfaz anterior como inspección avanzada.

## Incremento 5 — retirar el sistema antiguo

- [ ] **UI-060** Migrar settings y herramientas restantes. Hecho: grupo
      «Herramientas» en la barra y la paleta (lote J); Notas como pantalla
      (`/notes`, lote J; parcial: dibujo, foto, selección múltiple); Memoria
      como pantalla (`/memory`, lote K; los ajustes de Skills van con Skills);
      Calendario como pantalla (`/calendar`, lote L; cuentas CalDAV: anterior);
      Correo como pantalla (`/email`, lote M; cuentas, IA y programación: anterior);
      Ajustes como pantalla (`/settings`, lote N: modelos, IA por defecto, voz,
      búsqueda, recordatorios, agente completo, atajos, sistema; integraciones,
      cuentas, MCP, cuenta, usuarios y tema: anterior por pestaña);
      Agentes como pantalla (`/agents`, lote O: Workers, runners,
      definiciones y Expertos con su panel de revisión);
      idioma (inglés/español), apariencia (sistema/claro/oscuro) y barra
      lateral redimensionable (lote P).
      Pestañas legacy de Ajustes (cuenta, usuarios, herramientas, sistema:
      lote R; integraciones: S; modelos locales: T); Apariencia con el
      editor de tema y los efectos (lote U; Fondos era un sandbox sin ruta);
      Skills como pantalla (`/skills`, lote V); Automatizaciones completas
      (crear desde frase o forma, editar, pausar, ejecutar, historial: lote W);
      Actividad con ficha del run y decisiones (lote X); Proyectos completos
      (crear, ajustes, mandos del agente, chats, objetivos, memoria, actividad: lote Y);
      Galería (álbumes, etiquetas, favoritas, subir, visor, a chat: lote Z);
      editor de imagen completo en React (`/library/edit`, lote Z2: capas, máscaras,
      ajustes, selección, inpaint, quitar fondo, filtros, borradores, «Pide»);
      Correo completo (`/email`, lote AB: triaje, etiquetas, urgencia, IA,
      redactar con adjuntos y programación, bandeja de salida, bajas, ajustes);
      Deep Research como pantalla (`/research`, lote AC) y chip «Investigación»
      en el compositor;
      Compare como pantalla (`/compare`, lote AD: cuatro modos, hasta 8 paneles,
      a ciegas, votación y marcador);
      Biblioteca completa (documentos con importar/ordenar/exportar, chats, investigación,
      archivo) y editor de documentos a pantalla completa (`/documents/{id}`: barra
      Markdown, buscar, vistas, ejecutar, versiones con revisión, exportar, PDF con campos,
      anotaciones y firmas: lote AA).
      Faltan: Cookbook, Deep Research, Compare,
      Tournament, Procedencia, Historial importado. Vitales (uso de GPU) en
      Studio: lote Q.
- [ ] **UI-061** Borrar los puentes muertos, el DOM legacy de cada pantalla ya
      migrada y el propio flag. Empezar a dividir `style.css`, a solas y sin
      mezclarlo con features.
- [ ] **Skill propia `faustus-ui-studio`**: inputs `screen`, `user_job`,
      `project_type`, `existing_components`; outputs `design_brief`,
      `component_plan`, `implementation`, `visual_qa_report`.
