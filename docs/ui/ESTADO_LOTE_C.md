# Estado de los lotes C y D — Incremento 1 cerrado

Rama: `feat/studio-ui`. Fecha: 04-09-2026.

UI-020, UI-021, UI-022 y UI-030 hechos. **El incremento 1 está completo**: se
puede entrar en `http://127.0.0.1:7000/?shell=studio`, ver un Inicio con datos
reales, navegar con teclado y volver atrás en un clic.

## UI-020 — router, store y rutas de servidor

- `studio/src/shell/store.ts` (Zustand): contexto de interfaz, layout y paleta.
  Solo persiste preferencias de layout; nada de cachés ni permisos.
- `studio/src/shell/routes.ts`: los seis destinos, con una marca `ready` que
  distingue lo migrado de lo que todavía no.
- `app.py`: **lista blanca** de `/studio`, `/projects`, `/projects/{id}`,
  `/automations` y `/activity`. `/` y `/library` ya existían. Cero comodines.

### Lo que descubrió el test

`tests/test_studio_routes.py` empezó afirmando que una ruta de API inexistente
devuelve 404 JSON. Falla: con auth activo, el middleware **redirige a `/login`**
antes de que el enrutado la vea, así que el 404 no llega nunca. Fijar 404 habría
sido fijar la configuración de auth, no el enrutado. El test comprueba ahora el
invariante que sí vale en cualquier configuración: **una ruta no declarada nunca
devuelve el shell**, que es exactamente lo que haría un `{path:path}`.

## UI-021 — AppShell bajo flag

- Se inyecta desde `static/index.html` con un bloque que **no carga nada** salvo
  que el flag esté puesto o venga `?shell=studio` / `?gallery=1`.
- El árbol legacy sigue en el documento y sus módulos siguen vivos; simplemente
  deja de pintar mientras el piloto está montado.
- `Interfaz anterior` y `?shell=legacy` devuelven la app de siempre **idéntica**.
  Verificado en el navegador.

### Los dos fallos que solo aparecieron montándolo

1. **Ocultar el legacy se llevó por delante los overlays.** La regla que apaga
   el árbol antiguo esconde todo hijo de `<body>` que no sea la raíz de Studio,
   y Radix y cmdk montan sus portales exactamente ahí. Ctrl+K parecía no hacer
   nada. Arreglado con `#fs-overlay-root`, una raíz de overlays exenta por id,
   que además lleva la clase base para que los portales no hereden la
   tipografía del CSS antiguo.
2. **El z-index gigante enterró la paleta.** Le puse `2147483000` al shell para
   ganarle a los paneles legacy, y con eso quedaba por encima de sus propios
   portales. Con el legacy ya oculto no hay carrera que ganar: shell 50,
   overlays 60.

Ninguno de los dos se ve leyendo el código. Los dos se ven en dos segundos
abriendo la página.

## UI-022 — command palette

`cmdk` en `Ctrl/Cmd+K`, con los seis destinos y las acciones. "Buscar
conversaciones" es un comando dentro de la paleta, no un atajo rival.

## UI-030 — Inicio

Datos **reales**, no maqueta: `/api/projects`, `/api/sessions` y
`/api/approvals/pending`. Orden deliberado: lo que está bloqueado esperándote,
lo que puedes continuar, dónde trabajas, y cómo empezar algo. Sin modelo, sin
temperatura y sin GPU: son ajustes, no el asunto de la pantalla.

Si un endpoint falla, la pantalla lo dice por su nombre en vez de enseñar un
cero. Si fallan todos, ofrece la interfaz anterior.

## Verificado en el navegador (7001)

- Inicio con proyectos y conversaciones reales, tema oscuro.
- Ctrl+K abre, filtra y navega; `/projects` cambia la URL y marca el destino
  con `aria-current`.
- `/activity` **recargado directamente** monta el shell en su ruta: la lista
  blanca del servidor funciona.
- `?shell=legacy` devuelve la interfaz antigua intacta.
- Consola: los mismos errores de service worker que ya da la interfaz antigua.
  Cero errores nuevos.

## Prueba de que no se ha tocado lo viejo

`git diff --stat master..HEAD -- static/ src/` estaba **vacío** hasta este lote.
Ahora el único cambio en `static/` es el bloque de arranque del piloto al final
de `index.html`, y en `app.py` las cinco rutas nuevas. Nada más.

## Lo que sigue

- Auditoría Vercel a mano y pasada de `impeccable` sobre Inicio, que ya es una
  pantalla real y por fin tiene sentido criticar.
- Comprobación de accesibilidad sobre la página renderizada (orden de foco,
  trampas de teclado, contraste en vivo).
- Incremento 2: ContextBar y Studio de código.
