# Estado del lote Y — Proyectos completos

Fecha: 05-09-2026. Rama `feat/studio-ui`.

## Brief (modo Operate)

**Trabajo.** Un proyecto es el sitio donde el agente trabaja con normas:
carpeta, instrucciones, memoria, objetivos y sus manías (confianza,
revisión, checkpoints, tests). La página debe permitir empezar a
trabajar ahí en un gesto y ver, sin abrir nada, cómo se va a comportar.

**Lo que faltaba.** La página de Studio solo leía. El modal anterior
tenía crear/editar/borrar, fijar, archivar, exportar, chats del proyecto,
raíces de trabajo, objetivos con estado/prioridad/notas/bloqueos,
memoria editable, actividad del agente con enlace a la respuesta exacta,
y sincronizaba el workspace con el proyecto al cambiar de chat.

**Dirección.** Pestañas por asunto en vez de un hub con todo apilado; el
Resumen abre con el compositor del proyecto (modelo + frase) porque
empezar a trabajar es la acción; tres tarjetas debajo con contenido
distinto (instrucciones, el agente aquí con puntos verdes, raíces). Los
ajustes son un formulario completo a la vista, con el diálogo nativo
para la carpeta (la regla de Luis: «esa ventana de mierda» no vuelve).
`/projects/new` es la misma página con solo el formulario. En Studio, un
chip con el nombre del proyecto en la cabecera, y el workspace sigue al
proyecto con la misma clave de siempre.

## Qué hay

`adapters/projects.ts` (todo `/api/projects/*`, chats por carpeta,
empezar conversación filed en el proyecto, exportar, AGENTS.md),
`screens/Project.tsx`, `screens/project/{Settings,Objectives,Memory,
Audit}.tsx`, `screens/Projects.tsx` (nuevo, archivados, fijados),
`projects.css` (bloque Y), `Studio.tsx` (chip + workspace del proyecto,
`?m=` para saltar a un mensaje), `Transcript.tsx` (`data-db-id`).

## Verificado en el 7001

Crear «Studio smoke project» con carpeta, instrucciones y modo revisión
→ página con los mandos; objetivos: OBJ-1, OBJ-2 bloqueado por OBJ-1,
estado a en curso, notas guardadas, descartar OBJ-2 (API confirma);
memoria: MEMORY.md creado por el scaffold, leer, editar, guardar;
contexto literal; fijar; empezar conversación → `/studio?s=…` con el
chip «Studio smoke project», la sesión en la carpeta del proyecto y el
workspace `_claude_tmp` aplicado; Chats muestra la conversación; borrar
la conversación (por `/api/session`, ver PENDIENTES 89); archivar →
aviso, restaurar; borrar el proyecto → lista. Móvil 420 px.

## Crítica

- P2 corregido: «Examinar…» caía debajo del campo (el `.fs-field` a 100%
  dentro de la fila).
- P3 abierto: 89, 90.
