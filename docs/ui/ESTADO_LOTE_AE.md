# Estado del lote AE — Chat en grupo y personajes completos

Fecha: 05-09-2026. Rama `feat/studio-ui`. Cierra la fila «Deep Research,
Group chat, Persona, Compare desde el compositor» de PARIDAD §3. Nada de
`group.js` ni de la pestaña Persona/Grupo del modal de presets de la
anterior sobrevive; la etiqueta del grupo (el texto que hace que los
modelos se hablen) está en `adapters/group.ts`.

## Brief (banco de trabajo)

**Trabajo.** Sentar a varios modelos a una mesa —cada uno como es o con
un personaje— y hablar con ellos y verlos hablar entre sí, por turnos o
todos a la vez; guardar mesas que funcionan; y, aparte, escribir
personajes bien (con ayuda de la IA, con su temperatura) y tener un
personaje improvisado con texto antes/después de cada mensaje.

**Lo que hacía mal la anterior.** La mesa vivía en una pestaña del modal
de presets, con selects sin nombre, el botón «Start» compartido con
«guardar preset», el modo en un botón que decía «Sequential», y las
respuestas pintadas a mano dentro del chat normal con `dataset.raw`. El
personaje: un `+ New`, un `Expand` sin explicación y tres sliders.

**Dirección.** Una pantalla propia `/group` en dos estados: la mesa
(filas participante: avatar, modelo por paleta, personaje, quitar; añadir;
turnos como segmentado con una frase que explica la diferencia; grupos
guardados como chips; Guardar como grupo; Empezar) y la conversación
(asientos arriba, burbujas con el color del participante, la tuya a la
derecha, compositor abajo, Parar). El padre `[GRP]` es una sesión normal:
aparece en Studio con icono de grupo, abrirlo vuelve a la mesa, y su
transcripción en Studio lleva el nombre de quien habla. Los personajes
siguen en la paleta de presets del compositor, con los conocimientos que
faltaban: Expandir con IA, temperatura y máx. tokens en la plantilla, y
el personaje propio como formulario aparte con prefijo/sufijo y Activo.

**Revisión contra los defaults.** Nada solo por icono salvo quitar;
selects con `aria-label`; nombres duplicados numerados en vez de dos
voces iguales; estados vacíos con qué hacer («Say something to the
table»); errores del servidor tal cual; colores por token (siete tonos
de avatar compartidos con Correo, ahora globales `.fs-avatar`).

## Qué entra

- `adapters/group.ts`: `startGroup` (padre + una sesión por participante
  con prompt de sistema y etiqueta), `recordUser`, `recordReply`
  (`inject_messages` al padre con `group_model` y a los demás como
  `[Nombre]: …`), estado por padre en `localStorage`, grupos guardados
  (`/api/presets/groups`), `isGroupSessionName` / `stripGroupPrefix`.
- `adapters/presets.ts`: `saveTemplate` con temperatura y máx. tokens,
  `expandPrompt`, `getCustomPersona` / `saveCustomPersona`.
- `screens/group/Group.tsx` + `group.css`; `PresetPalette.tsx` con el
  formulario ampliado y el personaje propio; `palette.css` (`__knob`).
- Studio: `Turn.speaker` desde `metadata.group_model`, etiqueta en la
  transcripción, icono y nombre sin `[GRP]` en la lista, abrir un padre
  conocido lleva a `/group?s=`.
- Global: `.fs-avatar` en `components.css` (antes `.fs-mail__avatar`),
  `.fs-seg button { white-space: nowrap }`.
- Rutas: `/group` en `app.py`, `SERVER_ROUTES`, `TOOLS` (Users), `/group`.
- i18n: 52 filas.

## Verificado en el navegador (7001)

- Mesa con dos qwen3.5:9b «como él mismo», por turnos → Empezar crea el
  padre y navega a `?s=<padre>`; «Presentaos en una frase…» → dos
  burbujas, la segunda responde a la primera («tu enfoque en la
  directitud…»); parar/enviar alternan.
- «Transcripción en Studio» → el padre en Studio con las dos respuestas
  etiquetadas «QWEN3.5:9B», icono de grupo en la lista y título sin
  `[GRP]`.
- Paleta de presets: «No preset», «New preset or persona…», «Custom
  persona (prefix, suffix, sampling)…» → formulario con nombre, prompt,
  Expandir con IA, temperatura 1.0, máx. tokens sin límite, prefijo,
  sufijo y Activo.

## Sin verificar

- PENDIENTES 114.
