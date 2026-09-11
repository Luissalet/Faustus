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

Cuando el mismo repo (misma ruta real, `realpath`+`normcase`) aparece bajo
más de un proyecto — dos proyectos enlazando la misma carpeta — sale UNA sola
vez: `project_id`/`project_name` se quedan con el PRIMER proyecto que lo
enlazó (compatibilidad con clientes que solo leen esos dos campos), y
`"projects": [{"id","name"}, ...]` lleva uno por cada proyecto que lo enlaza
(`src/git_panel.py::_dedupe_repos`).

### Rendimiento (Lote 84)

`GET /api/git/repos` hace dos cosas para que 24 repos en Windows tarden
< 2 s en vez de los 9,5 s que tardaba:

* **El RECORRIDO del filesystem se cachea 30 s por owner**
  (`git_panel._DISCOVERY_CACHE`, `_DISCOVERY_TTL`) — lo caro en Windows es el
  `os.listdir` recursivo bajo cada carpeta enlazada, no el estado git de cada
  repo. La caché queda invalidada de dos formas: automáticamente, si la
  lista de proyectos del owner cambia (`_projects_fingerprint` compara
  `id`/`workspace`/`updated_at`/`context_revision` de cada proyecto en cada
  llamada — barato, es una lectura de JSON, no un recorrido de disco) — así
  que crear/renombrar/(des)enlazar un proyecto invalida la caché sin que
  nadie tenga que acordarse de llamarlo explícitamente; y explícitamente,
  `POST /api/git/repos` llama `invalidate_discovery_cache(owner)` justo tras
  crear/clonar, para que el repo nuevo aparezca en el siguiente listado sin
  esperar los 30 s (una creación de repo no cambia ningún proyecto, así que
  el fingerprint no lo detectaría solo). Un listado con `project_id`
  siempre recorre fresco (un único proyecto es barato) — la caché es solo
  para el listado de todos los proyectos del owner.
* **El resumen de cada repo se calcula EN PARALELO**
  (`git_panel.repo_summaries`, `ThreadPoolExecutor(max_workers=8)`) — cada
  resumen es su propio proceso `git` (spawn), así que solapar 8 a la vez en
  vez de esperarlos uno a uno es la otra mitad de la mejora.
* **Cada resumen hace el mínimo de llamadas `git`**: una sola
  `git status --porcelain=v2 -z --branch --untracked-files=all` da rama,
  detached, sha de HEAD, upstream, ahead/behind y los tres contadores dirty
  — todo lo que antes salía de `symbolic-ref` + `rev-parse --abbrev-ref @{u}`
  + `rev-list --count` + `status` + `rev-parse HEAD` combinados (con `-z`,
  las cabeceras `# branch.*` de v2 también terminan en NUL, así que un único
  `split("\x00")` las separa de las entradas de fichero:
  `git_panel.parse_status_v2_branch`). El `user.name`/`user.email` efectivo
  sale de un único `git config --get-regexp '^user\.(name|email)$'` (antes,
  dos `git config` sueltos). Con eso, un resumen completo hace **3**
  llamadas `git` (status+branch, user, `remote -v`) en vez de las **8-9**
  de antes (las 8 del propio resumen, más una `remote -v` repetida que
  `routes/git_routes.py` disparaba aparte para calcular `identity` — ahora
  reutiliza los `remotes` que `repo_summary` ya trajo,
  `git_identities.active_identity_for_repo(..., remotes=row["remotes"])`).

`GET /api/git/repos?light=1` (o el `project_id`-scoped) pasa `light=True` a
`repo_summary`: **1** sola llamada `git` por repo — omite `remotes`, `user`,
`identity` y `policy`, deja solo `branch`/`detached`/`head_sha`/`upstream`/
`ahead`/`behind`/`dirty` (más `id`/`path`/`name`/`project_id`/`project_name`/
`projects`/`root_folder`/`parent_repo_id`, que no cuestan `git`). Pensado
para el polling del panel: la UI hace `?light=1` cada pocos segundos y
conserva `identity`/`policy` del último listado completo.

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

### `GET /api/git/repos?project_id=<opcional>&light=<0|1>`

