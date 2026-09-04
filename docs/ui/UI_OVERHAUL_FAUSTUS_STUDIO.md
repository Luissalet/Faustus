# Overhaul UI: Faustus Studio, no otro chat con una barra lateral

> **Lee antes `DECISIONES_UI.md`.** Manda sobre este documento donde haya
> conflicto. En concreto: la «Propuesta de primer prototipo» de Studio creativo
> del final queda **aplazada** al segundo incremento; se empieza por cimientos
> más Inicio.

Fecha: 04-09-2026  
Base revisada: `static/index.html`, `static/style.css`, `static/app.js` y módulos de `static/js/` de Faustus.

Plan de programación detallado: `PLAN_CODE_UI_FAUSTUS_STUDIO.md`.

## Diagnóstico

Faustus ya tiene mucho más que un chat: proyectos, galería, editor, memoria, skills, documentos, investigación, correo, calendario, tareas, agentes externos, modelos y ajustes. El problema de la interfaz no es falta de capacidades; es que muchas se presentan como entradas de una barra lateral, pestañas o modales independientes.

Eso obliga al usuario a saber **dónde vive una función** antes de poder pensar en **qué quiere conseguir**. Para un producto multipropósito, la navegación debe empezar por la intención y el proyecto, no por el subsistema interno.

## Idea rectora

**Faustus Studio:** un estudio personal con cuatro verbos principales:

```text
Crear · Trabajar · Automatizar · Conservar
```

- **Crear:** imágenes, vídeo, audio, documentos y escritura.
- **Trabajar:** chat, código, investigación, navegador, archivos y tareas de agente.
- **Automatizar:** workflows, calendario, correo, triggers y entregas.
- **Conservar:** proyectos, memoria, activos, recetas, fuentes y decisiones.

El chat sigue existiendo, pero pasa a ser el compositor universal de intenciones, no la única pantalla de la aplicación.

## Nueva arquitectura de información

```text
Faustus
├─ Inicio                 qué sigue hoy y cómo empezar
├─ Studio                 una intención → plan → ejecución → resultados
├─ Proyectos              contexto, personas, archivos, memoria y actividad
├─ Biblioteca              todos los artefactos y sus recetas
├─ Automatizaciones        workflows, runs y aprobaciones
└─ Actividad               tareas, notificaciones, errores y auditoría

Configuración             modelos, conexiones, seguridad, apariencia y sistema
```

La barra izquierda queda reducida a esos cinco destinos. Las herramientas técnicas, modelos, runners, comparación, etc. aparecen contextualmente o dentro de Configuración/Actividad, no compiten con el trabajo principal.

## La pantalla central: Studio

```text
┌──────────────┬─────────────────────────────────────────────────────────────┬──────────────────────┐
│ Navegación   │ Proyecto: Campaña Lira · Brief activo                        │ Inspector            │
│              │                                                             │ Contexto             │
│ Inicio       │  ¿Qué quieres conseguir?                                    │  ✓ Proyecto          │
│ Studio       │  [ Crear un vídeo de 15s para el lanzamiento...          ]  │  ✓ Personaje Lira    │
│ Proyectos    │                                                             │  ✓ 3 referencias     │
│ Biblioteca   │  Crear   Escribir   Programar   Investigar   Automatizar    │  Skill               │
│ Automatizar  │                                                             │  Video corto v1      │
│ Actividad    │  [Imagen] [Vídeo] [Documento] [Código] [Investigación]     │                      │
│              │                                                             │  Ejecución           │
│              │  Plan propuesto                                             │  Local GPU           │
│              │  1. Guion  2. Storyboard  3. Render  4. Revisión            │  12 min estimados    │
│              │  [Editar plan] [Ejecutar]                                   │                      │
│              │                                                             │  [Detalles]          │
└──────────────┴─────────────────────────────────────────────────────────────┴──────────────────────┘
```

