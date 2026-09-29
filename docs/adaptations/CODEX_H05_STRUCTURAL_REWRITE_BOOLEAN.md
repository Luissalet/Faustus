# H05 parcial: booleano de aplicación en structural_rewrite

## Diferencia reproducida

El schema nativo declara `apply` como booleano opcional. El executor usaba
`bool(args.get("apply"))`: el JSON `{"apply": "false"}` seleccionaba la rama
de escritura, al igual que `{"apply": 1}`. `null`, listas y objetos también
eran aceptados pese a incumplir el tipo declarado. La reproducción inicial
ejecutó las clases extraídas por AST con receptores sintéticos: ninguna escritura,
servicio, modelo ni base de datos personal.

## Cambio y evidencia

El handler registrado conserva `False` cuando falta `apply` y exige un booleano
real cuando está presente. Un valor inválido devuelve el error habitual del
handler (`error`, `exit_code=1`) antes de invocar cualquiera de los backends.
El schema existente ya era correcto; no cambia su forma ni sus wrappers OpenAI
`type=function`, `function`, `parameters`.

La nueva suite importa el handler real desde `TOOL_HANDLERS`, sustituye únicamente
los backends preview/apply por receptores sin efectos y comprueba dos superficies:
JSON del fence y conversión nativa con `function_call_to_tool_block`. Esta última
conserva los valores de argumentos, incluidos los inválidos, para que el parser
real los rechace. Cubre ausencia, `False`, `True`, cadenas `false`/`true`, 0/1,
`null`, listas y objetos. Los inválidos no invocan ningún backend y los válidos
invocan exactamente el esperado. También verifica el booleano opcional anunciado.

Validación: `venv/Scripts/python.exe -m pytest tests/test_structural_search.py
tests/test_structural_rewrite_boolean_contract.py -q`: **47 correctas en 2,28 s**.
La suite nueva sola: **25 correctas en 1,00 s**. Las pruebas existentes ejercitan
preview y aplicación sobre archivos temporales; no se modifican archivos de usuario.

## Procedencia y límites

Continuación propia del criterio schema/parser en
[análisis H05/H06 fijado](CODEX_HARNESS_ANALISIS_2026-09-29.md), con fuente original
[Codex registry.rs, b1e72963](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/registry.rs).
Se usa la referencia ya documentada, sin repetir upstream ni el radar de proyectos.

El cambio no introduce binding general, captura por paso, validación universal de
JSON Schema ni nueva autorización. Los demás campos del parser estructural no
quedan certificados por este incremento. H05/H06 permanecen parciales.