Lista los repos bajo las carpetas enlazadas del owner (o de un único
proyecto, si se pasa `project_id`; un id que no exista o no sea del owner
responde 404).

```jsonc
{
  "repos": [{
    "id": "a1b2c3d4e5f6", "path": "/home/luis/code/faustus", "name": "faustus",
    "project_id": "p1", "project_name": "Faustus",
    // uno por cada proyecto que enlaza esta misma ruta real -- normalmente
    // uno solo; project_id/project_name arriba son siempre projects[0].
    "projects": [{"id": "p1", "name": "Faustus"}],
    "root_folder": "/home/luis/code/faustus",
    "parent_repo_id": null,
    "branch": "master", "detached": false, "head_sha": "abc123...",
    "upstream": "origin/master", "ahead": 0, "behind": 2,
    "dirty": {"staged": 1, "unstaged": 3, "untracked": 5},
    // Los 4 campos siguientes -- ausentes con `?light=1` (ver Rendimiento arriba):
    "user": {"name": "Luis", "email": "luis@example.com"},
    "remotes": [{"name": "origin", "fetch_url": "git@github.com:...", "push_url": "git@github.com:..."}],
    "identity": {"id": "sshcfg:...", "label": "Luissalet", "github_login": "Luissalet"},
    "policy": {"effective": {"...": "..."}, "overridden": false}
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
source: "ssh_config"|"manual"|"gh"}`.

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
- **`source: "gh"` (Lote 84)**: una por cada cuenta que `gh auth status`
  reporta ya logueada (`src/git_github.py`) -- `id: "gh:<login>"`,
  `identity_file: null`, `ssh_host: null` (no hay alias que deducir: el
  contrato es explícito en que inferirlo vía `gh api user/keys` es
  sobre-ingeniería), `github_login` = el login (siempre relleno, no viene de
  una prueba ssh), más `protocol` (`"ssh"|"https"`, el que `gh auth status`
  reporta para esa cuenta) y `active` (si es la cuenta activa de `gh`).
  `gh_accounts()` se cachea 15 s, así que listar identidades no dispara
  `gh auth status` en cada llamada. Ver "GitHub vía `gh`" más abajo.
- **`github_login`**: para `ssh_config`/`manual`, descubierto, no
  configurado. `POST .../probe` corre
  `ssh -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new git@<alias>`
  (o `-i <identity_file> git@<hostname>` para una clave suelta sin alias) y
  parsea `"Hi <login>! You've successfully authenticated"` de stderr (rc 1
  es la respuesta normal de GitHub a `-T` -- no da shell a propósito).
  Timeout 8 s, cacheado 10 min por id; `GET /api/git/identities` nunca
  prueba la red por sí sola, solo devuelve lo que ya esté en caché -- pero
  `GET /repos/{id}/identity` sí, ver abajo. Una identidad `gh` nunca se
  prueba por ssh: su login ya se conoce directamente.

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
     require_user. Si la identidad activa no es `source: "gh"` y su
     `github_login` no está cacheado, esta ruta PRUEBA por su cuenta
     (Lote 84) -- timeout 8 s (`probe_identity`, sin `force`, así que un
     resultado ya cacheado -- incluso uno fallido -- no vuelve a probar) --
     para que el chip pueda pintar "(login: X)" sin que un humano tenga que
     pulsar el botón de prueba aparte. Nunca bloquea más de esos 8 s.

PUT  /api/git/repos/{repo_id}/identity
     {"identity_id", "remote"="origin", "set_git_user": true}
     → reescribe la URL del remoto y, si `set_git_user`, ajusta la identidad
     git local del repo. require_human. Para una identidad `ssh_config`/
     `manual`: `git@<alias>:<owner>/<repo>.git` (entiende ssh scp-like,
     `ssh://` y `https://` de entrada) y, si trae `git_user_name`/
     `git_user_email`, los fija como `user.name`/`user.email` LOCAL (nunca
     global) -- SIEMPRE los sobrescribe. 400 `git.no_alias` si la identidad
     no tiene alias ssh (una clave suelta).
     Para una identidad `source: "gh"` (Lote 84, sin `ssh_host`): reescribe
     a `git@github.com:<owner>/<repo>.git` (o `https://github.com/<owner>/
     <repo>.git` si el `protocol` de esa cuenta de `gh` es https) -- nunca
     400 `git.no_alias`, esta fuente no lo necesita -- y, si `set_git_user`,
     fija `user.name` = `github_login` SOLO cuando el repo no tiene ya un
     nombre efectivo (local o global); nunca toca `user.email` (una cuenta
     `gh` no trae uno).
     400 `git.unrecognized_url` si la URL del remoto no se pudo parsear;
     400 `git.no_remote` si el repo no tiene ese remoto. Devuelve
     `{"remote", "remote_url_before", "remote_url_after", "git_user", "repo"}`.
