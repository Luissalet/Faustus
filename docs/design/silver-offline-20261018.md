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


## Silver: navegación e inspección local de repeticiones — 18-10-2026

La tarea 842 está integrada y activa en Silver Main `4eed0beb0d97`, aprobada
por revisión independiente CAS 1 sobre eac025d, 1dacccb, 658256e, e0a2c1a y
4eed0be. Las repeticiones RT terminadas tienen cursor de fotogramas a 30 Hz,
saltos de cinco segundos y teclas Inicio/Fin/flechas. Saltar pausa y conserva
velocidad; se reconstruye el registro con los activos históricos y se limpian
efectos, avisos y debilitados anteriores. Un KO nuevo mantiene su animación.
La herramienta de solo lectura `battle_rt_inspect` devuelve el mismo estado
público y un enlace pausado, sin escribir palmarés, partidas ni progreso.
Admite sólo RT terminadas y limita al final guardado, incluidas rendiciones
en tick cero; las páginas autónomas también retienen el final al reverlo.
Cada salto reconstruye desde el principio; puede tardar más en combates largos.

610 Python/2 omitidas y 539 Node/1 omitida en la revisión final; build app+HTML
correcto y HTML probado idéntico por SHA256 al final. El modo quiet doble
suprimió resumen Python: salida0, 612 marcas completas y colección612
confirmadas en suite-final.json. R1 interrumpida no se cuenta. Pruebas reales
de ratón/teclado, ES/EN, móvil390 sin desbordamiento y HTML autónomo tras
rendición560ticks/18,66s: End560 terminal, Home0 y salto150 pausado, consola0.
Main191herramientas, biblioteca51/24/3/44 y6arenas,18tablas/ajustes/token/medios
exactos tras activación gestionada sóloSilver y siete frentes inactivos.
Consulta nativa Main de RT existente en0/150/939 sin escritura, y UI Main
pausada150 con registro y cursor. Datos de biblioteca también exactos después.
Sin cambios en reglas, RNG, daño, paquetes, APIs externas, modelos, Hub ni push.
Evidencia: research-inbox/silver-replay842; detalles en docs/RT_REPLAY_SEEK.md
de Silver.845 Roca sigue en QA y no se considera activa en este registro.


## Silver: cierre de Roca y pausa de efectos — 18-10-2026

Las tareas 845 y 847 están integradas y activas, con revisión independiente
CAS 1 de sus commits exactos. Silver Main está en `e73a13ed23298e85b06f7d54782263506ee335ee` y conserva
191 herramientas. Las dos activaciones gestionadas reiniciaron sólo Silver,
con los siete frentes inactivos, HEAD comprobado y compilaciones idénticas a
las probadas. Las 18 tablas de biblioteca, ajustes, token y medios protegidos
se conservaron exactamente: 51 entrenadores, 24 gimnasios, 3 ligas, 44 combates
y 6 arenas. No se añadieron partidas QA a la biblioteca del usuario.

Roca añade Poder Pasado (6 cuadros), Pedrada (7), Tumba Rocas (9) y Desenrollar
(7), extraídos de la hoja local PMD Red Rescue Team facilitada por Luis. Se
descartan extremos sin recuadro claro, separadores y rótulos. Los píxeles se
conservan; una escala uniforme al dibujar (2,4 para los grupos de piedras y
1,7 para el cerco) hace visibles los motivos junto al efecto procedural. Las
774 entradas anteriores conservan su imagen y metadatos. El catálogo queda
en 778 sprites, 161 fuentes, 222 atlas y 2.806 cuadros. Esta noche se añaden
15 motivos de movimientos y 146 cuadros entre Dragón/Veneno y Roca; no son
quince movimientos nuevos en las reglas. Comparten atlas RT, campal, HTML y
vídeo, sin cambiar físicas, daño, semilla ni entradas.

La pausa congela la edad, posición y emisión automática de partículas del
Vfx compartido RT, clásico y campal. Se corrigen proyectiles, carga, Fuego
Fatuo y espuma de las ondas campales. La reproducción original acumulaba
120 partículas con 120 actualizaciones sin tiempo; revisión independiente
encontró además 120 de Fuego Fatuo y 480 del campal: ahora todos quedan en
cero y reanudan correctamente. Siguen llegando snapshots a los sprites y
los bursts explícitos de eventos siguen disponibles al reconstruir el replay.
No se promete una imagen idéntica entre dibujos pausados: queda jitter visual
procedural. No cambia el motor, RNG, daño, controles ni audio.

Validación de Roca: full Python final 611 correctas,
4 omitidas, cero fallos; Node 541 correctas y una
omitida, builds app/HTML correctos. La ejecución anterior declaró 612 PASS,
2 omitidas y un fallo por el recuento antiguo 218; se corrigió a 222 y las
10 pruebas de ese archivo pasaron. Las seis pruebas de píxeles con originales
locales también pasaron. Cuatro combates nativos con resumen idéntico al
replay, cuatro PNG exactos dentro del HTML, catálogo de escala y MP4 vertical
720×1280 de 23,1 s. Capturas escritorio/móvil 390 px, consola sin errores.

Validación de pausa: seis regresiones nuevas; full Node 547 correctas y una
omitida; Python de área 16 correctas con originales y ambos builds correctos.
No se repitió full Python para ese cambio JS: se compone sobre el full de
Roca anterior. Revisor: 21 Node propias y reproducciones de los dos defectos
adicionales. Native MCP 191, RT de prueba en cuatro posiciones, campal de 27
iniciales y hash igual al motor. UI RT en pausa/reanudar/pausa, móvil 390 ES
con movimiento reducido sin desbordar ni errores; campal mantiene 0:06 en
pausa y termina igual al motor tras reanudar. MP4 final 1280×720, 25 s y
1.926.512 bytes, sonido incluido.

Estas mejoras no añaden paquetes, APIs externas ni funciones online. Las
páginas autónomas exportadas usan su caché embebida y sus sustitutos locales;
las cuatro pruebas fuera de Silver de la tarea 840 no hicieron solicitudes
externas ni API. No se afirma que todos los proveedores históricos de la app
hayan desaparecido. No se tocaron modelos, GPU o Hub ni se hizo push.
Pruebas detalladas: research-inbox/silver-rock845 y silver-pause847.
