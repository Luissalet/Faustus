# H16 parcial: incertidumbre de llamadas internas de Code Mode

Fallo reproducido: el guest recibía un resultado `outcome_unknown`, pero podía
terminar normalmente y `run_code` devolvía código cero sin conservar ese resultado.
El harness consideraba la llamada evidencia exitosa para cerrar una tarea.

El runner ahora inicia un recibo antes de esperar cada dispatch. Conserva identidad
de llamada, herramienta, estado normalizado e incertidumbre acotada. No almacena
argumentos ni cuerpos de resultados en esos recibos. Mantiene contadores exactos
por estado y como máximo 32 registros; los registros omitidos se cuentan y sus
estados siguen contribuyendo al agregado. Las identidades son las que recibe el
bridge, no una identidad deducida después de terminar.

Si queda una llamada desconocida o cancelada, el resultado externo declara
`outcome_unknown`; si solo quedan resultados parciales, declara `partial`.
Se mantiene el código de salida real del script. El `ok` del transporte al guest
sigue significando entrega del resultado, no éxito de la herramienta. Un error
conocido manejado por el código no convierte automáticamente el script en fallido.
Un intento posterior exitoso no reconcilia ni borra un resultado anterior incierto.

La cuota de tiempo puede cancelar un dispatch pendiente: el recibo ya iniciado
permanece desconocido y llega al resultado de terminación. La interrupción externa
del task del runner no devuelve normalmente un resultado; este tramo no añade un
ledger durable para ese caso ni modifica su semántica de cancelación.

## Pruebas y límites

Subprocesos reales con bridge simulado, sin LLM ni servicios externos. Casos de
éxito, parcial, desconocido, cancelado, fallo conocido manejado, incertidumbre fuera
del límite de registros seguida de éxito y timeout mientras se espera el dispatch.
El resultado externo se pasa por `TurnLedger` para comprobar que no verifica tareas
cuando quedan resultados inciertos.

Resultado: **23 pruebas aprobadas** en la selección de nuevos recibos, garantías
del host, pausa de aprobación y aceptaciones A10/A11 (políticas y cuotas).

Los recibos solo observan `tools.call`. Code Mode sigue siendo un proceso del host:
no proporciona aislamiento de filesystem/red, no rastrea operaciones directas del
script y no se activa por este cambio. Tampoco demuestra que los efectos parciales
se hayan deshecho, ni que un error conocido carezca de efectos. H16 sigue parcial.

Referencia ya visitada: [backlog H16](CODEX_HARNESS_ANALISIS_2026-09-29.md) y
[Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia, sin nueva revisión upstream.
