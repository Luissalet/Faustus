# Proceso externo, reinicio y resultado

Referencias: [OpenHarness](https://github.com/autonomous-ai/openharness) y [Paperclip](https://github.com/paperclipai/paperclip). Implementación y pruebas propias; ninguno de los productos externos se ha instalado para este piloto.

La ruta existente `src/bg_jobs.py` ya lanza procesos separados y guarda ID, PID/fecha de creación, salida y código de terminación en disco. La prueba nueva inicia un intérprete Python que lanza una CLI sintética y termina; el proceso PowerShell continúa y otro consumidor recupera su resultado por el ID persistido. Se prueba tanto pwsh como Windows PowerShell, sin ventana visible, red o datos reales. El seguimiento pendiente se conserva y deja de aparecer al marcarlo atendido.

El ensayo reprodujo un fallo concreto: Windows PowerShell escribía `0` en UTF-16 con BOM, mientras Faustus lo leía como UTF-8. Un proceso correcto aparecía como fallido y su salida quedaba corrupta. La lectura de salida y exit code ahora reconoce BOM UTF-16 y UTF-8; también permite recuperar resultados antiguos ya escritos.

`done` sigue significando que terminó con código cero. El mensaje al agente habla de código de salida y no certifica que se cumplieron los objetivos del usuario. La prueba verifica únicamente su objetivo sintético: recuperar «pilot complete» y código cero.

Validación: `venv/Scripts/python.exe -m pytest -q tests/test_bg_jobs_restart_pilot.py tests/test_bg_jobs_store.py tests/test_bg_job_tools.py tests/test_l29_bg_jobs_ram_pressure.py` — 34 pruebas correctas tras reproducir y corregir el fallo.

Límites: no PTY interactiva, no recuperación de entrada estándar, no prueba de muerte abrupta del servidor completo ni cumplimiento de tareas por un agente CLI real. No añade una nueva abstracción de procesos. Esos entregables se solapan con H11/H24 del backlog Codex y deben usar esta evidencia como punto de partida.