La misma pantalla se adapta al trabajo, en lugar de abrir un modal nuevo por cada capacidad:

- Al crear una imagen aparecen referencias con roles (`identidad`, `pose`, `estilo`, `composición`), variantes y máscara de inpaint.
- Al escribir, aparecen estructura, fuentes, tono y documento vivo.
- Al programar, aparecen workspace, modo `explorar/planificar/implementar/revisar`, diff y pruebas.
- Al automatizar, aparecen el workflow, próximos pasos, costes, espera y aprobaciones.

## Principios de interacción

### 1. Contexto visible antes de enviar

Encima del compositor deben aparecer chips legibles y editables:

```text
Proyecto: Campaña Lira ×   Personaje: Lira ×   3 referencias ×
Skill: Vídeo corto v1 ×    Backend: GPU local ×    Memoria: proyecto
```

El usuario ve qué sabe y qué usará Faustus. Es más importante que enseñar temperatura, tokens o nombres de modelos por defecto.

### 2. Divulgación progresiva

Tres niveles de control:

- **Simple:** objetivo, referencias, formato, calidad y presupuesto.
- **Dirección:** estilo, plantilla de skill, composición, continuidad, variantes y aprobaciones.
- **Experto:** modelo, seed, adapter, workflow, ejecución, logs y receta exportable.

No hay una segunda app para expertos. Se abre profundidad donde es relevante, mediante un inspector lateral contextual.

### 3. Las skills son recetas, no cromos

Las tarjetas llamativas de skills pueden funcionar para descubrir capacidades, pero no deben convertirse en una tienda interminable. Una skill bien presentada responde a cinco preguntas:

```text
Qué resultado obtengo · Qué necesito aportar · Cuánto tarda/cuesta ·
Dónde se ejecuta · Qué puede leer/hacer
```

Ejemplo de tarjeta compacta:

```text
Vídeo corto desde brief                         Local / GPU
Convierte un guion y referencias en un clip con subtítulos.
Necesita: brief + 1–5 referencias · 8–15 min
Usa: memoria de proyecto · no publica sin aprobación
[Usar]  [Ver ejemplo]  [Inspeccionar receta]
```

El catálogo se filtra por intención, proyecto, hardware disponible y permisos. Las skills usadas recientemente o compatibles con el material seleccionado aparecen primero.

### 4. El resultado es un objeto de trabajo, no un mensaje perdido

Cada output se abre en una vista de artefacto con cuatro acciones principales:

```text
Usar en… · Variar · Corregir · Ver receta
```

Una imagen abre su árbol de variantes, referencias, máscara de corrección y procedencia. Un vídeo abre timeline, subtítulos, assets fuente y exportaciones. Un cambio de código abre diff, tests y checkpoint. Así la galería deja de ser un álbum y se convierte en biblioteca de producción.

### 5. La actividad no invade el chat

Los pasos de un agente, render o workflow se muestran como una línea de tiempo plegable de run:

```text
● Preparando referencias     ✓
● Generando 8 variaciones    62% · 2 min
○ Revisión de calidad        esperando
○ Aprobación para publicar   pendiente
```

El chat conserva la conversación y la decisión; la actividad ofrece el detalle auditable. Esto reduce el ruido de tool calls largos sin ocultar evidencia.

## Pantallas clave

### Inicio: "¿qué quieres terminar hoy?"

No es un dashboard de métricas. Debe incluir:

- continuar los tres proyectos/runs recientes;
- aprobaciones y tareas que esperan al usuario;
- quick starts según el contexto: «crear personaje», «hacer vídeo», «escribir informe», «arreglar código»;
- una zona pequeña de salud: GPU/cola/servicios solo si afecta al trabajo;
- recomendaciones concretas: «tienes 12 referencias de Lira: crea una ficha de personaje».

### Proyecto: la unidad de trabajo real

Un proyecto reemplaza la dispersión entre chat, carpeta, galería y memoria. Cabecera fija:

