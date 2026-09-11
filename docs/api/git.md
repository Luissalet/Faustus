# Panel de control de versiones (OBJ-4) — `/api/git/*`

Backend del panel estilo "Source Control" de VS Code: descubre los
repositorios git que viven bajo las carpetas enlazadas de los proyectos del
usuario (incluidos subrepos anidados) y expone su estado, historial, ramas,
detalle de commit/diff, y las operaciones mutantes (checkout, crear rama,
fetch/pull/push/sync, stage/unstage/discard, commit).

Implementación: `src/git_panel.py` (descubrimiento y ejecución de `git`) y
`routes/git_routes.py` (las rutas). Sin dependencias externas — solo `git`
en `PATH`.

## Identidad de un repositorio

`repo_id = sha1(normcase(realpath(repo_root)))[:12]`, estable entre
llamadas. Es también el único mecanismo de resolución: un `repo_id` se
calcula SIEMPRE recorriendo las carpetas enlazadas del owner que hace la
llamada (`GET /api/git/repos`), así que un id que no salga de ese recorrido
— de otro owner, o inventado a partir de una ruta arbitraria — nunca
resuelve a nada, y cualquier ruta que lo reciba responde 404 sin necesidad de
una comprobación de propiedad aparte.

El descubrimiento recorre hasta profundidad 3 bajo cada carpeta enlazada
(`workspace` del proyecto, más cualquier enlace `folder` habilitado),
saltando `node_modules`, `.venv`, `venv`, `__pycache__`, `dist`, `build` y el
propio `.git`; un directorio es un repo si contiene `.git` (directorio o
fichero — worktree/submódulo). Los repos anidados se listan también, con
`parent_repo_id` apuntando al repo contenedor más cercano ya descubierto.
Tope global: 60 repos.

## Autenticación y alcance

- **Lectura** (`GET`): `require_user`.
- **Mutación** (`checkout`, `branches`, `fetch`, `pull`, `push`, `sync`,
  `stage`, `unstage`, `discard`, `commit`): `require_human` — el token
  interno del propio modelo no abre estas rutas, igual que
  `routes/approvals_routes.py`.
- Todo está acotado a las carpetas enlazadas de los proyectos del owner que
  hace la llamada; un repo fuera de ese alcance responde 404.

## Errores

- `git` ausente en el host: `503 {"detail": "...", "error_class": "dependency.missing"}`.
- Fallo de `git` sin una clasificación más específica:
  `400 {"detail": <stderr recortado a 2000 chars>, "error_class": "git.command_failed", "stderr": <mismo texto>}`.
- Casos específicos, todos `409`:
  - `git.dirty` — un `checkout` (o la creación de rama con `checkout: true`)
    pisaría cambios locales; el cuerpo incluye `"dirty": [rutas]`.
  - `git.diverged` — `pull --ff-only` no puede avanzar porque la rama local y
    su upstream divergieron; el cuerpo incluye `"ahead"`/`"behind"`.
  - `git.rejected` — el remoto rechazó el `push` (non-fast-forward, hook de
    secret scanning, ...); el cuerpo incluye `"stderr"`.
  - `git.no_identity` — el repo no tiene `user.name`/`user.email`
    configurados para el commit.
- `git.nothing_to_commit` (`400`) — mensaje vacío, o nada en stage y no es
  `amend`.

Cualquier respuesta de una ruta de mutación añade además `"repo"`: el mismo
objeto que devuelve `GET /api/git/repos/{repo_id}`, fresco, para que la UI
pueda refrescarse de una sola vez — también en las respuestas de error,
cuando el repo ya se pudo resolver.

`git` nunca se ejecuta con `shell=True`: cada llamada es un `argv` real con
`cwd` explícito, timeout (60 s por defecto; 120 s para `fetch`/`pull`/`push`/
`sync`), y las mismas flags de endurecimiento que ya usa
`routes/workspace_routes.py` (`-c core.fsmonitor=`, `-c diff.external=`,
`--no-ext-diff` en los comandos de diff) para que un `.git/config` de un
repo clonado o escrito por el agente no pueda ejecutar nada al abrir el
panel. Los commits usan siempre la identidad **configurada en el propio
repo** — nunca se inyecta `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME` — así que un
repo sin identidad configurada falla con `git.no_identity` en vez de
committear silenciosamente como "Faustus" o similar.

## Rutas de lectura

### `GET /api/git/repos?project_id=<opcional>`

Lista los repos bajo las carpetas enlazadas del owner (o de un único
proyecto, si se pasa `project_id`; un id que no exista o no sea del owner
responde 404).

```jsonc
{
  "repos": [{
    "id": "a1b2c3d4e5f6", "path": "/home/luis/code/faustus", "name": "faustus",
    "project_id": "p1", "project_name": "Faustus", "root_folder": "/home/luis/code/faustus",
    "parent_repo_id": null,
    "branch": "master", "detached": false, "head_sha": "abc123...",
    "upstream": "origin/master", "ahead": 0, "behind": 2,
    "dirty": {"staged": 1, "unstaged": 3, "untracked": 5},
    "user": {"name": "Luis", "email": "luis@example.com"},
    "remotes": [{"name": "origin", "fetch_url": "git@github.com:...", "push_url": "git@github.com:..."}]
  }],
  "git_version": "2.43.0"
}
```

