# H17 parcial: anuncio de lookup_tools coherente con permisos

## Diferencia reproducida

`execute_lookup` recibía un contexto cuyo `tool_policy` bloqueaba `read_file`,
pero devolvía su schema, `promote=["read_file"]` y el hint «These tools are callable
this turn». El contexto de despacho ya transportaba la política; el handler no
la consultaba. El loop filtraba después la promoción con `audit_selection`,
pero no corregía el payload/texto del resultado mostrado al modelo.

Además, `audit_selection` podía retirar esa promoción y ofrecer de nuevo
`read_file` como fallback. Filtrar todo el pool hasta una lista vacía no bastaba:
el ranking interpretaba `pool=[]` como solicitud de reconstruir todo el catálogo.
La reproducción fue local, con una política y catálogo sintéticos, sin ejecutar
ninguna herramienta anunciada. No demuestra ejecución sin autorización: demuestra
anuncio falso y riesgo de llamadas rechazadas/reintentos.

## Cambio

`serve` y `serve_categories` aceptan `tool_policy` opcional; `execute_lookup`
transporta la política de su contexto a ambas rutas. Antes de crear filas, schemas,
conteos, ejemplos, output y promoción, consultan `is_permitted`/`permitted_names`,
la autoridad existente que ya usa la auditoría del loop. Se respetan nombres
equivalentes email, disabled y denylist no-admin. Las herramientas desconocidas
tampoco producen una fila que afirme que son ejecutables.

La auditoría filtra su pool de fallback mediante ese mismo predicado antes de
ordenar cercanía. `None` conserva selección del pool por defecto; una lista
explícitamente vacía permanece vacía. Las colecciones iterable de disabled se
materializan una vez en las rutas que las consultan dos veces, evitando que un
generador consumido pierda bloqueos durante el segundo filtro.

No cambian el índice, ranking principal, selector global ni schemas centrales.
Los nuevos parámetros son keyword-only y opcionales; callers antiguos mantienen
su firma válida. Sin política, las herramientas conocidas permitidas conservan
su schema y anuncio. Una respuesta vacía conserva el hint de búsqueda sin afirmar
que se cargaron herramientas ejecutables.

## Evidencia

16 casos nuevos ejecutan el handler registrado real y `ToolPolicy` real, con
MCP/índice/consulta admin sustituidos por fixtures sin servicios ni datos personales.
Verifican schema y catálogo, mezcla permitido/denegado, bloqueo total, política
ausente, aliases email en ambos sentidos por policy y disabled, categorías y
conteos, denylist no-admin, fallback permitido, pool vacío y disabled como generador.
Output JSON, payload anidado y promoción deben coincidir.

Validación: `venv/Scripts/python.exe -m pytest tests/test_lookup_policy_coherence.py
tests/test_tool_serve.py tests/test_tool_serve_bare_names.py
tests/acceptance/test_a08_tool_discovery.py
tests/acceptance/test_a09_tool_discovery_no_match.py -q`:
**42 correctas en 38,26 s**. El primer intento de la suite nueva tuvo una fixture
incorrecta que suponía `read_file` permitido para no-admin; se corrigió a
`ask_user` conforme a la denylist real, sin modificar esa autoridad.

## Procedencia y límites

Continuación H17 del [análisis fijado](CODEX_HARNESS_ANALISIS_2026-09-29.md),
con autoridad común H05 y fuente original ya documentada
[Codex registry.rs b1e72963](https://github.com/openai/codex/blob/b1e72963c3b71a9265a551e54beff078384efed9/codex-rs/core/src/tools/registry.rs).
Implementación propia sin repetir upstream ni el radar de proyectos.

Discovery no concede permisos: la revocación vigente y el dispatcher continúan
gobernando la ejecución. No se liga este resultado a un snapshot de paso ni se
certifica la estabilidad de MCP después del lookup. Se reutiliza el comportamiento
vigente del predicado, incluidos sus fallbacks ante errores; no se implementa una
nueva política fail-closed. El filtro del resultado de búsqueda ocurre después
del ranking/límite actuales, por lo que no repone candidatos inferiores cuando
los primeros estén bloqueados. Otros anuncios textuales y superficies Code Mode
no quedan certificados por este incremento. H17 permanece parcial.
