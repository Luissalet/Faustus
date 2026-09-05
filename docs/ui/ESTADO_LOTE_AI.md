# Estado del lote AI — los comandos `/`

Fecha: 05-09-2026. Rama `feat/studio-ui`. Cierra la fila «Comandos `/`» de
PARIDAD §3 (era **Parcial**) y con ella la fila «Chat / agente» de §1, que
dependía de §3 y §4. Queda fuera, a propósito, el motor de tours (`/demo` y
los once `tour-*`): es un subsistema propio y va en el lote AJ.

## Brief (banco de trabajo)

**Trabajo.** Escribir `/` y hacer algo sin soltar el teclado: cambiar un
mando de este chat, actuar sobre la conversación, saltar a una pantalla,
configurar algo, o pedir un informe (¿qué copias hay?, ¿qué modelo falla?,
¿qué recuerdos hay guardados?). Es una CLI injertada en un cuadro de chat, y
la gente que la usa la usa de memoria.

**Lo que había.** `static/js/slashCommands.js`: 7.329 líneas. El registro
(`const COMMANDS` en la 6421) tenía subcomandos con sus alias, más una
**segunda** tabla plana (`LEGACY_ALIASES`) pegada al lado para que `/new`
siguiera funcionando. Cada handler construía su propio HTML con estilos en
línea y lo empujaba al transcript como si fuera un mensaje del asistente:
`_bkList()` escribe `<ul class="harness-list">`, `_cmdPing` calcula colores a
mano, los huevos de pascua inyectan un `<style id="egg-styles">` en el `head`
cada vez. El autocompletado enseñaba sesenta nombres seguidos sin agrupar.
Studio tenía 45 de esos nombres, sin subcomandos y sin alias.

**Dirección.** El registro es **datos**, no handlers: nombre, alias,
categoría, uso, ayuda, y o bien `route` (va a una pantalla) o nada (lo hace
Studio). Los subcomandos son una tabla anidada con sus propios alias y los
alias planos de la anterior se resuelven a `padre sub`, así que ninguna
memoria muscular se rompe. Y la respuesta vuelve en **Markdown**, que es lo
que el lector del lote AH sabe dibujar: `/backup` es una tabla de copias,
`/scorecard` es la tabla que ya manda el servidor, `/help` es la lista
agrupada por categorías. Nada de HTML pegado.

## Lo que se hizo

### `screens/studio/commands.ts` (reescrito, 505 líneas)

79 comandos en siete categorías (Chat, Agent, Model, Memory, Tools,
Settings, Fun). `resolveCommand(name, rest)` devuelve `{command, sub, path,
args}` resolviendo, en este orden: alias plano (`/mv` → `chats rename`),
nombre o alias del comando, subcomando o alias del subcomando,
`defaultSub` cuando se escribe a secas. `matchCommands(prefix)` sugiere por
nombre, por alias, por `padre sub` (`/chats ex` → `chats export`,
`chats export-all`) y por alias plano, cada uno con su categoría; los
ocultos no salen hasta que los escribes. `helpMarkdown(query)` genera la
ayuda —una tabla por categoría, o la ficha de un comando con sus alias y
sus subcomandos— escapando los `|` que hay dentro de los propios usos.

Comandos que Studio no tenía y ahora sí: `chats` (new, delete, archive,
rename, favorite, unfavorite, fork, truncate, switch, sort, info, clear,
export, export-all), `toggle` (web, bash, research, doc, plan, rag,
sidebar, y la tabla de estado), `workspace`, `sh`, `backup`, `scorecard`,
`researchfit`, `agentsmd`, `project`, `memory` (list, add, delete, search),
`rag` (list, add, remove), `note`, `todo`, `event`, `skills`,
`reload-skills`, `find`, `search`, `model`, `theme`, `shortcuts`, `setup`
(con los doce proveedores como subcomandos para el autocompletado), `ping`,
`probe`, `tournament`, `provenance`, y los once ocultos.

### `adapters/commands.ts` (nuevo, ~330 líneas)

