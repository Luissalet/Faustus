# Recuperación de generación en las Sparks

Faustus conserva la recuperación de servidores loopback y comunica los fallos
tipados `degenerate_output` de un endpoint LAN a Prometheus mediante el Hub local.
El cliente usa la configuración de familia existente y la herramienta nativa
`deploy_report_issue`. No lleva direcciones, modelos, puertos ni comandos de
reinicio fijados en código.

Prometheus comprueba que la URL y el modelo corresponden a una receta que gestiona.
El propietario confirma el síntoma, coordina la inactividad y el ciclo de vida de
esa receta. Faustus nunca termina un proceso LAN ni carga otro modelo como
alternativa. Un servidor o una versión de Prometheus sin la herramienta devuelve
estado desconocido; no se presenta como recuperado.

El error de streaming conserva `error_class=degenerate_output` y el motivo original.
La indicación de que se ha solicitado una comprobación significa que el envío se
ha programado; no garantiza que el propietario lo haya aceptado ni que la
recuperación haya terminado. El resultado del envío aparece en el historial ya
existente de `model_server_heal.status()`. El estado detallado del propietario se
consulta con `deploy_recovery_status`.

`model_server_auto_heal=false` sigue desactivando esta recuperación. Los fallos
ordinarios de lógica, el JSON con Markdown y `finish_reason=length` no se convierten
por esta ruta en fallos tipados de generación degenerada. El envío no vuelve a
ejecutar herramientas ni repite operaciones del usuario.

Los canarios del propietario comprueban estructura de código y un manifiesto
literal verificable. Su aprobación no demuestra la semántica del algoritmo ni la
estabilidad prolongada: los resultados incluyen el alcance verificado y sus
limitaciones. El monitor local de `/slots` sigue restringido a loopback; no genera
sondeos periódicos nuevos contra las Sparks desde Faustus.

Faustus conserva, si el propietario los envía, `verified_scope`,
`algorithm_semantics_verified` y `canaries_discriminatively_validated` también en
su historial de recuperación. No inventa estos campos para propietarios antiguos.
`waiting_coordination` y `checking` indican una comprobación pendiente. Una acción
`restarted` conserva el alcance declarado por el propietario; no certifica la
semántica del algoritmo ni presenta el modelo como plenamente sano.

Verificación del cliente: cobertura de URL, propietario ausente, envío pendiente,
deduplicación concurrente, errores de streaming conservados y ajuste existente de
autocuración. Las pruebas del ciclo local mantienen la sustitución de un proceso
sintético en un puerto privado. Las pruebas de producto no detienen el servicio
global de las Sparks.
