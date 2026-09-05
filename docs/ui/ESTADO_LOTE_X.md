# Estado del lote X — Actividad con ficha y decisiones

Fecha: 05-09-2026. Rama `feat/studio-ui`.

## Brief (modo Operate)

**Trabajo.** Saber qué ha pasado y decidir lo que espera: una aprobación
no enseñada no es una puerta. Las filas ya tenían el vocabulario común
(UI-050); faltaba poder abrirlas y actuar.

**Lo que faltaba respecto a la anterior.** La pestaña Actividad del modal
de tareas expandía la fila con el resultado, y ofrecía abrir en chat,
informe, forzar, parar, repetir, copiar, vaciar caché; apilaba filas
idénticas; escondía las entregas «notification». Las aprobaciones se
decidían en la pill del chat. Inicio enviaba a la interfaz anterior.

**Dirección.** La misma anatomía lista + panel de Skills y
Automatizaciones: la fila sigue siendo la del carril de estados; el
panel es la ficha (aprobación: el plan entero y dos botones con una nota;
tarea: resultado en Markdown y sus acciones; render: sus campos y
cancelar). Una aprobación tiene un aviso ámbar que dice lo único que hay
que saber: no pasa nada hasta que decidas.

## Qué hay

`adapters/activity.ts` (fichas completas por tipo, apilado, `skipped`,
`decideApproval`, `cancelRender`, `openRunInChat` con la misma elección
de modelo que `_openResultInChat`, `reportUrl`), `screens/Activity.tsx`,
`activity.css` (bloque del lote), `Home.tsx` (las aprobaciones abren su
ficha; `plan.action` en vez de un rótulo genérico).

## Verificado en el 7001

11 filas apiladas (×3, ×5); ficha de una tarea con resultado; dos
aprobaciones creadas por `/api/approvals/request` → aparecen en Inicio
con su acción y abren su ficha en `/activity?status=accion&run=…`;
Aprobar con nota → «Aprobada», Denegar → «Denegada», `pending` vacío;
`/api/session` + `inject_messages` (lo que hace «Abrir en una
conversación») responden 200.

## Crítica

- P2 corregido: `skipped` se leía como «En cola» y ofrecía Detener.
- P3 abierto: las filas de render dependen de `require_admin`; sin admin
  la lista dice «no he podido leer renders» y sigue.
