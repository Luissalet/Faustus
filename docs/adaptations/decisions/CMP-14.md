# CMP-14 — Showcase: tres recorridos, datos mínimos, límites declarados

**Versión y recorrido:** base `95747d9`. `docs/adaptations/baseline.md`
(ADP-30) ya señalaba que `README.md`/`ACKNOWLEDGMENTS.md`/
`THIRD_PARTY_NOTICES.md` cubrían el branding y la atribución, pero que
`examples/showcase/`/`docs/showcase.md` no existían (`find -iname
"*showcase*"` → 0 antes de este lote) — el trabajo de "corregir metadatos
heredados" ya estaba hecho; faltaba construir la demostración en sí.

**Solución actual (antes de este cambio):** ningún fichero bajo
`docs/showcase.md` ni `examples/showcase/`. `src/doctor.py` no tenía
ningún check relacionado con demos/showcase.

**Solución de referencia (INFORME §3.13):** `docs/showcase.md` con los
tres recorridos (documento con revisión real, agente supervisado de
principio a fin, escritorio Windows semántico) enlazando pruebas,
configuración y límites; `examples/showcase/` con un proyecto de ejemplo
minúsculo; separar "conectar modelo existente", "instalar motor opcional",
"probar una demo"; sin afirmar instalador universal.

**Mecanismo concreto de la diferencia:** `docs/showcase.md` documenta cada
uno de los tres recorridos con: qué demuestra, qué ficheros lo implementan
(citando rutas reales del repo, incluidas las de otros lotes de esta misma
ola — W2-A1 para el documento, W2-C para el agente supervisado, este lote
para el escritorio), cómo probarlo offline con `examples/showcase/
sample-project/`, qué test lo cubre, y sus límites declarados
explícitamente (p. ej. "verificación física en Windows sigue pendiente").
Añade una sección aparte para CMP-06 (Herdr) aclarando que no es una cuarta
demo — no hay nada que mostrar sin una instancia Herdr real — y una
sección "tres cosas separadas" que distingue conectar-modelo-existente de
instalar-motor-opcional de probar-una-demo, cerrando con una frase
explícita de que Faustus NO es un instalador universal de un runtime de
terceros. `examples/showcase/sample-project/` contiene tres ficheros
mínimos y marcados como DEMO: `README.md` (nota de proyecto con una frase
repetida a propósito, fixture de "ocurrencias múltiples"),
`requirement.json` (un requisito fabricado, con `tests: []` a propósito
para tener una fila "sin probar" real en el recorrido de vecindario), y
`desktop-scenario.json` (dos controles con el mismo nombre, fixture de
"objetivo ambiguo se rechaza"). `src/doctor.py::_showcase_demo` (nuevo
check, registrado en `run()`) comprueba que `docs/showcase.md` y
`examples/showcase/sample-project/` existen y no están vacíos — `ok`/`fail`
según toque, nunca confundiendo "el fichero existe" con "el recorrido
funciona" (eso lo comprueban los tests de cada CMP, citados por nombre en
el propio `docs/showcase.md`).

**Cobertura:** **presente**, con dos límites declarados a propósito: (1)
los recorridos 1 y 2 citan ficheros/tests de W2-A1/W2-C que este lote NO
posee ni ejecuta — las rutas citadas son las que el contrato de la ola
asigna a esos lotes; si alguno cambia de nombre de fichero, este documento
queda desincronizado hasta que se corrija (riesgo declarado, no oculto);
(2) el check de `doctor.py` es deliberadamente superficial (existencia de
fichero, no ejecución del recorrido) — ampliarlo a ejecutar los tests
citados sería un cambio de alcance distinto (un `doctor` que ejecuta
pytest en vivo), no lo que pide esta ficha.

**Estado comparativo:** hipótesis de mejora — no hay una demo previa que
comparar, así que "paridad" o "ventaja externa" no aplican; es contenido
nuevo alineado con lo que pide la ficha.

**Decisión:** **conservar.** No hay nada que sustituir; se construye desde
cero tal como pide el criterio de aceptación.

**Pruebas ejecutadas:**
- `python3 -c "from src import doctor; print(doctor._showcase_demo().to_dict())"` → `state: ok`, ambos ficheros/directorios detectados.
- `python3 -m pytest tests/test_doctor.py tests/test_doctor_services.py tests/test_ops_doctor.py -q -p no:cacheprovider -W ignore` → 43 passed (el nuevo check no rompe ninguna aserción existente sobre `doctor.run()`; ninguna fija una lista cerrada de áreas/nombres).
- `python3 -c "import json; json.load(open('examples/showcase/sample-project/requirement.json')); json.load(open('examples/showcase/sample-project/desktop-scenario.json'))"` → ambos JSON parsean.
- No ejecutado: los tests de W2-A1 (`test_cmp01_doc_session_js.py`) y W2-C (`test_cmp05_attention.py`) citados en `docs/showcase.md` — pertenecen a otros lotes de esta misma ola; si no existen todavía en el momento en que se lee este documento, es una carrera de paralelismo esperada, no un error de este lote.
