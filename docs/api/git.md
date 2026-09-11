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

El objeto repo de `GET /api/git/repos` (y de cualquier respuesta de
mutación) gana además dos campos, calculados por `routes/git_routes.py`
(`src/git_identities.py`, `src/agent_git_policy.py`):

```jsonc
{
  // ...los campos ya descritos arriba, más:
  "identity": {"id": "sshcfg:...", "label": "Luissalet", "github_login": "Luissalet"},
  // null si el remoto `origin` es https, o no coincide con ningún alias conocido
  "policy": {"effective": {"use_branch": false, "branch_prefix": "faustus/",
                          "commit": false, "commit_message_prefix": "faustus: ",
                          "push": false, "push_set_upstream": true},
            "overridden": false}
}
```

`identity` es solo un match por alias -- nunca prueba ssh (eso es
`POST /api/git/identities/{id}/probe`, aparte), así que listar repos nunca
se bloquea por la red; `github_login` es el que ya esté en caché, o `null`.

## Identidades SSH (`src/git_identities.py`)

Implementación: `src/git_identities.py`. En la máquina real de Luis,
"cuenta" no es un login -- es un alias de `~/.ssh/config` emparejado con una
clave (`Host Luissalet` → `id_ed25519_bookhoard`, junto a `github-lsaletec` y
`github-mlgpigeon`, los tres apuntando a `github.com`). Cambiar de cuenta en
un repo es reescribir el host del remoto a otro alias; ssh elige la clave
correcta solo con el `Host` que hace match -- no hay un "login" que gestionar
aparte.

Una identidad: `{id, label, ssh_host (alias, o null), hostname, identity_file,
git_user_name?, git_user_email?, github_login? (descubierto, o null),
source: "ssh_config"|"manual"}`.

- **`source: "ssh_config"`**: descubiertas, nunca escritas por esta API salvo
  con `write_ssh_config` (ver abajo). Dos formas:
  - un bloque `Host <alias>` de `~/.ssh/config` con un único alias sin
    comodín (`Host *` y cualquier alias con `*`/`?` se ignoran -- no hay un
    alias concreto al que reescribir un remoto);
  - una clave suelta `~/.ssh/*.pub` cuya clave privada no es ya el
    `IdentityFile` de ningún bloque -- sin alias (`ssh_host: null`), label =
    nombre del fichero.
- **`source: "manual"`**: las que un humano da de alta por
  `POST /api/git/identities`, guardadas en `DATA_DIR/git_identities.json`
  (owner-scoped) -- nunca la clave privada en sí, solo su ruta.
- **`github_login`**: descubierto, no configurado. `POST .../probe` corre
  `ssh -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new git@<alias>`
  (o `-i <identity_file> git@<hostname>` para una clave suelta sin alias) y
  parsea `"Hi <login>! You've successfully authenticated"` de stderr (rc 1
  es la respuesta normal de GitHub a `-T` -- no da shell a propósito).
  Timeout 8 s, cacheado 10 min por id; `GET /api/git/identities` nunca
  prueba la red, solo devuelve lo que ya esté en caché.

```
GET  /api/git/identities
     → {"identities": [...], "ssh_config_path": "/home/luis/.ssh/config", "ssh_dir": "/home/luis/.ssh"}

POST /api/git/identities
     {"label", "ssh_host"?, "hostname"="github.com", "identity_file",
      "git_user_name"?, "git_user_email"?, "write_ssh_config": false}
     → 201 {"identity": {...}} (source: "manual").
     400 git.identity_file_missing si identity_file no existe.
     Si `ssh_host` no está ya en ~/.ssh/config y `write_ssh_config: true`,
     AÑADE un bloque Host al final del fichero (nunca edita uno existente;
     hace una copia `.bak` primero).

DELETE /api/git/identities/{id}
     → {"ok": true}. 400 git.identity_not_manual si el id viene de
     ssh_config (descubierta, no se puede borrar); 404 si no existe.

POST /api/git/identities/{id}/probe
     → {"github_login": str|null, "ok": bool, "detail": str} -- siempre
     fuerza una prueba nueva (ignora la caché de lectura, la reescribe).

GET  /api/git/repos/{repo_id}/identity?remote=origin
     → {"active": identidad|null, "remote": "origin", "remote_url",
        "git_user": {"name", "email", "scope": "local"|"global"|"none"}}

PUT  /api/git/repos/{repo_id}/identity
     {"identity_id", "remote"="origin", "set_git_user": true}
     → reescribe la URL del remoto al alias de la identidad
     (`git@<alias>:<owner>/<repo>.git` -- entiende ssh scp-like, `ssh://` y
     `https://` de entrada) y, si `set_git_user` y la identidad trae
     name/email, hace `git config user.name`/`user.email` LOCAL del repo
     (nunca global). require_human. 400 `git.no_alias` si la identidad no
     tiene alias ssh (una clave suelta); 400 `git.unrecognized_url` si la
     URL del remoto no se pudo parsear; 400 `git.no_remote` si el repo no
     tiene ese remoto. Devuelve `{"remote", "remote_url_before",
     "remote_url_after", "git_user", "repo"}`.
