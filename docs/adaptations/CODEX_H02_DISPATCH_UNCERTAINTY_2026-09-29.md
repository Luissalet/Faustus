# H02/H04: exit 126 no demuestra ausencia de efectos

Corrección posterior al contrato de metadatos: `DockerSandboxProvider.exec`
trataba el código 126 como contenedor desaparecido y devolvía `executed=False`.
Docker también propaga ese código desde un programa que ya pudo escribir archivos
o enviar datos. El recibo podía afirmar `not_executed` y favorecer un reintento duplicado.

Ahora los fallos 126 y los mensajes de contenedor ausente observados **después del
dispatch** conservan incertidumbre: `executed=None`, `outcome_unknown=True` y
`reconcile_action=read_current_state_before_retry`. Se conserva la ruta Docker
intentada, stdout/stderr y el código; no hay fallback host ni reenvío automático.
`sandbox_exec` propaga esa incertidumbre al consumidor y H04 la normaliza como
`outcome_unknown`. Un probe previo que rechaza antes de enviar sigue pudiendo
afirmar que no se ejecutó.

Prueba nueva: el doble de Docker escribe un señuelo y devuelve 126; se comprueba
el efecto, una sola llamada y el estado incierto tanto en `PythonTool` como en
`normalize_tool_result`. No se simula una ejecución de Docker real como si hubiera
ocurrido: las pruebas Docker siguen omitidas por daemon no disponible.

```text
venv/Scripts/python.exe -m pytest tests/test_sandbox_policy_metadata.py tests/test_sandbox_provider.py tests/test_sandbox_exec.py tests/test_codex_h04_effect_results.py -q
```

Resultado: **50 correctas, 5 omitidas**. Fuente conceptual ya revisada:
[Codex ToolOrchestrator](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/orchestrator.rs)
y backlog H02/H04 del análisis local. No se ha cambiado la política de ejecución.
