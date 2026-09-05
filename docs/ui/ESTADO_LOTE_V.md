# Estado del lote V — Skills

Fecha: 05-09-2026. Rama `feat/studio-ui`.

## Brief de diseño (frontend-design + impeccable `shape`, modo Operate)

**Trabajo y persona.** Luis, revisando lo que el asistente ha aprendido:
qué procedimientos merecen confianza (publicar), cuáles probar, cuáles
son ruido (duplicadas, genéricas, fallidas). Es triaje sobre una lista con
evidencia por fila; no es una página que se mire, es una que se usa.

**Lo que la anterior hacía mal** (crítica antes de migrar): una pila de
tarjetas con hasta seis píldoras por cabecera y un menú kebab que
escondía las acciones; ordenar y filtrar mezclados en un `<select>` con
dos `optgroup`; la tarjeta expandida solo enseñaba el SKILL.md crudo;
el formulario de alta y el importador ocupaban la cabecera siempre; los
ajustes de aprendizaje vivían en otra pestaña; y la regla de «borrar no
aptas» (duplicadas + genéricas + fallidas + bajo umbral) solo existía
dentro de un botón destructivo.

**Dirección elegida.**
- Lista + panel, la misma anatomía que Correo: la fila lleva la
  evidencia (nombre en mono porque es un slug, descripción, estado, usos,
  origen profesor, aviso) y la cifra de confianza a la derecha con color
  por tramo y ✓ si pasó auditoría. El panel lleva todo lo demás en tres
  pestañas: Resumen, SKILL.md, Prueba.
- Filtros como chips con recuento; **«Necesitan atención»** hace visible
  el criterio del borrado en lote antes de borrar nada. «Sin auditar» es
  aviso suave (gris, sin icono); el ámbar queda para lo que está mal.
- Alta e importación viven en el panel («Nueva skill»), no en la cabecera.
- Los cinco ajustes de aprendizaje, en un popover de la cabecera junto al
  interruptor «Skills activas».
- Un solo momento de movimiento: el veredicto entra con `fs-rise`; la
  última línea del registro respira mientras corre. Nada más se mueve.
- Copia en el idioma del producto: «Publicar: entra en el índice que lee
  el agente», «Permitir una vez», «Corre en el servidor y tarda un rato».

**Revisión del plan contra los defaults**: sin tarjetas iguales en
rejilla, sin eyebrows, sin numeración decorativa (el procedimiento sí va
numerado porque es una secuencia), sin gradientes en texto, sin mono como
disfraz (solo en slug, SKILL.md y registro). El único acento de color es
la confianza y el estado, que son datos.

## Qué hay

`adapters/skills.ts` (los mismos endpoints que `skills.js`, más el juicio
del cliente: agrupación de duplicadas, clase de necesidad, «necesita
atención», orden, búsqueda), `screens/Skills.tsx` (lista, barra, banner
de auditoría, barra de selección, diálogos), `screens/skills/Detail.tsx`
(panel: resumen, SKILL.md con edición, prueba con aprobaciones y
veredicto; panel de alta e importación), `screens/skills.css`. Ruta
`/skills` en `routes.ts`, `app.py` y `AppShell`; comando `/skills`.

## Verificado en el 7001 (Claude in Chrome + Playwright del venv)

Lista de 6; filtros, confianza ≤ 85 (2), búsqueda; abrir → resumen; SKILL.md
leer, editar, guardar (ida y vuelta) y el índice se relee; prueba real con
`qwen3.5:27b`: arranque, registro en vivo, caja de aprobación («Permitir
una vez» → «Approved exact app_api action once; resuming test»), sigue
corriendo; publicar / despublicar con recuento; seleccionar varias →
aprobar en lote (2 publicadas, devueltas a borrador), diálogo de auditoría
(cancelado); nueva skill `studio-smoke-skill` → aparece seleccionada →
borrar con diálogo; importar `anthropics/skills@frontend-design` desde
GitHub → aparece y se abre → borrada; popover Aprendizaje: umbral 85→70→85
en `/api/prefs`. Móvil 420px: lista, panel con «Todas las skills» para
volver. Oscuro.

## Crítica (impeccable `critique`, a mano; sin detector)

- Fuerte: la fila dice en una línea lo que la anterior decía con seis
  píldoras; el aviso de atención explica el porqué en el panel con el
  grupo y la candidata a conservar.
- P1 corregido: en móvil todo el shell quedaba en 72px (regresión del
  lote P, no de este). Arreglado en `shell.css`; afecta a todas las
  pantallas.
- P2 corregido: al abrir otra skill se conservaba la pestaña anterior
  (Prueba) — ahora vuelve a Resumen. El deslizador del umbral disparaba
  un PUT por paso — ahora espera 350 ms.
- P2 corregido: con una aprobación pendiente el registro tapaba la
  pregunta — el registro se acorta a 22vh mientras espera.
- P3 abierto: la prueba usa el modelo por defecto (PENDIENTES 82).

## Trampas

- `/api/skills/{name}/markdown` devuelve 404 tanto si la skill no existe
  como si no tiene archivo (entradas del `skills.json` antiguo) como si
  el dueño no coincide. Studio trata el 404 como «sin archivo» y ofrece
  escribirlo desde los campos.
- `os.replace` en Windows falló una vez con «Access is denied» al guardar
  el SKILL.md justo después de leerlo desde fuera (bloqueo transitorio);
  al repetir, guardó. No es de la interfaz.
