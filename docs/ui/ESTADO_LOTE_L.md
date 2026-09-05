# Estado del lote L - Calendario como pantalla

Fecha: 05-09-2026. Rama `feat/studio-ui`. Verificado en el 7001.

## Qué entra

- **Calendario** (`/calendar`, chunk propio de 28 KB; `adapters/calendar.ts`
  sobre `/api/calendar` sin cambios de forma; los eventos llegan ya
  expandidos por el servidor, una fila por repetición, `uid::sello`):
  - Vistas: **mes** (rejilla de 6 semanas, lunes primero, día de hoy
    marcado, panel del día seleccionado con horas, lugar y repetición),
    **semana** (siete columnas con los eventos del día), **agenda** (60
    días a partir del cursor) y **año** (doce meses con densidad de eventos;
    clic en un día lleva al mes). Vista recordada (`faustus_studio_calendar_view`).
  - Evento: título, todo el día (fechas sin hora), inicio y fin, repetición
    (RRULE: día, semana, laborables, mes, año; una regla distinta se
    conserva), lugar, calendario (solo al crear: el servidor no lo cambia),
    color propio o «el del calendario», notas. Borrar: en una repetición,
    «Solo esta vez» (`scope=occurrence`) o «Toda la serie».
  - Añadir en tus palabras: `/quick-parse` con la zona horaria del
    navegador; crea el evento, salta a su día y avisa si la confianza es
    baja. Si el modelo no responde o no lo entiende, abre el formulario con
    el texto como título.
  - Filtros por calendario (chips con su color, apagados = ocultos).
  - Calendarios: nombre y color en línea (guardan al salir del campo),
    nuevo, borrar (con sus eventos, confirmación), exportar .ics
    (`/export/{id}`), importar .ics (`/import`, opcionalmente en un
    calendario concreto).
  - Sincronizar CalDAV (`/sync?direction=pull`) si hay algún calendario de
    esa fuente; las cuentas se configuran en la anterior.
  - Teclado: ←/→ mueven, T hoy, N nuevo, m/w/a/y cambian de vista. Doble
    clic en un día (mes) o en la cabecera (semana) crea ahí.
  - `?d=YYYY-MM-DD` abre en esa fecha.
- Servidor: `/api/calendar/quick-parse` exento del timeout de 45 s (una
  llamada al modelo de utilidad; con `qwen3.5:9b` en local tarda más y la
  anterior también recibía 504).
- `/calendar`, atajo `open_calendar` y «Herramientas» apuntan a la pantalla.

## Verificado en el navegador

- Mes de septiembre de 2026 con hoy marcado; panel «Sábado, 5 de
  septiembre · Nada ese día».
- Doble clic en el 8 → formulario con 09:00–10:00 → «Dentista» en «Clínica
  Sol» → tarjeta azul en el 8 y en el panel del día con hora y lugar.
- Añadir en tus palabras «Comida con Marta el viernes a las 14 en La Tasca»
  (ver «Sin verificar»).

## Sin verificar

- El resultado del `quick-parse` con el modelo local: en el 7001 el
  servidor responde `{"ok": false, "error": "LLM call failed: 504 … timed
  out after 3 attempts"}` (Ollama con `qwen3.5:9b` como modelo de
  utilidad no contesta a tiempo). La pantalla cae al formulario con el
  texto como título, que es lo verificado. Con un modelo de utilidad
  pequeño debería crear el evento directamente.
- Importar .ics por el navegador (el puente no adjunta ficheros de fuera de
  la sesión) y la sincronización CalDAV (no hay cuenta configurada).
- Semana, agenda y año con datos (renderizan; sin eventos suficientes para
  juzgar la densidad del año).