```text
Campaña Lira     Estado: activa       [Nuevo trabajo]
Brief · Activos · Personajes · Documentos · Código · Automatizaciones · Actividad
```

- **Brief:** propósito, audiencia, restricciones, decisiones.
- **Activos:** imágenes, vídeo, audio y documentos relacionados.
- **Personajes:** fichas y paquetes de referencia.
- **Documentos/Código:** según el tipo de proyecto, no siempre ambos.
- **Automatizaciones:** solo las vinculadas a ese proyecto.
- **Actividad:** runs, aprobaciones, coste y procedencia.

### Biblioteca: una galería que sabe de dónde viene todo

Filtros por proyecto, tipo, persona/personaje, skill, modelo, fecha, estado y derechos. Un asset muestra un lineage tree:

```text
Brief → storyboard-03 → image-03b → inpaint-face → video-shot-04 → vídeo-final
```

No se mezclan todas las imágenes en una sola cuadrícula sin contexto.

### Automatizaciones: estado, no diagrama por obligación

Primero se ven las recetas legibles:

```text
Cada lunes · Preparar resumen de investigación · Próxima ejecución: 09:00
Material nuevo → Crear borrador → Esperar aprobación → Enviar por correo
```

El diagrama de nodos es una inspección avanzada. La acción central es pausar, revisar, aprobar, duplicar o corregir el próximo paso.

## Lenguaje visual

### Dirección estética

Evitar tanto el "SaaS azul genérico" como el cyberpunk recargado. La propuesta es **editorial, cálida y técnica**:

- fondo carbón/gris muy profundo o papel cálido en tema claro;
- una tinta/acento principal de marca y colores semánticos reservados para estado;
- tipografía de lectura fuerte para títulos y una monoespaciada solo para código, rutas y recetas;
- previews grandes cuando el contenido sea visual; densidad mayor cuando sea código/datos;
- elevación muy contenida: paneles planos, bordes sutiles, sombras solo para focos/modales;
- motion breve y funcional: expansión de inspector, progreso de run, cambio de variante; nunca decorativa por defecto.

### Semántica de color estable

| Color | Significado único |
|---|---|
| Acento de marca | acción principal / selección |
| Azul o violeta | trabajo en curso / agente |
| Verde | completado/verificado |
| Ámbar | necesita revisión/aprobación |
| Rojo | fallo, bloqueo o acción destructiva |

No usar el color de cada herramienta como identidad principal: el usuario debe percibir un solo producto.

## Qué cambiar primero en la UI actual

| Prioridad | Cambio | Archivos actuales más implicados |
|---|---|---|
| P0 | Reducir navegación a Inicio, Studio, Proyectos, Biblioteca, Automatizaciones y Actividad. Mantener el resto detrás de contexto/ajustes. | `static/index.html`, `static/js/sidebar-layout.js`, `static/js/panels.js`, `static/js/ui_visibility.js` |
| P0 | Sustituir la dicotomía técnica `Agent / Chat` por intenciones visibles; el routing interno puede conservar ambos modos. | `static/index.html`, `static/js/chat.js`, `static/js/assistant.js`, `static/js/agentHarnessUI.js` |
| P0 | Añadir barra de contexto editable al compositor y un inspector derecho contextual. | `static/index.html`, `static/js/projects.js`, `static/js/skills.js`, `static/js/memory.js`, `static/style.css` |
| P1 | Convertir las salidas de chat/generación en tarjetas de artefacto reutilizables, con lineage/provenance. | `static/js/chatRenderer.js`, `static/js/gallery.js`, `static/js/provenance.js`, `static/js/galleryEditor.js` |
| P1 | Crear páginas de proyecto y biblioteca; reducir el uso de modales grandes para navegación primaria. | `static/index.html`, `static/js/projects.js`, `static/js/documentLibrary.js`, `static/js/gallery.js` |
| P1 | Mostrar runs como timeline plegable, no como texto de herramientas dentro del chat. | `static/js/agentHarnessUI.js`, `static/js/chatStream.js`, `static/js/tasks.js`, `static/js/agentRunners.js` |
| P2 | Añadir Studio creativo con referencias por rol, variantes, máscaras y recetas. | `static/js/galleryEditor.js`, `static/js/editor/*`, `static/js/fileHandler.js`, nuevas vistas `static/js/studio/*` |
| P2 | Sustituir settings dispersos por búsqueda global + páginas por intención. | `static/js/settings/*`, `static/js/agentSettings.js`, `static/js/modelControls.js` |

