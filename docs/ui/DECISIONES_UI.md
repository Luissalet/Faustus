# Decisiones cerradas del overhaul de UI

Fecha: 04-09-2026
Rama de trabajo: `feat/studio-ui` (master no se toca)

Autoridad: este documento **manda** sobre `PLAN_CODE_UI_FAUSTUS_STUDIO.md` y
`UI_OVERHAUL_FAUSTUS_STUDIO.md` donde haya conflicto. Los otros dos siguen
siendo la especificación de producto y de pantallas, y no se han modificado.

**Deroga expresamente** la primera línea de «Prohibiciones de arquitectura» del
plan técnico («No migrar a React/Tailwind»). Ver §1.

## 0. Verificado contra el repo antes de decidir

- `static/index.html` 260 KB, `static/style.css` 1,52 MB, `static/app.js`
  200 KB: las cifras del plan son correctas.
- `tests/e2e/` **es Playwright real**, opt-in con `ODYSSEUS_E2E=1`, con servidor
  propio y modelo falso. Playwright 1.62 está en `venv`.
- No hay bundler de ningún tipo: `static/js/package.json` es literalmente
  `{"type": "module"}`, el `package.json` raíz sólo declara una devDependency
  y no hay `node_modules`. Node instalado: v22.19.0.
- `src/app_helpers.py::serve_html_with_nonce` sirve el HTML con nonce de CSP.
  Un bundle es un `<script>` externo: compatible sin cambios.
- `src/approval_store.py` y el coste por run en `src/agent_loop.py` y
  `src/external_worker.py` existen: ApprovalCard y chips de coste tienen
  respaldo real.
- No existe ningún mecanismo de feature flags.
- `static/index.html` contiene 262 `<svg>` inline y ninguna librería de iconos.

## 1. Stack: React es el destino, pantalla a pantalla

Decidido el 04-09-2026. El plan técnico prohibía React; esa prohibición queda
derogada, pero **no** por una reescritura de golpe.

- React monta el shell desde el primer incremento y **todas** las pantallas
  acaban dentro de él.
- El DOM legacy es un inquilino temporal: se le echa pantalla a pantalla y se
  borra en la fase 6. No habrá dos interfaces permanentes.
- Build con **Vite**. Salida a `static/studio/`, servida por FastAPI con el
  nonce que ya existe. Cero CDN en tiempo de ejecución: local-first significa
  que todo se sirve desde el propio Faustus.
- **Sin Tailwind.** Los tokens de `DESIGN.md` son la autoridad y el sistema de
  temas actual (`--bg`, `--panel`, `--fg`, `--red`) tiene que seguir vivo. CSS
  con variables, colocado por componente.
- El build tiene que ser reproducible sin red y `Start-Faustus.ps1` debe
  levantar la app sin que nadie se acuerde de compilar: si el bundle está
  obsoleto o falta, el arranque lo construye o falla diciendo por qué. Nunca
  sirve un bundle viejo en silencio.
- Ninguna dependencia entra sin comprobar licencia, peso y necesidad, y ninguna
  se añade «por si acaso».

Motivo, en orden de peso: los primitivos accesibles y la virtualización —lo más
caro del plan— vienen resueltos por el ecosistema; los agentes producen React
con mucha más fiabilidad que un shell a medida; y el objetivo declarado es
competir con productos que ya son React. Lo que React **no** aporta es criterio
visual: eso sale de `DESIGN.md` y sería idéntico en vanilla.

### Dependencias aprobadas para el shell

| Pieza | Elección | Por qué |
|---|---|---|
| Framework | React 19 | destino decidido |
| Build | Vite | node v22.19.0 lo soporta; sin config exótica |
| Rutas | React Router | rutas canónicas del plan, filtros en query string |
| Estado de UI | Zustand | el store pequeño del plan, sin ceremonia |
| Primitivos | Radix UI | Dialog, Menu, Popover accesibles de verdad |
| Listas | TanStack Virtual | Biblioteca por encima de 50 entradas |
| Paleta | cmdk | UI-022 |
| Iconos | lucide-react | ver §6 |

Cualquier añadido fuera de esta tabla es una decisión nueva y se anota aquí.

## 2. Primer incremento

```text
UI-000  journeys y capturas baseline           (con la UI actual, sin tocarla)
UI-001  DESIGN.md + contratos de componentes   (bloqueante duro)
UI-002  toolchain Vite y arranque integrado    (NUEVO)
UI-010  tokens CSS + puente con el tema legacy
UI-011  primitivos sobre Radix
UI-012  guardas y auditoría adaptadas a JSX    (reescrito, ver §7)
UI-020  router + store + fallback SPA
UI-021  AppShell React bajo flag
UI-022  command palette
UI-030  Inicio real
```