```

## GitHub vía `gh` (`src/git_github.py`, Lote 84)

Para "crear un repo y subir cosas" hace falta el lado GitHub del remoto antes
de poder hacer `git push`. Esta pieza es un wrapper fino y testeable sobre
tres subcomandos de la CLI `gh` -- nunca la API REST de GitHub directamente,
nunca un token propio: usa la sesión de `gh` que Luis ya tiene autenticada
(`gh auth status` → cuentas `Luissalet` (activa) y `Mlgpigeon`, protocolo
ssh). Todo corre a través de un único punto de entrada inyectable,
`git_github._run_gh` (nunca `shell=True`, argv real, `stdin=DEVNULL` para que
un `gh` no interactivo jamás se quede esperando un prompt) -- los tests
sustituyen esa función, nunca necesitan un `gh` real. En Windows, `gh` puede
resolver como `gh.exe`; `shutil.which("gh")` ya lo encuentra vía `PATHEXT`
sin nada especial.

- **`gh_available()`**: comprobación de PATH, sin lanzar ningún proceso.
- **`gh_version()`**: `gh --version`, parseado a `"2.40.1"`.
- **`gh_accounts()`**: `gh auth status`, parseado a
  `{"available", "version", "accounts": [{"login","active","protocol","scopes"}]}`
  -- nunca el token, solo lo que `gh auth status` ya imprime en claro.
  Cacheado 15 s (`gh_accounts(use_cache=False)` lo salta) porque tanto el
  listado de identidades como la elección de URL de remoto lo consultan.
- **`gh_token(login)`**: `gh auth token --user <login>` -- el token vive SOLO
  en memoria, como `GH_TOKEN` en el entorno del proceso hijo que crea el
  repo; nunca se loguea ni aparece en ninguna respuesta.
- **`create_github_repo(login, name, private=True, description="")`**:
  `gh repo create <login>/<name> --private|--public [--description ...]`,
  ejecutado con `GH_TOKEN` de `login` en el entorno del hijo -- SIN cambiar
  la cuenta activa global de `gh` (`gh auth switch`). No toca el working tree
  local (sin `--source`/`--push`/`--remote`): solo crea el repo en GitHub;
  `git_panel.add_remote`/`push` hacen el resto, igual que lo haría un humano
  a mano. Tras crear, `gh repo view <login>/<name> --json
  nameWithOwner,url,sshUrl` da las URLs exactas (no se parsea el stdout de
  `repo create`, que cambia de formato entre versiones de `gh`). Devuelve
  `{"full_name","html_url","ssh_url","https_url"}`. 409 `github.exists` si
  el nombre ya existe bajo esa cuenta (stderr contiene "already exists");
  502 `github.failed` con `stderr` para cualquier otro fallo de `gh`.

```
GET  /api/git/github/accounts
     → {"available": bool, "version": str|null,
        "accounts": [{"login","active","protocol","scopes":[...]}]}
     require_user.

POST /api/git/repos
     {..., "github"?: {"create": bool, "login": str, "private": true,
                        "description"?: str, "push": true, "identity_id"?: str}}
     Solo tiene efecto con `mode: "init"` ("clone no aplica" -- un repo
     clonado ya trae el `origin` de su fuente). Tras crear el repo local (y
     su commit inicial, si aplica): crea `login/name` en GitHub, añade
     `origin` (URL ssh de una `identity_id` explícita si se dio, si no la
     del protocolo de la cuenta `login` en `gh_accounts()`, si no la ssh que
     `create_github_repo` ya resolvió) y, si `push` (default true), hace
     `git push -u origin <default_branch>`.
     → 201 `{"repo", "github": {...}|null, "push": {"ok","output"}|
       {"ok": false,"error_class","stderr"}|null}` -- `github`/`push` quedan
       `null` cuando no se pidió `github.create`.
     400 `github.login_required` si `github.create` es true sin `login`.
     409 `github.exists` / 502 `github.failed` si `gh` falla -- el repo LOCAL
     ya creado se conserva en ambos casos, y la respuesta incluye `"repo"`.

