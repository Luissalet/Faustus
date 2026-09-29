# H11 parcial: frames JSON de Code Mode con límite explícito

Fallo reproducido: imprimir 100 KB, dentro de una cuota de salida de 2 MB,
terminaba como error sin resultado. El `StreamReader` del subprocess tenía el
límite predeterminado de 64 KiB; el error de `readline()` se convertía en falso EOF.

El límite del stream se deriva ahora de la cuota efectiva: seis veces los bytes
permitidos de salida más 64 KiB de sobrecarga. El factor seis permite escapes JSON
como `\u0000`; la sobrecarga cubre los campos del frame. Sigue siendo una lectura
acotada, proporcional a la cuota configurada. No se incrementa la cuota de salida
del guest ni se permite una lectura ilimitada.

Un frame demasiado grande se identifica como `protocol_frame_limit`, se termina
el proceso y no se despacha una llamada contenida en ese frame incompleto. El
diagnóstico explícito prevalece sobre el timeout mientras se cancela el drenado;
no se sigue esperando stderr de un guest bloqueado escribiendo el frame.

Pruebas con subprocess real, sin LLM: resultado completo de 100 KB, expansión por
Unicode y caracteres de control, frame de argumentos de 2 MB rechazado antes del
dispatch con cuota pequeña, y cuota de salida original que continúa rechazando
101 bytes cuando permite 100. Se mantiene la batería de protocolo, resultados
inciertos, stderr, aprobación y cuotas existentes.
Resultado de la selección: **32 pruebas aprobadas**.

Límites: el mismo frame cap acota solicitudes del guest y su resultado final;
argumentos excepcionalmente grandes pueden requerir usar referencias en vez de
enviarlos por el bridge. Este límite acota el buffering del `StreamReader` de
forma proporcional a la cuota; no es un límite global de RSS ni aislamiento de
memoria del proceso. No cambia aislamiento del host, activación de Code Mode,
permisos ni observación de operaciones directas fuera de `tools.call`.

Fuente conceptual ya visitada: [H11](CODEX_HARNESS_ANALISIS_2026-09-29.md) y
[Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia, sin nueva revisión upstream.
