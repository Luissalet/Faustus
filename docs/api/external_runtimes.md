# Runtimes externos (CMP-06) — `src/external_runtimes/`

Adaptadores READ-ONLY a runtimes de agentes/escritorio que Faustus NO
ejecuta él mismo. Hoy hay un único miembro: `herdr.py`, un cliente para un
runtime Herdr configurado externamente.

**Sin investigación externa, por instrucción del encargo del lote.**
Faustus nunca ha llamado a una instancia real de Herdr mientras se
construía esto: cada forma de abajo (`GET /version`, el listado de
sesiones/presencia) está inferida ESTRICTAMENTE de lo que describe
`INFORME_COMPARATIVO_V2.md §3.5`. El contrato de cable queda documentado
como **PENDIENTE DE VALIDAR contra un Herdr real** — `SUPPORTED_VERSIONS`
en `herdr.py` es deliberadamente una lista corta y explícita en vez de
"aceptar cualquier cosa que parsee", así que una respuesta de `/version`
no reconocida falla con un error tipado (`UnsupportedVersionError`) en vez
de que este adaptador adivine una forma que nunca ha visto de verdad.

## Solo lectura, permanentemente

Nada en este módulo envía una sesión, una entrada o un comando A Herdr —
`list_presence`/`negotiate_version` solo hacen GET. Si una ficha futura
añade un verbo que envíe algo, ESE verbo debe aplicar la misma disciplina
de `delivery` que ya usa `src/desktop_semantics/contracts.py::
ActionResult`: una petición que expira DESPUÉS de que el socket ya escribió
es `unknown`, nunca `not_delivered` — reintentar en silencio un envío que
puede haber llegado ya es exactamente el fallo que existe la convención
`unknown` del repo para evitar. `TransportError.delivery` ya distingue un
fallo confirmado-antes-de-enviar-ningún-byte (`not_delivered`) de un
timeout (`unknown`), como base lista para ese verbo futuro. Hoy ese verbo
NO existe, a propósito.

## Vocabulario (`herdr.py`)

- **`HerdrConfig(base_url, token)`** — `configured` es `bool(base_url)`.
- **`HerdrClient(config, *, transport=None, timeout=10.0)`** —
  `transport` es inyectable precisamente para que los tests no toquen la
  red (`tests/test_cmp06_herdr_adapter.py` usa un `FakeTransport` que
  nunca abre un socket).
  - `negotiate_version()` → `GET {base_url}/version`; acepta
    `{"version": "..."}` o un cuerpo string/number desnudo; cualquier otra
    forma, o una versión fuera de `SUPPORTED_VERSIONS`, es
    `UnsupportedVersionError` — nunca una adivinanza.
  - `list_presence()` → `GET {base_url}/sessions`; cada fila es una
    `Presence{session_id, label, state, certainty, signal_age_s, raw}`.
    `certainty ∈ {structured, heuristic}`: `structured` cuando la fila
    trae un timestamp real que se puede fechar (`last_seen_at`/
    `updated_at`), `heuristic` en cualquier otro caso — nunca presentadas
    como igual de fiables.
- **Errores tipados** (`error_class` = `external_runtimes.<motivo>`):
  `NotConfiguredError` (no hay `base_url` guardado),
  `UnsupportedVersionError`, `TransportError` (`delivery ∈ {not_delivered,
  unknown}`), y `HerdrError` genérico (p. ej. HTTP 4xx/5xx inesperado).

## Configuración (`settings: external_runtimes.herdr {base_url, token}`)

`src/settings.py` no tiene claves anidadas, así que esto vive bajo la
clave plana `external_runtimes_herdr` (`DEFAULT_SETTINGS["external_runtimes_herdr"]
= {"base_url": "", "token": ""}`). `token` se guarda cifrado con
`src/secret_storage.py` (el mismo almacén Fernet que ya usan las
contraseñas IMAP/SMTP, prefijo `enc:`) — nunca en claro, aunque el fichero
de settings sea donde vive. `herdr.load_config()`/`herdr.save_config(...)`
son el único punto de lectura/escritura; `save_config(base_url=...)` sin
`token` conserva el token ya guardado, `token=""` lo borra explícitamente.

## Rutas (`routes/external_runtimes_routes.py`)

```
GET  /api/external-runtimes/herdr/config     admin -- {base_url, configured, token_set} (nunca el token)
PUT  /api/external-runtimes/herdr/config     admin -- {base_url, token?}
GET  /api/external-runtimes/herdr/version    cualquier usuario -- negotiate_version()
GET  /api/external-runtimes/herdr/sessions   cualquier usuario -- {sessions: [...], count}
```

Convención de error: `{"error_class": ..., "detail": ...}` plano
(`routes/git_routes.py::_error`) — `NotConfiguredError`/
`UnsupportedVersionError` → 409, `TransportError`/`HerdrError` → 502 (con
`delivery` añadido cuando aplica).

## Adaptador de Studio (`studio/src/adapters/externalRuntimes.ts`)

Solo el adaptador tipado — GET/PUT config, GET version, GET sessions. El
cableado a una pantalla queda para un lote posterior (así lo dice la
ficha); este fichero no monta nada por sí solo.

## Tests

`tests/test_cmp06_herdr_adapter.py` — config (roundtrip, cifrado en
reposo, conservar/borrar token), negociación de versión (acepta conocida,
rechaza desconocida, timeout=`unknown`, fallo-antes-de-enviar=
`not_delivered`), `list_presence` (certeza structured/heuristic,
`signal_age_s`, forma envuelta `{"sessions": [...]}`, solo emite GET). Todo
contra un transporte falso — sin red, tal como exige el contrato del lote.