POST /api/git/repos/{repo_id}/github/publish
     {"login", "private"=true, "name"?: (por defecto el nombre del repo),
      "identity_id"?, "push"=true, "description"?}
     → para un repo que aún no tiene `origin`: crea `login/name` en GitHub,
     añade `origin`, empuja (`push` default true). Misma respuesta que
     arriba (siempre con `github`/`push`, nunca `null` aquí -- si `gh`
     falla, el error se devuelve directamente en vez de `null`s). 409
     `git.remote_exists` si el repo YA tiene `origin` (no se llama a `gh`
     en absoluto). require_human.
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
      "default_branch": "main",
      "github"?: {...}}  # ver "GitHub vía `gh`" más abajo
     → 201 {"repo": <objeto repo>, "github": {...}|null, "push": {...}|null}.
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

## Herramientas del agente (Lote 87)

Además de la política automática de arriba, el modelo puede llamar a git
explícitamente dentro de un turno — "haz commit de esto y push" — a través
de nueve tools de function-calling, en vez de `bash`. Implementación:
`src/agent_tools/git_tools.py`; cada una es un ejecutor fino sobre
`src.git_panel` (el mismo runner de `git` endurecido — sin `shell=True`,
`-c core.fsmonitor=`, `--no-ext-diff` — que ya usa el panel), así que su
vocabulario de errores es el mismo que documentan las rutas de arriba.

```
git_status    lectura   rama, ahead/behind, staged/unstaged/untracked, últimos N commits
git_log       lectura   historial de commits (limit, ref)
git_diff      lectura   diff de árbol de trabajo / staged / de un commit, recortado a 60 KB
git_branch    escritura crea (y por defecto hace checkout de) una rama
git_checkout  escritura cambia de rama
git_commit    escritura stage de paths EXPLÍCITOS (nunca `-A`) + commit con la identidad propia del repo
git_push      remoto    push (nunca --force -- git_panel.push no tiene esa opción)
git_pull      remoto    pull --ff-only (nunca merge/rebase)
git_fetch     remoto    fetch
```

Las nueve aceptan un `path` opcional (por defecto, el workspace activo del
turno); las de solo lectura devuelven además datos estructurados (`branch`,
`commits`, `diff`, ...) junto al `output` de texto.

### Confinamiento al workspace del turno

Todo `path` se resuelve con `src.tool_execution._resolve_tool_path` — el
MISMO allowlist que ya usan `read_file`/`write_file`/`manage_spreadsheet`
(el workspace del turno, más cualquier carpeta enlazada al proyecto de la
sesión). Un `path` que se sale de esas raíces, o un workspace sin repo git
en él o por encima, se rehúsa ANTES de lanzar ningún proceso `git`:

- `error_class: "git.outside_workspace"` — el path (o el workspace activo,
  si se omitió `path`) no está dentro de las raíces confinadas del turno.
- `error_class: "git.not_a_repo"` — el path confinado no tiene ningún
  `.git` en él ni por encima.

El resto de errores reutiliza, byte a byte, el vocabulario que ya usan las
rutas del panel: `git.dirty`, `git.diverged`, `git.rejected`,
`git.no_identity`, `git.nothing_to_commit`, `git.command_failed`,
`dependency.missing` (git no está instalado en el host).

### Política de git del agente y aprobación humana

