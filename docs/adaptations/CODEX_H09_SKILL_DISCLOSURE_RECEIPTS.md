# H09/H21 parcial: recibos de los fragmentos de skills incluidos

## Hallazgo visitado y alcance

`DisclosureResult` registraba nombres incluidos/omitidos, pero no qué versión ni
qué fragmento se había renderizado. Además, el caller de nivel 1 incrementaba
`record_use` y `remember_surfaced` antes del presupuesto: una skill seleccionada
y después omitida se contaba como utilizada y recibía atribución de resultados.

Ahora los niveles 0 y 1 generan un recibo por fragmento realmente incluido. Los
omitidos conservan su lista existente para diagnóstico; no tienen recibo de
contenido incluido. Nivel 1 adjunta los recibos al mensaje de skills ensamblado,
con metadatos de datos no confiables, y atribuye uso únicamente a `included`.
La selección, puntuación, permisos, confianza y presupuesto no cambian.

## Dos etapas explícitas

- `rendered`: identificador de skill, nivel, hash SHA-256 del fragmento exacto
  del renderer y cantidad de caracteres. Nivel 0 cubre la línea de esa skill,
  sin el encabezado de categoría; nivel 1 cubre su sección completa. Si se conocen,
  añade fuente categórica, versión y ruta relativa dentro del almacén de skills.
- `assembled`: SHA-256 del contenido completo del mensaje después de pasar por
  `untrusted_context_message`, y recibos renderizados de nivel 1. Este wrapper
  puede eliminar invisibles y escapar delimitadores; su hash no se confunde con
  el hash del fragmento anterior. Incluye el número de cuerpos omitidos.

No son pruebas de entrega al proveedor, lectura efectiva del modelo, ejecución
de la skill ni éxito. Compactación, saneamiento del protocolo o presupuesto de
una fase posterior pueden transformar/eliminar el mensaje. Los contadores ahora
significan cuerpo incluido en este ensamblado, no ejecución verificada.

Nivel 0 conserva versión/fuente/ruta relativa en `SkillsManager.index_for` y el
renderer devuelve sus recibos. Su caller live aún convierte el resultado a texto;
**no se transportan recibos individuales de nivel 0** al mensaje. El hash del
wrapper incluye el índice cuando existe, pero no inventa atribución individual.
Nivel 2 sigue pendiente. No se duplicaron descubrimiento ni revisión por digest.

## Privacidad y precisión

El recibo no contiene descripción, procedimiento, texto del fragmento, usuario ni
ruta absoluta. La ruta se calcula bajo la raíz conocida del almacén y se valida;
si falta origen verificable o está fuera, se omite. No se fabrican rutas a partir
del nombre. La fuente se restringe a categorías conocidas; URLs/credenciales
aportadas como fuente no se exportan. Identificadores fuera de un slug acotado se
representan por hash. La versión requiere formato semántico acotado.

La versión es la del objeto cargado: el loader existente puede aplicar `1.0.0`
por defecto. No demuestra declaración explícita en YAML, commit upstream,
aprobación, ni digest de todo el directorio. Nombres/rutas relativos siguen siendo
metadatos potencialmente sensibles y el hash no anonimiza contenido adivinable;
no se añade nuevo endpoint público ni log con los recibos.

La excepción de presupuesto de nivel 1 permanece: la primera skill puede exceder
el límite y entra completa; siguientes skills pueden omitirse. El recibo refleja
ese texto completo, sin atribuir una truncación que no ocurrió.

## Evidencia local

**50 pruebas pasadas**, 4,12 s. Incluyen recibos L0/L1, presupuesto y primera skill
sobredimensionada, hash exacto, omisión de fuentes/rutas inseguras, metadatos de
índice, aislamiento de owner, selección y ensamblado real: dropped no recibe
`record_use` ni `remember_surfaced`; el wrapper altera un delimitador/invisible y
su hash final coincide con el mensaje, distinto del fragmento previo.

`venv/Scripts/python.exe -m pytest tests/test_skill_disclosure_receipts.py
tests/test_skills_disclosure.py tests/test_skill_index_prompt_injection.py
tests/test_skill_selector.py tests/test_skill_index_toolset_gating.py
tests/test_lote4_global_skill_visibility.py tests/test_skills_manager_owner_isolation.py -q`

No se usaron datos reales, red ni proveedores. Referencias ya inspeccionadas:
[análisis H09/H21 y sección 22](CODEX_HARNESS_ANALISIS_2026-09-29.md),
[Codex fijado b1e72963](https://github.com/openai/codex/tree/b1e72963c3b71a9265a551e54beff078384efed9).
Implementación propia; no se repitió revisión upstream. H09/H21 permanecen parciales.
