# CMP-06 — Adaptador Herdr, solo lectura

**Versión y recorrido:** base `95747d9`. `docs/adaptations/baseline.md`
(ADP-13) ya documentaba que "Herdr" no tenía ninguna mención en el código
del repo antes de este lote (`grep -ri Herdr` solo encontraba
`OBJETIVOS.md:410`, citándolo como candidato de investigación no
adoptado). Este lote crea el paquete `src/external_runtimes/` desde cero.

**Solución actual (antes de este cambio):** inexistente — ni cliente, ni
settings, ni ruta, ni adaptador de Studio para ningún runtime externo de
agentes/escritorio.

**Solución de referencia (INFORME §3.5):** adaptador Herdr SOLO LECTURA:
cliente HTTP a un runtime Herdr configurado (`settings:
external_runtimes.herdr {base_url, token}`), negociación de versión
(`GET /version` o el que documente Herdr; si desconocida → error explícito
`external_runtimes.unsupported_version`), lista de sesiones/presencia con
`certainty ∈ {structured, heuristic}` y `signal_age_s`; NUNCA reenvía
entradas; sin red en tests (fakes); un timeout tras enviar no se
interpreta como "no enviado".

**Mecanismo concreto de la diferencia:** `src/external_runtimes/herdr.py`
define `HerdrConfig`, `HerdrClient` (transporte inyectable — el productivo
usa `requests`, los tests inyectan un `FakeTransport` que nunca abre un
socket), `Presence` y errores tipados. `negotiate_version()` hace
`GET {base_url}/version`, acepta `{"version": ...}` o un cuerpo escalar, y
rechaza cualquier versión fuera de `SUPPORTED_VERSIONS = ("1",)` con
`UnsupportedVersionError` — deliberadamente una lista corta en vez de
"aceptar lo que sea", porque el contrato de `/version` en sí NO está
validado contra un Herdr real (ver más abajo). `list_presence()` hace
`GET {base_url}/sessions`, acepta una lista desnuda o `{"sessions": [...]}`
/`{"items": [...]}`, y marca `certainty="structured"` solo cuando la fila
trae un timestamp real (`last_seen_at`/`updated_at`) que permite calcular
`signal_age_s`; en cualquier otro caso, `"heuristic"`. `TransportError`
distingue `delivery="not_delivered"` (fallo confirmado antes de enviar
ningún byte) de `delivery="unknown"` (timeout — nunca "no enviado"),
mismo vocabulario que `src/desktop_semantics/contracts.py::ActionResult`;
documentado como la base que un futuro verbo de escritura DEBE reusar,
porque hoy este módulo no tiene ninguno (ni uno solo de sus métodos manda
nada a Herdr). `token` se guarda cifrado (`src/secret_storage.py`, prefijo
`enc:`) bajo la clave plana `external_runtimes_herdr` en `settings.py`
(que no soporta claves anidadas). `routes/external_runtimes_routes.py`
expone GET/PUT config (admin), GET version/sessions (cualquier usuario).

**Sin investigación externa, por instrucción del encargo:** el contrato de
cable (`/version`, `/sessions`, sus campos) está inferido ESTRICTAMENTE de
lo que dice `INFORME_COMPARATIVO_V2.md §3.5` — Faustus no ha llamado a un
Herdr real en ningún momento de este trabajo. Documentado explícitamente
como pendiente de validar en `docs/api/external_runtimes.md` y en el
docstring del propio módulo.

**Cobertura:** **presente, con contrato declarado pendiente de validar.**
El adaptador, la config cifrada, las rutas y el adaptador de Studio están
completos y probados contra fakes. Lo que NO está cubierto, por diseño y
por instrucción: (1) verificación contra un Herdr real (no había uno
disponible ni se buscó uno — "sin investigación externa"); (2) ningún
verbo de escritura (intencionalmente ausente — CMP-06 es "solo lectura");
(3) cableado de UI más allá del adaptador tipado (la ficha lo deja para un
lote posterior explícitamente).

**Estado comparativo:** hipótesis de mejora — el informe describe una
integración observada en otra herramienta; aquí se construye un contrato
PROPIO inspirado en esa descripción, sin código ni API real de Herdr de
por medio (nada que adaptar/licenciar: no hay repositorio de origen).
"Ventaja externa documentada" no aplica porque no se ejecutó nada externo
para medir nada; sigue pendiente de comparación real contra Herdr.

**Decisión:** **conservar como contrato propio, pendiente de validar.**
No hay nada previo que sustituir o integrar; se construye el módulo nuevo
tal como pide la ficha, con el límite declarado en vez de simular una
validación que no ocurrió.

**Pruebas ejecutadas:**
- `python3 -m pytest tests/test_cmp06_herdr_adapter.py -q -p no:cacheprovider -W ignore` → 13 passed (config roundtrip + cifrado en reposo, negociación de versión conocida/desconocida, timeout=`unknown` vs fallo-antes-de-enviar=`not_delivered`, certeza structured/heuristic, forma envuelta de `/sessions`, solo emite GET — sin red real en ningún caso).
- `python3 -m pytest tests/test_settings_store_shape.py tests/test_agent_settings_schema.py tests/test_settings_transactions.py -q -p no:cacheprovider -W ignore` → 51 passed (la clave nueva `external_runtimes_herdr` no rompe la paridad settings/schema: no matchea `SCHEMA_KEY_RE` porque no empieza por `agent_`/`browser_`/`desktop_`).
- `DATABASE_URL="sqlite:///:memory:" python3 -c "import app"` → arranca limpio con la ruta nueva registrada; `TestClient` contra `GET /api/external-runtimes/herdr/version` y `/config` devuelve 401 (auth), confirmando que la ruta existe y no da 404.
- No ejecutado: nada contra un Herdr real (declarado arriba, a propósito).
