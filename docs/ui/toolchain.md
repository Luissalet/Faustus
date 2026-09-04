# Faustus Studio — toolchain (UI-002)

## Architecture

```text
studio/                      ← source (TypeScript + JSX)
  index.html                 ← Vite entry (dev only, not served in production)
  src/
    main.tsx                 ← React entry point
    ...                      ← future: components, screens, store

scripts/
  build-studio.js            ← build-if-stale script

static/studio/               ← build output (gitignored)
  studio.js                  ← single entry bundle
  index.html                 ← built HTML (not served — FastAPI serves its own)
  chunks/                    ← code-split chunks (when they appear)
  assets/                    ← CSS and other assets
```

## Build

```bash
# Full build (always)
node scripts/build-studio.js --force

# Build only if sources changed
node scripts/build-studio.js

# Dev server with HMR (proxies API to Faustus on :7001)
npx vite --config vite.config.ts
```

### How it works

`scripts/build-studio.js` compares the newest mtime under `studio/`
against `static/studio/studio.js`. If the source is newer (or the bundle
is missing), it runs `vite build`. If the bundle is fresh, it exits
immediately.

On failure, it exits non-zero with a message. It never serves a stale
bundle in silence.

### npm install note

The system has `NODE_ENV=production` and `npm config omit=dev` globally.
Build tools (vite, typescript, @vitejs/plugin-react) are in `dependencies`
instead of `devDependencies` so they install regardless of environment.
This is fine for a private repo.

After cloning, run:

```bash
npm install --ignore-scripts
node node_modules/esbuild/install.js
```

The `--ignore-scripts` is needed because esbuild's postinstall can fail
on Windows with npm 10.x due to a shell-spawn bug
(`ERR_INVALID_ARG_TYPE`). The manual `install.js` call does the same
work.

## Start-Faustus.ps1 integration

`Start-Faustus.ps1` lives at `D:\LocalAI\Start-Faustus.ps1` (outside
this repo). To integrate the Studio build, add this block **before** the
uvicorn start:

```powershell
# ── Build Studio UI if stale ──────────────────────────────────
$studioScript = Join-Path $OdysseusRoot "scripts\build-studio.js"
if (Test-Path $studioScript) {
    Write-Host "Checking Studio bundle..." -ForegroundColor Cyan
    $buildResult = & node $studioScript 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Studio build failed — refusing to serve a stale bundle." -ForegroundColor Red
        Write-Host $buildResult
        exit 1
    }
}
```

This is documentation only. `Start-Faustus.ps1` is not edited by this
ticket.

## Bundle budget

| Metric | Budget | Current (empty shell) |
|--------|--------|-----------------------|
| Entry JS (uncompressed) | ≤ 350 KB | 194.54 KB |
| Entry JS (gzip) | ≤ 120 KB | 60.84 KB |
| Total JS (all chunks, gzip) | ≤ 180 KB | 60.84 KB |
| CSS (gzip) | ≤ 30 KB | 0 KB |
| Build time | ≤ 5 s | 0.5 s |

The empty shell (React + ReactDOM + no-op component) is the baseline.
As the shell, router, store and primitives arrive (UI-010 through
UI-022), the budget has room for ~120 KB gzipped of application code.

If a ticket pushes the total past budget, it must either code-split
the offender into a lazy chunk or justify the increase in
`DECISIONES_UI.md`.

## What is NOT in scope

- No CDN at runtime. Everything is served from `static/studio/`.
- No SSR. The bundle is a client-side SPA.
- No hot module replacement in production. HMR is dev-only via
  `npx vite`.
- No content hashing in filenames. FastAPI serves files with its own
  cache headers, and the build script checks freshness by mtime.

## Approved dependencies

From `DECISIONES_UI.md` §1:

| Package | Purpose | License |
|---------|---------|---------|
| react, react-dom | UI framework | MIT |
| react-router | Client-side routing | MIT |
| zustand | UI state store | MIT |
| @radix-ui/react-dialog | Accessible dialog primitive | MIT |
| @radix-ui/react-dropdown-menu | Accessible menu primitive | MIT |
| @radix-ui/react-popover | Accessible popover primitive | MIT |
| cmdk | Command palette (UI-022) | MIT |
| lucide-react | Icon library (tree-shaken) | ISC |
| @tanstack/react-virtual | List virtualization | MIT |
| vite | Build tool | MIT |
| @vitejs/plugin-react | JSX transform | MIT |
| typescript | Type checking | Apache-2.0 |

Any addition outside this table requires a decision entry in
`DECISIONES_UI.md` with license, weight and justification.
