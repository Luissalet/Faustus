# Estado del lote AL — La semana como horario, y decir lo que pasó

Fecha: 05-09-2026. Rama `feat/studio-ui`. Cierra las dos últimas filas
**Parcial** de PARIDAD (Calendario y Memoria) y las dos últimas notas
«(…: anterior)» que quedaban entre paréntesis. Después de este lote **no
queda ninguna fila Parcial ni Anterior** en `PARIDAD_FUNCIONAL.md`.

## Brief (banco de trabajo)

**Trabajo.** Cuatro cosas que comparten una idea: *enseñar lo que hay,
no que el usuario lo adivine*. Una semana que dice a qué hora; un carril
que dice que hay algo hoy; una limpieza que dice qué se llevó; y un turno
que dice adónde se fue la ventana de contexto.

**Lo que había.** La semana de Studio eran siete listas de fichas: te
decía *qué* había el miércoles, no *cuándo* —que es justo para lo que
existe una vista de semana—. La limpieza de la memoria devolvía «14 →
13» y te dejabas los ojos comparando. El ledger de contexto llegaba del
servidor (`src/context_ledger.py` lo calcula desde hace tiempo) y solo la
interfaz anterior lo pintaba. Y borrar un mensaje era un clic sin red.

**Dirección.** La aritmética separada del DOM (`lib/agenda.ts`), para que
lo que se puede probar se pruebe y el componente solo haga punteros. Los
avisos con el patrón que ya usan Notas y Biblioteca, para que se aprenda
una vez. Y decir la verdad cuando algo no se puede deshacer, en lugar de
prometer un «deshacer» que el servidor no puede cumplir.

## Lo que se hizo

### La semana es un horario — `lib/agenda.ts` + `screens/calendar/Week.tsx`

`lib/agenda.ts` es la parte pura: minutos a píxeles y vuelta, el trozo de
un evento que cae en un día (`spanOn`, que resuelve el que viene de ayer,
el que se va a mañana y el de duración cero), el reparto de solapes en
columnas (`layout`: dos reuniones a las diez son dos mitades, y una
columna que queda libre la reutiliza el siguiente), el imán a cuartos de
hora, los tres pasos del zoom y qué palabras busca la lupa.

`Week.tsx` pone los dedos encima: 24 filas de hora con la franja de todo
el día y la línea de ahora; **arrastrar un evento** lo mueve de hora y de
día; **arrastrar su borde inferior** lo alarga; **arrastrar en un hueco**
lo crea y abre el diálogo ya relleno. El fantasma se pinta mientras dura
el arrastre y el evento agarrado se dibuja en la columna sobre la que
está —el fallo que costó encontrar: como cada día solo conoce sus propios
eventos, al cruzar de columna el evento desaparecía a media mudanza; la
solución fue sacar `Tile` a su propio componente y pintar el agarrado en
la columna de destino—.

### El resto del Calendario

- **Buscar** por título, lugar, notas o calendario, con la lista de
  aciertos y salto al día.
- **Deshacer** el borrado, con el Toast de siempre y su reloj.
- **Recordármelo en Notas** desde el evento: crea la nota con la fecha.
- **Imagen de fondo** por evento, con el mismo sentinela `bg:<url>` que
  ya usan las notas.
- **Zoom** de la hora en tres pasos, recordado (`faustus_studio_cal_zoom`).
- De paso: bastantes cadenas que estaban escritas a pelo en castellano
  dentro del componente ahora pasan por `t()`.

### Insignias en el carril — `shell/badges.ts`

Notas y Calendario llevan un número con lo que hay hoy (vencidas o de
hoy; eventos de hoy). Se leen al abrir y cada dos minutos con la pestaña
visible. **El fallo**: la primera lectura estaba también detrás de
`document.visibilityState === 'visible'`, y en una pestaña recién
restaurada eso es `hidden`, así que la insignia no salía nunca. Ahora la
primera lectura va siempre y solo el intervalo mira la visibilidad.

### El informe de la limpieza — `screens/Memory.tsx`

«Ordenar con el modelo» compara la lista de antes con la de después y
abre un informe: **qué se quitó** (tachado), **qué se reescribió** (de →
a) y **qué se fundió** en un recuerdo nuevo. Si el servidor dice que ya
estaba limpia, lo dice y no abre nada.

### El desplegable de contexto — `adapters/chat.ts`, `model.ts`, `Transcript.tsx`

El evento `context_ledger` ya venía del agente con el reparto completo;
lo que faltaba era enseñarlo. Ahora el turno lleva un `<details>`
**Contexto** con la línea de cabecera («8,5k de 131,1k · 7 % de la
ventana») y, dentro, una fila por sección con su barra, sus tokens y su
porcentaje: prompt de sistema, esquemas de herramientas, instrucciones
del proyecto, skills, memorias, documentos, web, adjuntos, otro contexto
recuperado, resultados de herramientas, historial y tu mensaje. Debajo,
el aviso de recorte de las descripciones de herramientas y los consejos
que manda el servidor. Está plegado salvo que haya un aviso de verdad,
en cuyo caso se abre solo.

Las etiquetas las nombra el servidor en inglés y son una lista cerrada,
así que se traducen en `sectionLabel()` en lugar de mandarle el idioma al
backend.

### Borrar un mensaje ahora pregunta — `screens/Studio.tsx`

Al mirar el backend para el «deshacer» resultó que **`delete-messages` no
guarda versión**: al contrario que truncar, no hay nada que restaurar (la
nota de PARIDAD que decía «Undo/restaurar: anterior» estaba equivocada:
la anterior tampoco lo tenía, borraba al primer clic y avisaba después).
Como no se puede prometer un deshacer que el servidor no puede cumplir,
Studio **pregunta antes**, enseñando el principio del mensaje y diciendo
la consecuencia sin adornos, y avisa al terminar. El deshacer de verdad
queda apuntado como trabajo de backend (PENDIENTES 139).

## Pruebas

- `studio/checks/agenda.check.mjs` (nuevo) + `tests/test_studio_agenda_js.py`:
  minutos y reloj, el hueco de un día partido (de ayer, a mañana, todo el
  día, duración cero), el reparto en columnas (dos que solapan, uno pegado
  al siguiente que no solapa, una columna liberada que se reutiliza, un
  grupo posterior que no se estrecha por una multitud anterior), mover y
  alargar con sus topes e imán, el arrastre dibujado hacia arriba, el
  toque que no es arrastre, y qué encuentra la lupa.
- `tests\test_studio*.py`: **23 en verde** (eran 22).

## Verificado en el 7001

Horario de la semana con arrastre para crear (fantasma → diálogo
relleno), arrastre para mover entre días —persistido—, búsqueda, saltar
al día, recordatorio a Notas, borrado con deshacer, y el zoom recordado.
Insignias del carril (Notas 1, Calendario 1). Informe de la limpieza:
14 → 13 recuerdos, con «asdf» en la lista de lo que se fue. Desplegable
de contexto en un turno en vivo, con las siete secciones que tenía esa
conversación, tokens y porcentajes, y las etiquetas enteras (la primera
versión las cortaba con puntos suspensivos: la columna era de 9em y
«Other retrieved context» no cabía; ahora 13em). Borrado de un mensaje
con su pregunta previa, su vista del texto y su aviso al terminar.

## Lo que queda

En PENDIENTES 139–145. Lo importante: el desplegable de contexto es del
turno en vivo (al recargar no está, porque no se guarda con el mensaje) y
el servidor solo lo emite cuando hay novedad, no cada ronda.