```

## Crear / clonar repos

```
GET  /api/git/folders
     → {"folders": [{"path", "project_id", "project_name"}]} -- las carpetas
     enlazadas del owner (workspace + enlaces `folder`), de-duplicadas; el
     único sitio donde `POST /api/git/repos` puede escribir.

POST /api/git/repos
     {"mode": "init"|"clone", "parent_folder": str (una de /folders o un
      subdirectorio suyo), "name": str (solo [A-Za-z0-9._-]),
      "url"?: str (clone), "identity_id"?: str,
      "initial_commit": true (init: crea README.md + commit "Initial commit"),
      "default_branch": "main"}
     → 201 {"repo": <objeto repo>}.
```

`parent_folder` fuera de las carpetas enlazadas del owner: 403
`git.folder_not_allowed` (la misma garantía de confinamiento que ya da el
descubrimiento, antes de tocar el disco). `name` inválido: 400
`git.invalid_name`. El directorio destino ya existe y no está vacío: 409
`git.exists`. `mode: "clone"` sin `url`: 400 `git.invalid_name`. Clone con
`identity_id`: la URL se reescribe al alias de esa identidad antes de
clonar (mismo `rewrite_remote_alias` de arriba), timeout 120 s; fallo → 409
`git.clone_failed` con `stderr`. `mode: "init"` con `identity_id`: fija
`user.name`/`user.email` LOCALES del nuevo repo desde esa identidad antes
del commit inicial; sin identidad ni `user.*` global en el host, el commit
inicial falla con 409 `git.no_identity` (el repo queda creado, sin commit).

Crear rama: la ruta ya existente `POST .../branches {name, start_point?,
checkout}` (ver arriba) es también el endpoint que usa Studio para "crear
rama desde X" -- no hay una ruta nueva para esto.

## Política de git del agente (`src/agent_git_policy.py`)

Qué hace el agente por su cuenta dentro de un repo que está trabajando,
configurable: ¿usa una rama aparte?, ¿comitea lo que cambia?, ¿hace push? --
a preferencia del usuario, global o por repo.

Setting global: `git_agent_policy` en `src/settings.py DEFAULT_SETTINGS`
(no `agent_git_policy` -- ver el docstring de módulo de
`src/agent_git_policy.py` para el porqué: ese prefijo está reservado por el
formulario genérico de Settings → Agent Tools, que solo sabe describir
campos escalares, y esta es una política de cinco campos con su propia ruta
y su propia tarjeta en Studio). Override por repo en
`DATA_DIR/git_repo_policies.json` (owner+repo_id).

```jsonc
{"use_branch": false, "branch_prefix": "faustus/", "commit": false,
 "commit_message_prefix": "faustus: ", "push": false, "push_set_upstream": true}
```

```
GET/PUT /api/git/policy                 → política global. PUT admite un
                                          subconjunto de campos.
GET/PUT /api/git/repos/{repo_id}/policy → override por repo.
                                          {"policy": {"effective", "overridden",
                                          "override": {...}|null}}.
                                          PUT con {"inherit": true} quita el
                                          override (vuelve a heredar la global).
```

Semántica, cuando el workspace de un turno de agente está dentro de un repo
(`git rev-parse --show-toplevel` desde el workspace -- puede ser un
subdirectorio del repo):

- **`use_branch`**: antes de que el turno llegue al bucle del agente
  (`routes/chat_routes.py`, `chat_stream`'s `stream_with_save`, justo antes
  de `stream_agent_loop`), si HEAD no está ya en una rama `branch_prefix*`,
  crea `<branch_prefix><slug-de-la-sesión>-<yymmdd>` desde HEAD y hace
  checkout. Si el árbol está sucio, NO crea rama y emite `skipped: "dirty"`.
  Idempotente sin tabla de sesión que mantener: como el turno 1 deja HEAD en
  la rama del agente, el turno 2 la encuentra ya activa y no hace nada.
- **`commit`**: al final del turno (`_record_turn_side_effects`, que ya
  conoce los ficheros que tocó el harness), `git add` SOLO esos ficheros
  (nunca `-A` sobre todo el árbol) y `git commit -m "<prefix><resumen>"` con
  la identidad YA configurada del repo -- nunca una que este módulo
  inyecte; sin identidad → aviso `git.no_identity`, sin commit.
- **`push`**: solo tras un commit con éxito en la MISMA llamada, `git push`
  (con `-u origin <rama>` si no hay upstream y `push_set_upstream` está
  activo); si falla, aviso con `stderr`, nunca reintenta.
- Política toda en `false`: el repo no se toca. Nunca `push --force`
  (`git_panel.push` no tiene esa opción). Nunca comitea/hace push sobre un
  árbol con conflictos.

Cada acción emite un evento SSE `git_policy`:
`{"action": "branch"|"commit"|"push", "ok": bool, "branch"?, "sha"?,
"detail"?, "skipped"?}`, que Studio pinta como un chip discreto en el
transcript.