### `GET /api/git/repos/{repo_id}`

El mismo objeto de arriba para un repo concreto, recalculado en el momento.

### `GET /api/git/repos/{repo_id}/status`

```jsonc
{
  "branch": "master", "detached": false, "ahead": 0, "behind": 2, "upstream": "origin/master",
  "staged": [{"path": "a.py", "status": "M"}],
  "unstaged": [{"path": "b.py", "status": "M"}],
  "untracked": [{"path": "c.py"}],
  "conflicts": [{"path": "d.py"}]
}
```

`status` de cada fichero en stage es `A|M|D|R|C` (con `"old_path"` cuando es
un rename/copy detectado); en working tree, `M|D`. Construido a partir de
`git status --porcelain=v2 -z`.

### `GET /api/git/repos/{repo_id}/log?limit=50&cursor=<sha>&ref=<branch|all>`

Paginado por cursor: `next_cursor` es el sha del último commit de la página
actual, o `null` cuando no hay más. `ref=all` recorre `--all`; por defecto,
`HEAD`. Orden `--date-order`.

```jsonc
{
  "commits": [{
    "sha": "abc123...", "short": "abc123",
    "parents": ["def456..."],
    "author": "Luis", "email": "luis@example.com", "date": "2026-09-10T12:00:00Z",
    "message": "Fix the thing", "body": "Longer explanation.\n",
    "refs": ["HEAD -> master", "origin/master", "tag: v1"]
  }],
  "next_cursor": "def456..."
}
```

### `GET /api/git/repos/{repo_id}/branches`

```jsonc
{
  "current": "master",
  "local": [{"name": "master", "sha": "abc123...", "upstream": "origin/master",
             "ahead": 0, "behind": 2, "is_current": true}],
  "remote": [{"name": "origin/dev", "sha": "789abc..."}]
}
```

### `GET /api/git/repos/{repo_id}/commits/{sha}`

Detalle de un commit, con la lista de ficheros que tocó
(`git diff-tree --numstat`/`--name-status -M -C`). `additions`/`deletions`
son `null` para un fichero binario.

### `GET /api/git/repos/{repo_id}/commits/{sha}/diff?path=<path>`

### `GET /api/git/repos/{repo_id}/diff?path=<path>&staged=0|1`

Ambas devuelven `{"path", "diff", "truncated": bool, "binary": bool}`; el
diff se recorta a 200 KB. `staged=0` compara working tree vs. index (o, para
un fichero sin trackear, contra el dispositivo nulo de la plataforma, para
que se muestre como una adición completa); `staged=1` compara index vs.
`HEAD`.

Un `path` que se salga del repo (absoluto, `..`) responde 404 en cualquiera
de las rutas que lo reciben.

## Rutas de mutación

Todas devuelven, además de lo indicado, `"repo"` (ver arriba).

- `POST .../checkout {"branch", "create": false, "start_point"?}` →
  `{"ok": true, "branch"}`, o 409 `git.dirty` con `"dirty": [rutas]`.
- `POST .../branches {"name", "start_point"?, "checkout": true}` →
  `{"ok": true, "branch"}` (mismo 409 `git.dirty` si `checkout` pisaría
  cambios).
- `POST .../fetch {"remote"?, "prune": false}` → `{"ok": true, "output"}`.
- `POST .../pull {"remote"?, "branch"?}` (`--ff-only`) → `{"ok": true, "output"}`,
  o 409 `git.diverged`.
- `POST .../push {"remote"?, "branch"?, "set_upstream": false, "force": false}`
  → `{"ok": true, "output"}`, o 409 `git.rejected`. `force: true` se rechaza
  con 400 antes de tocar git — no hay forma de hacer force-push por esta API.
- `POST .../sync` → hace `pull --ff-only` y, solo si tuvo éxito, `push`;
  `{"ok", "pull": {...}, "push": {...}|null, "repo"}`. Si el pull falla, la
  respuesta lleva el código/`error_class` del pull, con `"push": null` — el
  push nunca se intenta.
- `POST .../stage {"paths": [...]}` o `{"all": true}`.
- `POST .../unstage {"paths": [...]}` o `{"all": true}`.
- `POST .../discard {"paths": [...], "confirm": true}` — restaura ficheros
  trackeados al estado del index/`HEAD`. Destructivo: sin `"confirm": true`
  responde 400 sin tocar nada.
- `POST .../commit {"message", "amend": false}` → `{"ok", "sha", "short", "message"}`.
  400 `git.nothing_to_commit` si el mensaje está vacío o no hay nada en
  stage (y no es `amend`); 409 `git.no_identity` si el repo no tiene
  `user.name`/`user.email` configurados. Usa siempre la identidad del repo,
  nunca la sobrescribe.
