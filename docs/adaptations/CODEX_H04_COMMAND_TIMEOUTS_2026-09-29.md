# H04: conservar incertidumbre tras timeout de comandos

Estado: **implementado y probado en los productores de resultados**.

El normalizador H04 reconoce `timed_out=True`, pero varias rutas de comandos
perdían esa evidencia al devolver solo un error y código 124. Así un timeout con
posibles efectos se convertía en un fallo ordinario.

Se propaga ahora `timed_out=True` desde los límites reales de Bash, Python,
PowerShell, Bash mediante tmux y sandbox Docker efímero/persistente. La bandera
procede del ejecutor; **no se infiere de exit_code=124**, porque un programa puede
devolver ese número tras terminar normalmente.

Las pruebas recorren resultado del productor → `normalize_tool_result` y
comprueban `outcome_unknown` con reconciliación antes de repetir. Incluyen timeout
duro, inactividad, tmux, sandbox persistente y controles de salida voluntaria 124.
No se añaden reintentos ni cambios de política, y no se afirma ausencia de efectos
al matar un proceso.

```text
venv/Scripts/python.exe -m pytest tests/test_command_timeout_outcomes.py tests/test_subprocess_hardening.py tests/test_sandbox_policy_metadata.py tests/test_sandbox_exec.py tests/test_sandbox_provider.py tests/test_codex_h04_effect_results.py -q
```

Resultado: **71 correctas, 7 omitidas** (dependencias/plataforma de pruebas existentes).
Las pruebas nuevas usan dobles de procesos, no inician procesos que queden vivos.
Las pruebas Docker reales siguen pendientes del daemon; este cambio no las sustituye.

Fuente conceptual, ya revisada en el backlog H04:
[Codex ToolOrchestrator](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/orchestrator.rs).
