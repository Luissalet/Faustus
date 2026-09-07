# Studio: extensión del panel de trabajo

Mode: Operate. Identidad existente de Faustus. El usuario pidió configuración del
equipo junto al chat y un panel persistente inspirado en las referencias de Codex.
Extensión acotada mediante código; sin nuevo mundo visual, seed ni raster de
producto. `DESIGN.md` y `.impeccable/design.json` permanecen sin modificaciones.

## Direction contract

THESIS: una conversación reúne su equipo, fuentes y resultados editables; navegar
no debe descartar borradores ni sugerir que un trabajador oculto se ha detenido.

OWN-WORLD: tokens cálidos y técnicos existentes, divisores discretos, botones y
tipografía del editor actual. Los estados se escriben, no son contadores decorativos.

STORY: elegir coordinador → configurar miembros → trabajar → consultar resultados
y agentes → editar con seguridad. Los conflictos conservan el borrador humano.

FIRST VIEWPORT: control de equipo junto al selector de modelo; panel derecho con
resultados, fuentes, agentes y navegador. El ajuste de ancho precede a la lista de
resultados abiertos. Navegador muestra capturas del agente, no navegación interactiva.

FORM: ampliación de la composición existente, con panel superpuesto por debajo de
escritorio. No aplica seed. Interacción distintiva: salir de un resultado y volver
al mismo borrador. La persistencia del panel es por conversación y sesión de
navegador; en privado permanece sólo en memoria.

FINISH: revisión de cierre pass después de resolver P1 (guardado tardío que podía
borrar una edición posterior) y P2 (áreas móviles de controles del equipo y cierres
de resultado). Detector manual una vez: `[]`. TypeScript, build y comprobaciones
de workbench correctos. Contratos, pruebas y límites en
`docs/ui/studio-workbench.md`; no se regenera la autoridad visual global sin permiso.

## Evidencia y alcance del cierre

Capturas de QA: `.impeccable/review/workbench/{desktop-panel,desktop-team,
mobile-panel,mobile-team,desktop-light}.png`. Son componentes reales dentro del
shell sintético local del puerto 7005, no el flujo completo de Studio. No hay
raster nuevo publicado que requiera procedencia de producto.

Se verifica la extensión y la corrección de los hallazgos señalados, no se
certifica todo el backend. La nueva suite global terminó sin fallos: 12.486 pruebas
correctas y 82 omitidas. Los cambios posteriores tienen 127 pruebas focalizadas
correctas y una omitida; no se mezclan ambos conjuntos como una única ejecución.
No se integra chat CLI ni navegador interactivo completo, y los conflictos
de edición requieren revisión humana, no una mezcla automática.
