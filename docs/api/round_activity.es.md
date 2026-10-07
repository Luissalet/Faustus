# Actividad del modelo por ronda

El desplegable de actividad del chat combina eventos de rondas del modelo, bloques de uso persistidos, eventos de workers, recibos del asesor y pasos de herramientas. Solo muestra campos informados por el backend o medidos por el cliente.

`round_info` puede incluir `model`, `input_tokens`, `output_tokens`, `cached_tokens`, `engine_timings`, `request_duration_ms`, `request_duration_source`, `finish_reason` y `usage_source`. Los campos del motor, como `prompt_ms`, `predicted_ms` y `cache_n`, permanecen ausentes si el runtime no los informó. `request_duration_ms` es la duración observada del stream en el cliente y se presenta por separado de las fases del motor.

Las rondas `round_info` de subagentes se conservan como `round_activity` en el informe del worker y se reenvían en el evento de ronda. Se guardan como máximo 100 rondas por worker. Los registros anteriores que no tengan este campo siguen mostrando el modelo agregado, estado, rondas, tokens y herramientas.

La interfaz no deduce una GPU ni un nodo a partir del nombre del modelo o del actor. La atribución de dispositivo solo es válida cuando un evento del runtime la proporciona; el esquema actual de rondas no afirma que las GPU se compartan ni identifica un nodo remoto.

La vista de actividad es observacional. No añade condiciones de cierre, pasos de aprobación ni cambios en la configuración del modelo.