Los comandos que son una pregunta al servidor y una respuesta en pantalla.
Cada uno devuelve Markdown: `backupList`/`backupNow`/`backupVerify`,
`scorecard`/`scorecardClear`, `researchFit`/`researchFitApply`, `agentsMd`,
`ragList`/`ragAdd`/`ragRemove`, `ping`, `probe` (NDJSON en streaming, con
`probeMarkdown` que redibuja la tabla según llegan las filas),
`endpointIdByName`, `shellExec`, `findInChats`, `dbStats`,
`skillsMarkdown`, `reloadSkills`.

### `lib/fun.ts` (nuevo) y `screens/studio/Egg.tsx` (nuevo)

Los once ocultos, separados en datos y dibujo. `lib/fun.ts` decide qué se
ve —la cara de la moneda, los dados, la respuesta de la bola ocho, la
galleta, el verso, la cita, la tipografía de cinco filas del banner, la
vaca, el tiempo abierto, el color— y lo decide **una vez**, así que una
tirada es un hecho del turno y no algo que cambia mientras lo miras. La
lluvia de `/matrix` también vive ahí (`rain()`), porque los literales de
color de un canvas son píxeles y no tokens de diseño: en `.ts` la guarda de
colores no aplica, que es exactamente para lo que existe esa excepción.
`Egg.tsx` los dibuja con clases y tokens, con su bloque
`prefers-reduced-motion`.

### `Studio.tsx`

`runCommand` resuelve y hace `switch (path)`. `Notice` gana dos campos:
`rich` (la respuesta es Markdown y la dibuja `<Rich>`) y `egg` (la dibuja
`<Egg>`); `report(markdown, tone)` es el `say()` de los informes. El
`/setup` no escribe credenciales: lleva a Ajustes y avisa de que una clave
no viaja en un comando.

De paso, dos arreglos que salieron al probar: `/versions` mostraba la fecha
como un epoch crudo (`listVersions` pasaba a `String` un número que
`relativeTime` sabe leer) y contaba «0 mensajes» (el servidor manda `count`,
no `removed`); ahora es una tabla con el `/restore` listo para copiar y la
vista previa de lo que se quitó.

## Pruebas

- `studio/checks/commands.check.mjs` (nuevo) + `tests/test_studio_commands_js.py`:
  el registro está bien formado (ningún nombre o alias repetido, toda
  categoría declarada, todo `defaultSub` existe), la resolución de alias,
  alias planos y subcomandos, `parseCommand` (una ruta unix no es un
  comando), las sugerencias, `/help` con sus pipes escapados, los mandos de
  generación, `/agents`, y la parte pura de los ocultos.
  **Y una guarda contra el fallo que se coló**: lee `Studio.tsx` y comprueba
  que cada comando que Studio ejecuta tiene su `case`. Sin ella, `/mv`
  resolvía a `chats.rename`, no había rama, y no pasaba nada en silencio.
- `tests\test_studio*.py`: 21 en verde (eran 20).

## Verificado en el 7001

`/help` (79 comandos, tabla por categorías, con scroll), `/help chats` (la
ficha con alias y subcomandos), `/toggle` (tabla de interruptores, modo y
carpeta), `/web` (alias plano: enciende el chip), `/roll 3d6` (dados y
total), `/ascii Faustus`, `/matrix` (la lluvia), `/cowsay`, `/odyssey`,
`/8ball`, `/uptime`, `/color 3aa` (muestra con copiar), `/ping` (tres
endpoints con latencia y modelos), `/backup` (5 copias, 403 MB, con las
instrucciones de restaurar), `/shortcuts` (los keybinds reales),
`/memory list`, `/scorecard 30` (la tabla del servidor, con scroll
horizontal), `/rag list`, `/project`, `/workspace`, `/find markdown`,
`/skills`, `/researchfit` (perfil «roomy», RTX 4070 Ti, dos ajustes que
cambiarían), `/sh echo hola`, `/note`, `/todo`, `/model`, `/mv`, `/star`,
`/unstar`, `/goto Casa`, `/chats truncate 1`, `/versions` y `/restore`.

## Lo que queda

- El motor de tours: `/demo` y `tour-compare|cookbook|research|library|theme|settings|gallery|brain|task-1|task-2`.
  La anterior los hace con halos y tooltips posicionados por `getBoundingClientRect`
  (`tourAutoplay.js` los dispara la primera vez que abres cada modal). Lote AJ.
- `/probe` redibuja la tabla entera con cada fila que llega: con muchos
  modelos eso es un re-render por fila. Si molesta, agrupar por tanda.