## Estrategia de implementación sin rehacer la aplicación

1. **No reescribir `index.html` de golpe.** Introducir un `AppShell` que monte pantallas en el área principal y conviva temporalmente con paneles existentes.
2. Crear un estado de UI mínimo y único: ruta, proyecto activo, run activo, panel inspector y selección de artefactos. Evitar que cada módulo o modal tenga su propio concepto de contexto.
3. Extraer tokens de diseño de `static/style.css`: espaciado, tipografía, elevación, colores semánticos, tamaños de icono y breakpoints. El tema actual puede seguir funcionando como capa de compatibilidad.
4. Migrar una pantalla cada vez: primero Inicio/Studio, luego Proyectos/Biblioteca, después Automatizaciones/Actividad.
5. Medir antes y después con tres tareas: crear un proyecto, generar/editar un asset y ejecutar una tarea de código. La nueva UI gana solo si reduce pasos y dudas.

## Stack de skills para que los agentes mantengan el nivel visual

La estética no debe depender de que el agente de turno tenga buen gusto. Cada cambio visual relevante pasa por una secuencia de skills con responsabilidades distintas:

```text
brief de producto
→ dirección visual
→ arquitectura de componentes
→ implementación
→ revisión visual + accesibilidad
→ screenshots de regresión
```

### Skills externas recomendadas para los agentes que implementen Faustus

