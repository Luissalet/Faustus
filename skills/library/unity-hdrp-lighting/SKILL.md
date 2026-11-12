---
name: unity-hdrp-lighting
description: Ajusta iluminación HDRP con exposición, materiales y capturas reproducibles desde la cámara de juego. Use when a Unity HDRP scene is washed out, too dark, flat, occluded or inconsistent between editor preview and Play mode.
version: 1.0.0
category: engineering
tags: [unity, hdrp, lighting, iluminacion, exposure, gamedev]
status: published
source: imported
---

## When to Use

El escenario HDRP sale quemado, oscuro, plano o diferente al jugar; se pierden
la silueta del personaje, las aberturas o las señales. También al cambiar un
Volume, cielo, niebla, intensidad física o material. Esta skill exige comprobar
la cámara de juego y el pipeline instalado, sin trasladar recetas de otro RP.

## Procedure

1. Inspecciona `ProjectVersion.txt`, `Packages/manifest.json` y el paquete HDRP
   resuelto. Blood Arrows usa Unity 6000.6.5f1/HDRP 17.6. Consulta GraphicsSettings,
   QualitySettings, asset de pipeline y cámara con `unity` → `scene` y `object`.
   Anota los Volumes activos, prioridades, máscaras y si comparten perfil.
2. Lee el diseño visual antes de tocar luces. Define qué debe distinguirse:
   cuerpo, siguiente abertura y peligros. El laboratorio es una referencia de
   medición; el túnel de `Course.unity` es la escena real. No sustituyas la
   dirección artística por una escena de estudio que no representa el nivel.
3. Sigue la cadena luz → superficie → exposición → tonemapping → pantalla.
   Comprueba unidades de intensidad, temperatura, sombras, cielo, niebla y
   ambiente. Lee el material y sus propiedades instaladas mediante `material`;
   en HDRP/Lit un nombre de shader o propiedad de otro pipeline puede no tener
   efecto. Verifica que texturas y emisiones estén realmente vinculadas.
4. Captura antes de cambiar. En `build_playtest`, la receta admite
   `lighting:light|dark` y conserva el prefab del jugador y el perfil de entorno
   existente. Ejecuta los mismos recorridos con el perfil deseado y carpeta
   nueva. Para el nivel real conserva `Course.unity`, su niebla y sus luces.
   Registra posición, tiempo y FOV de cada imagen para poder repetirla.
5. Espera adaptación de exposición y calentamiento temporal. Decide si la prueba
   necesita exposición fija o la automática del juego; no mezcles ambas sin
   anotarlo. Usa capturas equivalentes para evaluar contraste. Una imagen del
   Scene View con iluminación de trabajo no demuestra el resultado en Play.
6. Cambia la causa más pequeña y repite el recorrido. Si hace falta editar un
   perfil compartido, identifica sus consumidores y conserva su identidad.
   No apagues niebla, sombras, bloom o geometría sólo para que el informe parezca
   limpio: la petición es verificar la escena que se va a jugar. Comprueba que
   materiales y prefab siguen enlazados al reabrir la escena guardada.

## Pitfalls

Un material blanco puede ser un asset sin asignar, una textura ausente, exposición
alta o un shader que aún compila. Mira consola y referencias antes de culpar
a la luz. Los efectos temporales del render request tienen historial separado;
el banco lo actualiza cada frame para evitar vectores falsos. Una imagen con
dispositivo `Null` o procedente de `-nographics` no prueba iluminación. El panel
claro u oscuro del informe cambia su presentación, no la iluminación del juego.

## Verification

Entrega capturas y clips desde la cámara HDRP, consola sin errores no explicados,
receta y configuración relevantes. Mira detalles de personaje, pared cercana y
apertura lejana en ambas condiciones de luz cuando formen parte del cambio.
Anota si el HUD está fuera del render capturado. Verifica el nivel real además
del laboratorio; guarda cualquier oclusión o pérdida de contraste como fallo
con una tarea concreta. No certifiques iluminación sólo porque el build termine.
