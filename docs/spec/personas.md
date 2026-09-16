# Agent personas (`src/personas/`)

R4, ola Reach — inspirado en la investigación de `msitarzewski/agency-agents`
(`agent_repos.md` §5: ~307 personas Markdown por división). Faustus ya tenía
el sitio correcto para "quién es este agente": `src/agent_defs.py` (AGENT.md
con `mode`/`tools`/`permission`/perfiles) y `src/behavior_modes.py` ("cómo
habla", ortogonal). Una persona ENCAJA en `agent_defs` — no es un tercer
selector — vía el nuevo campo `persona: <slug>` en el frontmatter de un
AGENT.md.

## Qué hay

- `src/personas/loader.py` — `parse(text)` → `Persona` (frontmatter YAML:
  `name, slug, division, summary, tags, tools_hint, language`; cuerpo =
  identidad + misión + flujo + entregables + métricas). Reutiliza el MISMO
  parser de frontmatter que `AGENT.md`/`SKILL.md`
  (`services/memory/skill_format.py`) — no hay un segundo dialecto.
- `src/personas/builtin/*.md` — **16 personas**, ≤350 palabras cada una:
  `backend-python, frontend-react, code-reviewer, security-auditor,
  test-engineer, devops-windows, data-analyst, technical-writer` (inglés),
  `editor-literario, guionista, world-builder` (castellano de España),
  `research-analyst, product-manager, ux-designer, ml-engineer,
  prompt-engineer` (inglés).
- `src/personas/registry.py` — builtins + `DATA_DIR/personas/*.md` del
  usuario, que sobreescribe un builtin por slug (mismo orden de precedencia
  que `agent_defs`: built-in < user). `render_system_block(slug)` da el
  bloque listo para anteponer a un `system_prompt`; devuelve `""` para un
  slug desconocido en vez de lanzar — una referencia obsoleta cuesta un
  párrafo del prompt, nunca la definición del agente entera.
- `routes/persona_routes.py` — `GET /api/personas`, `GET /api/personas/{slug}`,
  `GET /api/personas/{slug}/render`, `PUT /api/personas/{slug}` (usuario),
  `DELETE /api/personas/{slug}` (borra solo el override del usuario; un
  builtin nunca se borra, solo se sobreescribe). Gate `require_user`, igual
  que `board_routes.py` (dato propio del usuario, sin efecto externo).

## El enganche en `agent_defs.py`

Cambio ADITIVO, con test (`tests/test_agent_defs_extended.py` ajustado a la
nueva clave):

- `persona` añadido a `FRONTMATTER_KEYS` y a `AgentDef` (campo `persona: str`).
- `parse()` valida que sea un slug usable (no verifica que EXISTA — igual que
  los `*_profile` ya existentes, para no acoplar la carga de un AGENT.md a
  que el catálogo de personas esté disponible ese instante).
- `resolve_task()` antepone `render_system_block(persona)` al
  `system_prompt` del agente, ANTES de su propio prompt — la persona nunca
  sustituye las reglas de tools/paths/delegación, que siguen siendo
  enteramente de `agent_defs`.
- `_materialise()` (herencia `extends`): `persona` sigue la regla de
  "REPLACE donde el hijo lo dijo" de cualquier otro escalar.

## Tests

`tests/test_r4_personas.py` (18 tests: parseo válido/inválido, los 16
builtins cargan sin error, presupuesto de 350 palabras, override de
usuario, borrado, `render_system_block`) y
`tests/test_r4_persona_routes.py` (8 tests HTTP).

## Qué queda

- El picker de personas en el Studio (UI) no se tocó en este lote — la API
  ya está lista para que lo consuma.
- No hay verificación de que `tools_hint` nombre tools reales — es
  deliberado (ver docstring de `loader.py`): es una pista para el lector,
  no una concesión de permiso.
