# Estado del Lote A — Incremento 1

Rama: `feat/studio-ui`. Fecha: 04-09-2026.

## Resumen

Los tres tickets del Lote A están completados. La rama tiene cuatro
commits nuevos encima de `ac10a4c`:

```text
cfea6f6  Freeze the five baseline journeys, and document what hurts
ac793c1  Critique the current UI, validate every contrast pair, and write the design authority
c906485  Wire up Vite, React 19 and the approved deps, and measure the empty bundle
```

## UI-000 — Baseline de la UI actual: HECHO

- `tests/e2e/test_studio_baseline.py`: 5 tests, los 5 pasan.
- Cinco journeys capturados en tres viewports (1400×900, 1024×768, 390×844):
  crear proyecto, trabajo creativo, tarea de código, encontrar resultado,
  resolver aprobación.
- 63 capturas en `docs/ui/baseline/`.
- `docs/ui/journeys.md` documenta pasos, clics, puntos de confusión y lo
  que el fake model no puede cubrir.

### Lo que no se pudo medir

- El journey creativo no completa la generación (el fake model devuelve
  texto, no imágenes). Documentado en journeys.md.
- El journey de búsqueda depende del índice de conversaciones, que está
  vacío en el entorno E2E. Documentado.
- No se midió tiempo real de usuario. Los tiempos reportados son del
  test automatizado (~2 min total para los 5 journeys).

## UI-001 — Autoridad visual: HECHO

- `docs/ui/critica-inicial.md`: crítica completa con la skill
  `redesign-existing-projects`, incluyendo tipografía, color, layout,
  interactividad, contenido y componentes.
- Dos anatomías consultadas: Linear (surface ladder, densidad,
  restraint) y Raycast (command palette, keyboard-first, elevation
  sin sombras). Registrado qué se toma y qué no.
- `DESIGN.md` en la raíz del repo con todos los tokens validados.
- **Seis pares de color corregidos** porque no llegaban a WCAG AA
  (4.5:1). Los originales del plan estaban entre 3.56:1 y 4.43:1;
  todos los corregidos superan 4.51:1. La tabla de correcciones está
  en DESIGN.md.
- `docs/ui/component-contracts.md`: contratos P0 (8 primitivos) y P1
  (4 objetos de producto) con props, HTML, accesibilidad y test IDs.

## UI-002 — Toolchain: HECHO

- `package.json` actualizado con las dependencias aprobadas
  (`DECISIONES_UI.md` §1). Build tools en `dependencies` en vez de
  `devDependencies` porque el entorno tiene `npm config omit=dev`
  global.
- `vite.config.ts` con salida a `static/studio/`, proxy al Faustus
  del 7001 en dev.
- `studio/src/main.tsx`: entry point mínimo que compila y no monta
  nada. El shell es UI-021.
- `scripts/build-studio.js`: compila si el bundle falta o está
  obsoleto; falla con mensaje si no puede; nunca sirve un bundle
  viejo en silencio.
- `docs/ui/toolchain.md`: presupuesto de bundle (≤ 350 KB / ≤ 120 KB
  gzip), instrucciones de instalación, integración con
  Start-Faustus.ps1 (documentada, no editada).
- `.gitignore` actualizado: `static/studio/` es build output.

### Bundle actual (shell vacío)

| Metric | Value |
|--------|-------|
| studio.js | 194.54 KB (60.84 KB gzip) |
| Build time | ~0.5 s |

### Incidencia: npm en Windows

`npm install` falla con `ERR_INVALID_ARG_TYPE` cuando ejecuta el
postinstall de esbuild (npm 10.9.3 + Node 22.19.0 en Windows). La
solución es `npm install --ignore-scripts` seguido de
`node node_modules/esbuild/install.js`. Documentado en toolchain.md.
`Start-Faustus.ps1` no tiene este problema porque
`scripts/build-studio.js` ejecuta `vite` vía `node` directamente,
sin pasar por el script runner de npm.

## Tests

- Los 5 tests E2E baseline pasan.
- Los tests unitarios existentes tienen fallos preexistentes en
  Windows (documentados en `PENDIENTES_UI.md`). Ningún fallo nuevo
  introducido por este lote: el único fallo observado
  (`test_bash_runs_a_command_with_the_effective_bound`) se reproduce
  en la rama antes de los commits del Lote A.

## Qué sigue

El Lote B (UI-010 tokens CSS, UI-011 primitivos, UI-012 guardas) puede
empezar ahora. `DESIGN.md` es la autoridad.
