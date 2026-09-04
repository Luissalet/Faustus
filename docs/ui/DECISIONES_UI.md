# Decisiones cerradas del overhaul de UI

Fecha: 04-09-2026
Rama de trabajo: `feat/studio-ui` (master no se toca)

Autoridad: este documento **manda** sobre `PLAN_CODE_UI_FAUSTUS_STUDIO.md` y
`UI_OVERHAUL_FAUSTUS_STUDIO.md` donde haya conflicto. Los otros dos siguen
siendo la especificación de detalle y no se han modificado.

## 0. Verificado contra el repo antes de decidir

- `static/index.html` 260 KB, `static/style.css` 1,52 MB, `static/app.js`
  200 KB: las cifras del plan son correctas.
- `tests/e2e/` **es Playwright real**, opt-in con `ODYSSEUS_E2E=1`. Arranca un
  servidor propio con dir temporal, bypass de localhost, sin auth, y un modelo
  falso. Playwright 1.62 está instalado en `venv`. UI-000 no necesita harness
  nuevo.
- No existen `DESIGN.md` ni `static/js/shell/`.
- `src/approval_store.py` y el coste por run en `src/agent_loop.py` y
  `src/external_worker.py` existen: ApprovalCard y los chips de coste tienen
  respaldo real en backend.
- **No existe ningún mecanismo de feature flags** en el repo.
- `static/index.html` contiene **262 `<svg>` inline** y ninguna librería de
  iconos.

## 1. Primer incremento: resuelve la contradicción entre los dos documentos

El documento de producto propone empezar por un prototipo de Studio creativo.
El plan técnico propone cimientos y después Studio de código. **Gana el plan
técnico, ampliado con Inicio:**

```text
UI-000  journeys y capturas baseline
UI-001  DESIGN.md + contratos de componentes
UI-010  tokens.css / base.css / legacy-bridge.css
UI-011  primitivos accesibles
UI-012  auditor de regresiones con baseline de deuda
UI-013  inventario y extracción de iconos      (NUEVO, ver §4)
UI-020  store + router + fallback SPA
UI-021  AppShell bajo flag
UI-022  command palette
UI-030  Inicio real
```

Motivo: activar el flag tiene que enseñar algo usable, no seis destinos
placeholder. Inicio es la pantalla de menor riesgo —solo lectura sobre
adapters— y obliga a estrenar tokens, primitivos, router, estados vacíos y
teclado sin tocar datos.

El prototipo de Studio creativo del documento de producto **queda aplazado** al
segundo incremento, detrás de UI-032 (Studio de código).

## 2. Fallback SPA en `app.py`: lista blanca, nunca comodín

Hoy `app.py` no tiene catch-all. El plan lo despacha en media línea y es el
cambio más peligroso del incremento.

Obligatorio:

- Registrar **exactamente** las siete rutas canónicas (`/`, `/studio`,
  `/projects`, `/projects/{project_id}`, `/library`, `/automations`,
  `/activity`) devolviendo el shell con nonce, igual que la raíz.
- **Prohibido** un `@app.get("/{path:path}")` genérico: se tragaría los 404 de
  la API y rompería el manejo de errores y los tests existentes.
- Ninguna ruta nueva se declara por delante de `/api`, `/health` ni de los
  montajes estáticos.
- Test de regresión obligatorio: una ruta de API inexistente sigue devolviendo
  404 JSON, no HTML.

## 3. Feature flag: `localStorage` + query param, sin backend

No hay sistema de flags y construir uno administrable es otro proyecto. Para un
piloto de un solo usuario:

- `localStorage['faustus_studio_shell'] = '1'` activa el shell nuevo.
- `?shell=studio` y `?shell=legacy` fuerzan el valor y lo persisten.
- Apagado por defecto. Rollback = recargar sin el flag.
- El shell nuevo no escribe ningún dato que la UI antigua no sepa leer.

Si más adelante se quiere flag por usuario en servidor, será su propio ticket.

## 4. UI-013 (nuevo): inventario y extracción de iconos

`index.html` lleva 262 SVG inline y el plan prohíbe "otra familia de iconos"
sin decir cuál es la actual. Sin este ticket, cada componente nuevo copia y
pega SVG y la deuda se reproduce dentro de la capa limpia.

Alcance:

- Inventariar los 262 SVG, deduplicar y quedarse con el set real.
- Publicar `static/js/shell/components/icon.js` con registro por nombre y
  tamaños de token. `aria-hidden` por defecto; `aria-label` obligatorio cuando
  el icono es el único contenido de un control.
- No se instala ninguna librería externa de iconos.
- Ningún módulo Studio nuevo puede llevar SVG inline: lo comprueba el test
  estático de UI-012.

## 5. `DESIGN.md` es bloqueante duro

Ningún ticket de UI-010 en adelante empieza sin `DESIGN.md` en la rama. Es lo
único que impide que cada agente invente radios, sombras y duraciones. Debe
incluir tokens dark/light, escala tipográfica, espacio, radios, elevación,
motion, estados completos, densidades por contexto y las licencias de fuentes
e iconos.

## 6. Deuda conocida y aceptada: identificadores de artefacto

La Biblioteca federada por adapters es la decisión correcta para no bloquear la
fase 4, pero sin espacio de identificadores común entre galería, documentos y
outputs de run, la acción `Usar en…` entre subsistemas será frágil. Se acepta
como deuda declarada, vive en `PENDIENTES_UI.md` y se decide **antes** de
UI-042, no durante.

## 7. Skills de diseño instaladas para los agentes

- `vercel-labs/agent-skills@web-design-guidelines` — auditoría final por línea.
- `leonxlnx/taste-skill@redesign-existing-projects` — crítica de lo existente.
- `pbakaus/impeccable` con `critique`, `polish` y `delight` — acabado.

Límites explícitos: `impeccable` no puede saltarse el presupuesto de
rendimiento ni `prefers-reduced-motion`; `taste-skill` no gobierna dashboards,
tablas ni flujos multipaso; ninguna de las tres decide arquitectura ni stack.
Faustus sigue siendo HTML, CSS y módulos ES sin build.

## 8. Reglas de trabajo en esta rama

- Un lote por vez, secuencial. Verificación en la instancia del 7001 con
  navegador antes de cerrar el lote.
- Commits como Luissalet. Changesets pequeños. No mezclar migración visual,
  cambio de API y refactor masivo en el mismo ticket.
- `static/index.html`, `static/style.css` y `static/app.js` tienen dueño único
  dentro de cada lote.
- `static/style.css` no crece: todo lo nuevo vive en `static/css/studio/`.
- Nada se cierra por una captura bonita si falla el journey, el teclado o los
  estados.
