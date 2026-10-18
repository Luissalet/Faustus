# Silver: capacidades locales verificadas

Las tareas 836 y 838 están activas en Silver Main 1357cb5528fd. El análisis y las
animaciones añadidos no hacen consultas externas. La corrección general de
imágenes ausentes de las páginas portables se sigue por separado en 840; no se
presenta como terminada aquí.

## Análisis del borrador

En Entrenadores, abre Constructor de equipos → Análisis del equipo → Analizar.
La tabla muestra defensa por tipo y cobertura de ataques ordinarios contra tipos
puros; la lista ordena las velocidades sin modificaciones. Utiliza el simulador
instalado, la generación y las reglas de nivel del equipo, incluidos los puntos
de Champions. Cambiar cualquier dato del borrador exige recalcular. No guarda
cambios, predice victorias, valida legalidad ni calcula daño. Habilidades,
objetos, teracristalización y estado del combate quedan fuera. Los movimientos
de daño fijo, tipo variable o efectividad especial muestran el motivo de exclusión.
La herramienta nativa es team_analysis, compartida por UI/REST/MCP.

## Material de Dragón y Veneno

Danza Dragón, Pulso Dragón, Enfado, Corte Vacío, Ácido, Armadura Ácida,
Lanzamugre, Gas Venenoso, Colmillo Veneno, Tóxico y Púas Tóxicas utilizan once
motivos seleccionados de hojas PMD Time/Darkness locales: 117 cuadros, crédito
redblueyellow. No son los programas de emisores del cartucho ni todas las
animaciones de las hojas. Atlas cuadrados, offsets y transparencia conservan
geometría y píxeles; el modo de movimiento reducido elige un cuadro estacionario
visible. Comparten renderer/reloj con RT, campales, exportación HTML y vídeo.
No alteran simulación, daño, RNG, disponibilidad de movimientos ni replays.

El catálogo contiene 774 entradas, 160 fuentes, 218 atlas y 2777 cuadros. Las 763 entradas,
158 fuentes y 179 hashes anteriores se conservan. leisure_catalog permite consultar
los once motivos, rectángulos, offsets, clave de fondo y cuadro estacionario.

## Comprobaciones

Cada fuente pasó 601 Python/2 omitidas. La composición pasó 530 Node/1 omitida y 15 Python
del área/1 omitida; la prueba de píxeles opcional sí pasó con los originales en la
fuente y en la revisión independiente. No se afirma una segunda suite Python
completa para la composición. MCP nativo, UI ES/EN y 390 px, partida clásica hasta
victoria, once replays RT con resumen exacto y MP4 portrait 720x1280 probados con
datos sintéticos. Cada entrega fue aprobada sobre su revisión CAS 1. Activaciones
sin partidas/trabajos activos y hashes de tablas, medios, ajustes y token exactos.

Fuentes de implementación: docs/TEAM_ANALYSIS.md, docs/DRAGON_POISON_SPRITES.md y
docs/SUPPLIED_SPRITES.md del repositorio Silver's Hoard. Evidencia local:
D:/LocalAI/research-inbox/silver-offline836 y D:/LocalAI/research-inbox/silver-dragon838. Sin push.


## Actualización: recursos autónomos activos (840)

Desde Main `18c66ab0cf52`, 840 está integrada y sustituye el estado pendiente
indicado al principio. Al exportar un entrenador, gimnasio, reto semanal/diario
o campal, los archivos existentes de la caché de sprites, FX y Reborn viajan en
un mapa de recursos incrustados con aliases; cada archivo aparece una vez.
No se descarga material al exportar. Las imágenes de Pokémon ausentes usan un
disco con iniciales; la decoración ausente queda vacía y se mantienen los efectos
Silver disponibles. Los sonidos Reborn cacheados sí viajan. Música y gritos
habituales siguen sin incrustarse. El HTML crece con la caché y las páginas
antiguas requieren una nueva exportación. App y salas mantienen sus proveedores.

608 Python/2 omitidas y 534 Node/1 omitida en la revisión final, app+HTML compilados,
aprobación independiente CAS 1. Pruebas de navegador fuera de Silver en cuatro
clases de página: sólo su HTML local, sin API ni servidor externo, consola limpia.
Main mantuvo biblioteca, ajustes/token y medios exactos; una lectura sin escritura
verificó 851 recursos cacheados, 17.252.083 bytes originales. No se promete arte
completo cuando no está cacheado. Evidencia: research-inbox/silver-portable840.
