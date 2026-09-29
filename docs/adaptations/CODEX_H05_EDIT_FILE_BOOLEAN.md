# H05 parcial: replace_all booleano en edit_file

## Diferencia y cambio

El schema anuncia `replace_all` booleano opcional. El handler convertía su valor
con `bool(...)`: la cadena JSON `"false"` era verdadera. Sobre el texto temporal
`foo foo`, el reemplazo original producía `bar bar`, evitando el rechazo por dos
coincidencias que corresponde al modo de reemplazo único.

Ahora el campo ausente conserva `False`; un campo presente debe ser booleano
real. Los demás valores devuelven `error` y `exit_code=1` antes de procesar la ruta,
resolverla, leer, escribir o registrar historia. No se modifica `confirm_risky`
ni se añaden nuevas autorizaciones. El schema y sus wrappers nativos ya declaraban
el tipo correcto y permanecen iguales.

## Evidencia

La suite nueva ejecuta `TOOL_HANDLERS["edit_file"]` real, con archivos temporales,
resolución limitada al archivo sintético, doubt review desactivado mediante doble
y receptor de historia sintético. Cuenta resolución, lectura, escritura e historia.
La conversión nativa real `function_call_to_tool_block` conserva los valores JSON;
se comprueban tanto esa superficie como el contenido JSON del fence.

`"false"`, `"true"`, 0/1, `null`, listas y objetos dejan el archivo intacto y todos
los contadores en cero. Ausencia y `False` conservan rechazo de coincidencias
ambiguas; ambos permiten una coincidencia única. `True` reemplaza las dos.
Los éxitos escriben y registran exactamente una vez. Un control con el parser
anterior (`bool("false")`) y `_replace_text` original demuestra el resultado
`bar bar` sobre el contenido temporal, sin efectuar esa escritura.

Validación: `venv/Scripts/python.exe -m pytest tests/test_edit_file_boolean_contract.py
tests/test_edit_file.py tests/test_edit_base_revision.py tests/test_edit_preservation.py -q`:
**49 correctas, 2 omitidas en 2,55 s**. Las dos omisiones son pruebas históricas de
preservación que ya están marcadas como skip. La suite nueva sola tuvo
**30 correctas en 1,24 s**, antes del movimiento final de la validación al inicio
del procesamiento de campos; la suite conjunta valida ese código final.

## Evaluación sin cambio y límites

Se evaluaron `structural_search.max_results/context`: el handler convierte con
`int(...)` antes de `_catch`, pero `_direct_fallback` normaliza esas excepciones
en el dispatcher. El backend conserva comportamientos deliberados (0 resultados
usa 200, negativos se limitan a 1, contexto negativo se limita a 0). Rechazar
coerciones o cambiar rangos requiere otro alcance; no se modifican aquí.

Fuente ya fijada: [análisis H05/H06](CODEX_HARNESS_ANALISIS_2026-09-29.md),
[Codex registry.rs b1e72963](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/registry.rs).
Implementación propia del criterio schema/parser, sin repetir upstream ni el radar.
No se crea validador universal, binding de familia, autoridad única ni snapshot
de paso. H05/H06 permanecen parciales. Sin modelos, servicios ni datos personales.