Activar el flag tiene que enseñar un Inicio usable, no seis destinos vacíos.
El prototipo de Studio creativo del documento de producto queda aplazado al
segundo incremento, detrás de UI-032 (Studio de código).

## 3. Fallback SPA en `app.py`: lista blanca, nunca comodín

Hoy `app.py` no tiene catch-all, y es el único cambio del incremento que puede
romper la API.

- Registrar **exactamente** las siete rutas canónicas (`/`, `/studio`,
  `/projects`, `/projects/{project_id}`, `/library`, `/automations`,
  `/activity`) devolviendo el shell con nonce, igual que la raíz.
- **Prohibido** `@app.get("/{path:path}")`: se tragaría los 404 de la API.
- Nada se declara por delante de `/api`, `/health` ni de los montajes estáticos.
- Test de regresión: una ruta de API inexistente sigue devolviendo 404 JSON.

## 4. Flag: piloto con fecha, no segunda interfaz

- `localStorage['faustus_studio_shell'] = '1'`; `?shell=studio` y
  `?shell=legacy` fuerzan y persisten. Apagado por defecto, rollback = recargar.
- El flag existe para poder volver atrás durante el piloto, **no** para
  mantener dos interfaces vivas. Cada pantalla migrada retira su equivalente
  legacy; cuando la última se va, el flag se borra con ella.
- El shell nuevo no escribe ningún dato que la UI antigua no sepa leer.

## 5. `DESIGN.md` es bloqueante duro

Ningún ticket de UI-010 en adelante empieza sin `DESIGN.md` en la rama. Tokens
dark/light, escala tipográfica, espacio, radios, elevación, motion, estados
completos, densidades por contexto y licencias de fuentes e iconos. Es lo único
que impide que cada agente invente sus propios radios y duraciones.

## 6. Iconos: `lucide-react` en Studio, legacy muere con su pantalla

Con bundler ya no tiene sentido extraer a mano los 262 SVG inline. Se adopta
`lucide-react` (MIT, tree-shaken) para todo lo nuevo. Los SVG inline del
`index.html` no se tocan: desaparecen cuando muere la pantalla que los usa.
Esto sustituye al ticket de extracción manual que estaba previsto.

## 7. Qué le pasa a los tests

- **E2E Playwright**: sobreviven si el shell nuevo expone `data-testid` estables.
  Es obligatorio desde el primer componente, no un añadido posterior.
- **Contratos HTML estáticos**: la familia de tests que assertaba markup dentro
  de `index.html` no sirve contra un árbol renderizado en cliente. Se sustituye
  por dos guardas: lint de JSX (nada de `<div>` con `onClick`, ningún control
  sin nombre accesible, ningún color o radio fuera de tokens) y comprobaciones
  de accesibilidad sobre la página ya renderizada en Playwright.
- **Regla de oro**: ningún test existente se borra sin que su sustituto pase
  antes. Si algo se queda sin cobertura, va a `PENDIENTES_UI.md` con nombre y
  apellidos, no se calla.

## 8. Estilo y verificación: en el navegador, en cada ticket

El acabado no se deja para el final ni se juzga por capturas del propio agente.

- Cada ticket con superficie visible se abre **en el navegador**, en la
  instancia del 7001, antes de darse por cerrado. Desktop y móvil.
- Secuencia obligatoria por pantalla: `DESIGN.md` → crítica con
  `taste-skill@redesign-existing-projects` → implementación → `impeccable`
  (`critique`, luego `polish`, y `delight` sólo donde aporte) → auditoría con
  `web-design-guidelines` → capturas antes/después.
- `impeccable` manda en jerarquía, espaciado, estados y microinteracción; **no**
  manda sobre el presupuesto de rendimiento ni sobre `prefers-reduced-motion`.
  Una animación que no se puede interrumpir o que se ignora con movimiento
  reducido no entra, por bonita que sea.
- Ninguna de las tres skills decide arquitectura, stack ni dependencias.

## 9. Reglas de trabajo en esta rama

- `feat/studio-ui`. Master no se toca hasta que el piloto convenza.
- Un lote por vez, secuencial, con verificación en navegador antes de cerrarlo.
- Commits como Luissalet, changesets pequeños, sin mezclar migración visual,
  cambio de API y refactor en el mismo ticket.
- `static/style.css` no crece: lo nuevo vive en el árbol de Studio.
