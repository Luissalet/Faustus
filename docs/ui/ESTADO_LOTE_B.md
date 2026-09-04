# Estado del Lote B — Incremento 1

Rama: `feat/studio-ui`. Fecha: 04-09-2026.

## Resumen

UI-010, UI-011 y UI-012 hechos. El lote lo escribí yo (Claude, desde Cowork)
en vez del Claude Code local: su cuenta agotó el límite de uso justo al
terminar el lote A y devolvía `success` con cero tokens y cero tiempo de API.
Queda anotado porque explica por qué los commits de este lote tienen otra
cadencia.

## UI-010 — tokens y puente legacy: HECHO

- `studio/src/styles/tokens.css`: paleta de los dos temas, tipografía,
  espacio, radios, motion y alturas de control. Resolución de tema en tres
  niveles: `[data-theme]` explícito, `prefers-color-scheme`, y oscuro de base.
- `base.css`: foco visible global, skip link, `prefers-reduced-motion`,
  `max-width: 65ch`, `text-wrap: pretty` y cifras tabulares.
- `fonts.css`: las caras autoalojadas declaradas en la capa Studio, para que
  el bundle sea correcto por sí solo y no dependa de `style.css`.
- `legacy-bridge.css`: **opt-in**, con `data-theme-source="faustus"`.

### Decisión que no estaba en el plan

El puente tenía que ser opt-in. `theme.js` escribe `--bg`, `--fg`, `--panel`,
`--border` y `--red` como propiedades inline en `<html>` (línea 258), así que
esas variables están **siempre** presentes: heredarlas sin condición habría
hecho imposible ver nunca la paleta nueva. Con el atributo, un subárbol de
Studio adopta el tema del usuario y deriva con `color-mix` las superficies
que la paleta legacy no tiene, para que la escalera sobreviva a un tema de
dos colores.

## UI-011 — primitivos: HECHO

Button, IconButton, StatusBadge, EmptyState, Skeleton, Dialog, Menu, Popover
y `ExecutionTrace`. Radix pone focus trap, Escape, flechas y devolución de
foco en los tres overlays. Todos con `data-testid` estable desde el primer
commit.

Añadida una **galería de componentes** (`studio/src/gallery/`) que los
muestra todos con sus estados en ambos temas. No es un adorno: es lo que
permitió encontrar los tres fallos de abajo.

## Lo que encontró mirarlo en el navegador

Tres defectos que la especificación daba por buenos:

1. **Botón primario ilegible.** Los contratos pedían texto `--fs-text-1`
   sobre `--fs-brand`: blanco cálido sobre coral, **2,85:1**, por debajo de
   AA. Existen ahora `--fs-on-brand` y `--fs-on-danger`, medidos por tema y
   apuntando en direcciones opuestas: tinta sobre el coral oscuro (6,09:1),
   blanco sobre la marca más profunda del tema claro (5,52:1).
2. **Marca y peligro eran el mismo rojo.** Un "Eliminar" sólido al lado de
   un "Nuevo trabajo" sólido son dos botones idénticos — la queja de "tres
   rojos" de la crítica, reproducida dentro de la capa limpia. El
   destructivo pasa a ser contorno; `--fs-danger-solid` queda para un único
   caso: el botón que confirma dentro de un diálogo destructivo.
3. **Pesos tipográficos inexistentes.** La escala pedía 650 y 620, y solo
   hay Inter 400, 500 y 600 autoalojados. Corregida a lo que existe.

## UI-012 — guardas: HECHO

`tests/test_studio_guards.py`, 8 guardas, todas en verde. Detalle y
excepciones en `docs/ui/audit-baseline.md`. Deuda legacy medida hoy: 77
`transition: all` y 107 `outline: none` en `style.css`, más 262 SVG inline
en `index.html`. Ninguna se toca aquí.

## Bundle

307,38 KB · 98,10 KB gzip, contra un techo de 350 / 120. Incluye la
galería, que ninguna pantalla enviada va a importar.

## Verificado

En el navegador, en el 7001, tema oscuro y claro: los ocho primitivos con
todos sus estados, el foco visible, el diálogo con su trampa de foco y
cierre por Escape, el menú por teclado, y la traza colapsando.

## Lo que NO está hecho

- **Comprobación de accesibilidad sobre la página renderizada.** Las guardas
  son estáticas. Contraste en vivo, orden de foco y trampas de teclado
  necesitan la página montada y van con UI-021.
- **Auditoría Vercel a mano sobre los ficheros nuevos.** Las guardas cubren
  su parte mecánica; la lectura completa de la guía sobre este código está
  pendiente.
- **Pasada de `impeccable`.** Tiene sentido cuando haya una pantalla real
  (UI-030), no sobre una galería de piezas sueltas.

## Qué sigue

Lote C: UI-020 (router, store y la lista blanca de siete rutas en `app.py`,
nunca comodín), UI-021 (AppShell bajo flag) y UI-022 (paleta con `cmdk`).
