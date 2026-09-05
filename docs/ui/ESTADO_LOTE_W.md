# Estado del lote W — Automatizaciones completas

Fecha: 05-09-2026. Rama `feat/studio-ui`.

## Brief de diseño (frontend-design + impeccable `shape`, modo Operate)

**Trabajo y persona.** Luis, decidiendo qué corre solo y cuándo: ver de
un vistazo qué está activo, qué produjo la última vez, pararlo o
lanzarlo, y escribir una receta nueva sin aprender el modelo de datos.

**Lo que la anterior hacía mal.** Un modal con cuatro pestañas; la
receta era una tarjeta con un kebab de siete entradas; el formulario
construía selectores de fecha y hora a mano (los que Luis llamó «esa
ventana de mierda»), cambiaba de forma según el tipo y guardaba la hora
en UTC sin decirlo; el cron se enseñaba crudo; el historial era otra
vista que sustituía la lista.

**Dirección elegida.**
- Misma anatomía que Skills y Correo: lista de recetas a la izquierda,
  una receta en pleno a la derecha. La fila ES la frase del producto
  («Cada lunes a las 10:30 → Preparar resumen · próxima en 2 d»).
- Los cron habituales se leen en palabras («Cada 2 horas», «Cada día a
  las 06:00, 18:00»); lo raro se deja tal cual.
- Nueva automatización = una frase que el servidor redacta, o siete
  formas; después el formulario entero a la vista, con `date`/`time`
  nativos en hora local y lo secundario (modelo, encadenado, avisos) en
  un `details` que se abre solo si trae algo.
- Las confirmaciones dicen la consecuencia («Sus ejecuciones se van con
  ella»; «La antigua deja de funcionar»), y el 409 de «ya corre» es un
  diálogo con la opción real: lanzar otra al lado o esperar.
- Sin movimiento nuevo.

**Revisión contra defaults**: sin tarjetas iguales salvo las siete formas
(que sí son equivalentes), sin eyebrows, sin numeración, un solo acento.

## Qué hay

`adapters/automations.ts` (todo `/api/tasks`: CRUD, pausar/reanudar,
ejecutar/parar, revertir, vaciar caché, webhook, runs, metadatos, parse,
onboarding, reglas de correo urgente; vocabulario de tasks.js: categorías,
etiquetas de caché, presets, personas, codificación del destino de
correo, hora local↔UTC, `describeCron`), `screens/Automations.tsx`
(lista, filtros, selección, panel: detalle + historial + nueva + form),
`screens/automations/Form.tsx`, `screens/automations.css`.

## Verificado en el 7001

Lista de 11 integradas con categorías; abrir Email Tags; reanudar →
pausar; Editar: acción, cron, reglas de triaje cargadas, salida `none`;
Guardar sin cambios → «Guardado»; Nueva → forma «Prompt con horario» →
nombre, prompt, semanal, 10:30 → «Creada» y seleccionada con «Cada lunes
a las 10:30 · … · a una conversación», próxima en 2 d; Ejecutar ahora →
«En marcha», el historial muestra la ejecución (abortada: el modelo estaba
ocupado con la prueba de una skill); Borrar con diálogo; Nueva → frase
«every friday at 17:00 summarise my week» → borrador «Weekly Summary»,
semanal, viernes, 17:00, prompt redactado (el 27b tardó ~45 s). Móvil 420
px: lista y panel con «Todas las automatizaciones». Capturas con
Playwright.

## Crítica (a mano)

- P2 corregido: `dd` con el margen del navegador desalineaba los datos
  (también en Skills).
- P2 corregido: «Every 1 hours»; el icono de Detener parecía una casilla.
- P3 abierto: Detener siempre visible (PENDIENTES 88); avisos de
  escritorio (86).
