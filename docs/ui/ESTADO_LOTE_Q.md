# Estado del lote Q — Vitales (uso de GPU en la cabecera de Studio)

Fecha: 05-09-2026. Rama `feat/studio-ui`.

## Qué hay

`screens/studio/Vitals.tsx` + `adapters/usage.ts` + `vitals.css`. La
píldora de la anterior decía en palabras (`GPU 18% · 5.5/28G · 44° · no
model · RAM 43%`) lo que aquí se dibuja: una traza del uso de GPU de los
últimos dos minutos, un tanque de VRAM por tarjeta (del ancho de la
tarjeta), la temperatura como único número que cambia de color, el modelo
cargado; el desbordamiento a PCIe es lo único que grita. Clic → panel con
todo lo que sabía el panel antiguo, con las mismas fuentes
(`/api/system/usage`, `/api/system/gpu/orphans/release`). Sondeo
compartido (un solo temporizador por página) a 5 s, 1,5 s mientras Studio
responde; se para con la pestaña oculta. `/usage on|off`.

## Verificado en el 7001

Dos tarjetas (4070 Ti + 5060 Ti) sin modelo y con `qwen3.5:9b` cargado en
la #1: traza, tanques, temperatura, nombre; panel con salud 90/100, conjunto
y tarjetas, modelo con ubicación «GPU 1 (RTX 5060 Ti)» y keep-alive en
cuenta atrás, memoria compartida, equipo. Claro y oscuro. Guardas en verde
(`<svg>` con `guard-ok`: es una gráfica, no un icono).

## Pendiente

PENDIENTES 73–74.
