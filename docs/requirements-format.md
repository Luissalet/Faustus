# Formato de requisitos (ADP-18)

Dos formas de tener requisitos en Faustus: la base de datos
(`DATA_DIR/requirements.sqlite3`, ver `docs/api/requirements.md`) y un
sidecar opcional escrito a mano en el workspace del proyecto,
`.faustus/requirements.yaml`. El sidecar es una PROYECCIÓN de solo lectura
(`GET /api/projects/{id}/requirements/sidecar`) — no se importa
automáticamente a la base de datos.

## Esquema del requisito

| Campo | Tipo | Notas |
|---|---|---|
| `key` | `REQ-N` | Secuencial por proyecto, asignado por la base de datos al crear — en el sidecar es solo una referencia legible, no autoritativa |
| `title` | string | Obligatorio |
| `text` | string | Descripción larga (opcional) |
| `source` | `doc` \| `issue` \| `url` \| `human` | De dónde viene |
| `status` | `proposed` \| `accepted` \| `rejected` \| `superseded` | Un requisito propuesto por el modelo SIEMPRE nace `proposed`; solo un humano lo mueve a `accepted`/`rejected` |
| `proposed_by` | `human` \| `model` | Quién lo originó |
| `acceptance` | lista de strings | Un criterio por elemento |

## `.faustus/requirements.yaml`

Subconjunto deliberadamente pequeño de YAML — no una implementación general
(este repo no declara `pyyaml` como dependencia en
`requirements*.txt`/`pyproject.toml`, así que no se añade una librería
nueva solo para este sidecar; `src/requirements/store.py::parse_sidecar`
es un escáner propio, tolerante, hecho a mano para exactamente esta forma):

```yaml
requirements:
  - key: REQ-1
    title: Users can reset their password
    source: doc
    status: accepted
    text: |
      A user must be able to request a reset link by email.
      The link expires after one hour.
    acceptance:
      - Reset link expires after 1 hour
      - Old password stops working once reset completes
  - key: REQ-2
    title: Sessions expire after 30 minutes idle
    acceptance:
      - No API call succeeds with an expired session token
```

Reglas del parser:

- Indentación con espacios únicamente (los tabs se rechazan con un error
  explícito, nunca se adivinan).
- `text: |` es un escalar de bloque literal — todo lo indentado más que el
  campo, hasta la línea que vuelve a la indentación del campo o menos.
- `acceptance:` sin valor en la misma línea espera una lista anidada de
  `- elemento`.
- Cualquier línea que no encaje en ninguna de las dos formas
  (`- item` / `campo: valor`) se registra en `errors` y se SALTA — un
  fichero mal formado degrada a menos elementos, nunca lanza una excepción
  que rompa la ruta que lo lee.
- Comentarios (`#`) y líneas en blanco se ignoran.

## `@implements REQ-N` en comentarios de código

`src/requirements/evidence.py::scan_implements_comments(text)` detecta
`@implements REQ-N` en cualquier comentario de cualquier lenguaje (escaneo
de texto plano, sin AST). El resultado es candidato a un enlace
`kind='implements'` — **nunca** cuenta como `verified` en la matriz de
cobertura (`docs/api/requirements.md#matriz-de-cobertura`): solo un enlace
`kind='evidences'` explícito, registrado contra la revisión actual del
requisito, verifica algo.