| Skill | Uso obligatorio | Veredicto |
|---|---|---|
| [`anthropics/skills@frontend-design`](https://www.skills.sh/anthropics/skills/frontend-design) | Antes de diseñar una pantalla nueva o rehacer una existente. Obliga a escoger una dirección tipográfica, espacial y de movimiento deliberada, no una plantilla genérica. | **Base creativa.** 851K instalaciones, repositorio de Anthropic y auditorías publicadas como correctas. |
| [`pbakaus/impeccable@impeccable`](https://www.skills.sh/pbakaus/impeccable/impeccable) + sus subskills [`critique`](https://www.skills.sh/pbakaus/impeccable/critique), [`polish`](https://www.skills.sh/pbakaus/impeccable/polish) y [`delight`](https://www.skills.sh/pbakaus/impeccable/delight) | Tras tener una pantalla funcional: crítica independiente, corrección de jerarquía/espaciado/estados y microinteracciones que aporten algo real. | **El acabado que suele verse en redes.** Es una suite de diseño iterativo con 260K instalaciones para la skill base y ~65K estrellas del repositorio. Usar sus ideas, pero respetar el presupuesto de rendimiento y `prefers-reduced-motion`. |
| [`vercel-labs/agent-skills@web-design-guidelines`](https://www.skills.sh/vercel-labs/agent-skills/web-design-guidelines) | Al terminar cada cambio UI. Revisa diseño, interacción y accesibilidad sobre archivos reales y devuelve hallazgos por línea. | **Quality gate.** 605K instalaciones, repositorio Vercel. No sustituye pruebas manuales ni de teclado. |
| [`leonxlnx/taste-skill@redesign-existing-projects`](https://github.com/leonxlnx/taste-skill/tree/main/skills/redesign-skill) | Para criticar la UI existente antes de migrar cada superficie. La skill principal se limita a Inicio, onboarding y superficies expresivas. | **Acento, no ley.** Taste Skill v2 declara que no es para dashboards, tablas ni flujos multipaso; no debe gobernar el núcleo de producto de Faustus. |
| [`VoltAgent/awesome-design-md`](https://github.com/VoltAgent/awesome-design-md) | Consultar uno o dos `DESIGN.md` por patrón y sintetizar un `DESIGN.md` propio de Faustus. | **Biblioteca, no skill ejecutable.** Sus identidades sirven para estudiar anatomía visual; no se copia una marca completa. |

Skills que circulan mucho pero que **no** usaría como base automática: `ui-ux-pro-max` (su ficha actual muestra una auditoría fallida, aunque tenga 343K instalaciones) y `anti-ui-slop` de `uizze.com` (muy popular, 653K instalaciones, pero conviene revisar manualmente procedencia/contenido antes de darle acceso al agente). `extract-design-system` es útil solo si se parte de una web pública autorizada como referencia; extrae tokens, no sustituye un diseño propio.

### Skill propia: `faustus-ui-studio`

Además de skills externas, Faustus necesita una skill de proyecto, pequeña y estricta, que conozca su arquitectura actual y convierta las buenas intenciones en una rutina repetible. Debe vivir junto a las integraciones de agentes de Faustus y exigir:

1. Leer la pantalla y módulos afectados antes de proponer cambios.
2. Declarar el trabajo principal de la pantalla y el contexto del usuario antes de dibujar componentes.
3. Usar tokens del sistema de diseño; no añadir colores, sombras, tamaños o animaciones aislados.
4. Mantener el contenido/proyecto visible por encima de controles técnicos.
5. Implementar estados vacío, cargando, error, bloqueado por aprobación y completado.
6. Comprobar teclado, foco, contraste, responsive y reducción de movimiento.
7. Abrir la pantalla, tomar screenshots de escritorio/móvil y comparar antes/después antes de cerrar el trabajo.
8. Ejecutar la skill de auditoría de Vercel; cualquier excepción debe justificarse en el PR/run.

El manifiesto podría declarar como inputs `screen`, `user_job`, `project_type`, `existing_components` y como outputs `design_brief`, `component_plan`, `implementation`, `visual_qa_report`. Así deja de ser una recomendación estética y pasa a ser una capacidad instalable/medible de Faustus.

La arquitectura, los tokens, las rutas, el backlog UI-000 a UI-061, la estrategia de coexistencia con el frontend sin build y la matriz de pruebas están especificados en `PLAN_CODE_UI_FAUSTUS_STUDIO.md`.

## Criterios de éxito

- Una persona nueva puede iniciar un proyecto y elegir una skill útil en menos de un minuto.
- Antes de ejecutar, entiende qué proyecto, referencias, memoria, backend y coste se usarán.
- Puede volver a cualquier resultado y continuarlo sin rebuscar en un chat anterior.
- Una aprobación bloqueada es imposible de ignorar y explica exactamente qué acción liberará.
- Las funciones avanzadas siguen disponibles, pero no convierten la primera pantalla en una cabina de avión.
- La UI se percibe como una sola herramienta, aunque integre agentes, ComfyUI, modelos locales, calendario y archivos.

## Propuesta de primer prototipo

No empezar por todas las pantallas. Construir un prototipo navegable de **Studio de proyecto creativo** con:

1. selector de proyecto y barra de contexto;
2. compositor con chips `Crear imagen`, `Crear vídeo`, `Escribir`, `Programar`;
3. una tarjeta de skill con inputs/tiempo/backend/permisos;
4. timeline de run;
5. galería de tres variantes y la acción `Corregir zona`;
6. inspector de receta y procedencia.

Si esa ruta se siente clara, el mismo shell se reutiliza para código, investigación y automatizaciones. Si no se siente clara, no hay que maquillar las otras veintenas de paneles todavía.