`git_branch`/`git_checkout` (`policy.use_branch`), `git_commit`
(`policy.commit`) y `git_push` (`policy.push`) consultan la política
EFECTIVA del repo destino (`agent_git_policy.effective_policy` — la misma
función que usan `GET /api/git/repos/{id}/policy` y los hooks
before_turn/after_turn de arriba) antes de tocar nada. Si el campo relevante
es `false`, la llamada se rehúsa con
`{"error": ..., "policy": "git_agent_policy", "git_policy_field": "commit"|"push"|"use_branch"}`
— salvo que un humano haya aprobado explícitamente ESA llamada exacta.
`git_pull`/`git_fetch` no llevan gate de política: solo leen del remoto y
hacen fast-forward de refs locales, la misma clase de riesgo que los
botones de Fetch/Pull siempre activos del panel.

Cómo una tool sabe si su llamada fue aprobada por un humano — dos vías,
ambas comprobadas por `git_tools._human_approved(ctx, args)`:

1. **`ctx["human_approved"]`** — puesto por
   `src.tool_execution.execute_tool_block` a partir de si el contenido de
   ESTA llamada coincidió, byte a byte, con una `PendingToolApproval`
   sellada que el usuario respondió en una tarjeta de aprobación
   (`src.tool_approvals.ExactToolApproval.claim`). Es el MISMO mecanismo que
   ya usan el resto de tools con aprobación (entrada de escritorio, un
   veredicto del guard de comandos destructivos, una escritura tras
   contexto externo) — ningún subsistema de aprobación nuevo. Se activa
   cuando el turno ya necesitaba una tarjeta por otro motivo (una página
   externa leída antes, un comando marcado por el guard, ...) y el usuario
   también aprobó esta llamada de git.
2. **`args["user_confirmed"]`** — el propio modelo deja constancia de que
   preguntó al usuario (vía `ask_user`) si quiere saltarse la política del
   repo, el usuario dijo que sí, y reintenta la MISMA llamada con el flag a
   `true`. Es la misma forma "pregunta, y reintenta con un flag explícito"
   que ya usa `install_dependencies`
   (`src/agent_tools/exec_tools.py`) para un plan que necesita un sí humano
   sin que haya ninguna tarjeta sellada de por medio — un turno normal, sin
   contexto contaminado ("comitea esto y haz push"), no crea tarjeta porque
   ningún gate la habría bloqueado antes.

Un modelo corriendo desatendido (una tarea programada, sin turno humano que
relaye una respuesta) no tiene ninguna de las dos: `ctx["human_approved"]`
es falso porque nunca se selló una tarjeta, y nada le indica poner
`user_confirmed`. Es deliberado: en modo autónomo sin aprobación, no.

`git_commit` exige siempre `paths` (una lista no vacía de ficheros a
stagear) — nunca hace `git add -A` implícito. No hay, hoy, forma de que la
tool sepa qué ficheros tocó el propio turno del agente sin que
`src/agent_loop.py` se lo pase explícitamente (ver "Cambios necesarios en
ficheros ajenos" en el informe de cierre del Lote 87); mientras eso no
exista, el modelo debe nombrar los ficheros exactos.

### Esquemas y catálogo

Las nueve están declaradas en `src/tool_schemas.py::FUNCTION_TOOL_SCHEMAS`
(function-calling nativo), registradas en `src/agent_tools/__init__.py`
(`TOOL_HANDLERS`/`TOOL_TAGS`, para el fencing XML de los modelos sin tool
calling nativo) y clasificadas en `src/tool_capabilities.py`: las de lectura
como `READ_WORKSPACE`; `git_branch`/`git_checkout`/`git_commit` como
`WRITE_WORKSPACE`; `git_pull`/`git_fetch` como `NETWORK_EGRESS` +
`WRITE_WORKSPACE`; `git_push` como `NETWORK_EGRESS` +
`EXTERNAL_SIDE_EFFECT` (no existe un `ToolEffect.REMOTE` literal — se
componen a partir de los efectos existentes, todos ya dentro de
`POST_EXTERNAL_BLOCKED_EFFECTS`). `src/tool_registry.py` deriva su catálogo
automáticamente de esas tres fuentes — ninguna entrada manual adicional.
Las nueve están además en `NON_ADMIN_BLOCKED_TOOLS`
(`src/tool_security.py`) — misma clase de privilegio que `bash`/
`read_file`/`write_file`: tocan el disco (y, para push/pull/fetch, un host
remoto) del owner, nunca de un usuario público — y las de solo lectura en
`PLAN_MODE_READONLY_TOOLS`.
