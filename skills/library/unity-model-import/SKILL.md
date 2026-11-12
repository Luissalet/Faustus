---
name: unity-model-import
description: Importa modelos y rigs en Unity conservando escala, materiales, avatar y referencias, con comprobación en juego. Use when replacing a Unity character or environment asset, importing FBX, fixing an avatar or diagnosing missing materials and model offsets.
version: 1.0.0
category: engineering
tags: [unity, import, fbx, models, modelos, humanoid, gamedev]
status: published
source: imported
---

## When to Use

Se incorpora un modelo o se sustituye uno existente, cambia un avatar humanoide
o aparecen materiales ausentes, escala incorrecta o desplazamiento lateral.
La importación no está terminada hasta ver ese modelo moviéndose con el prefab
y controlador que lo usarán en el juego.

## Procedure

1. Lee el motor y los paquetes instalados con `project_profile`, el diseño y
   la documentación de arte. Localiza el archivo fuente, exportador, importador
   y prefab consumidor. En Blood Arrows existen `Tools/Blender/character`,
   `CharacterImport`, `AnimatorBuilder` y `PlayerBuilder`: amplíalos si procede,
   sin crear una segunda canalización ni reconstruir todo para un cambio puntual.
2. Conserva identidades de assets y referencias. Un `.meta` contiene la identidad
   que usan escenas, prefabs y materiales. Sobrescribir el contenido de un asset
   con su identidad conservada es distinto de borrarlo y crearlo otra vez.
   No copies un `.meta` a dos assets. Inspecciona estado Git antes y después;
   no incluyas cambios de importación ajenos en el commit.
3. Contrasta escala y ejes entre DCC y Unity: metros, altura del personaje,
   orientación frontal, pivote y pose de reposo. Separa raíz de movimiento,
   pivote corporal y modelo visual. En Blood Arrows el controlador queda en
   los pies y `Body` pivota a 0,95 m; trasladar el mesh para compensar un rig
   incorrecto puede ocultar el problema y romper todas las animaciones.
4. Revisa tipo de rig, mapeado humanoide, avatar válido y procedencia del avatar
   reutilizado. Comprueba root motion, loop, frame rate y las acciones exportadas.
   No basta con ver una miniatura ni que `Animator.avatar` sea no nulo: debe
   haber un clip activo, tiempo que avanza y huesos enlazados durante Play.
5. Inspecciona materiales y texturas por slot, shader instalado, normales,
   tangentes, UV, importación de color y emisiones. Usa `material` para leer
   propiedades reales antes de `modify_material`. Una malla blanca o invisible
   merece diagnóstico de referencias y consola; no arregles la captura
   sustituyendo silenciosamente el material por otro genérico.
6. Abre una escena limpia desde disco. Ejecuta andar, correr, vuelo recto y
   giros con el nuevo asset en el prefab real. En Blood Arrows usa `run_playtest`
   y una carpeta nueva; registra modelo local, caderas y clips. Comprueba contacto
   de pies, límites de piel, sombras, cuchillas y cámara. Una hoja de contacto
   sirve para revisar poses, pero no sustituye esos recorridos.

## Pitfalls

Una escena abierta puede conservar una referencia destruida tras reconstruir
el controller; vuelve a abrir la escena y distingue ese problema de un avatar
incorrecto. No exportes sólo la malla si el contrato incluye acciones y rig.
No añadas colliders de render al personaje sin revisar el controlador existente.
Los límites de triángulos y texturas son del proyecto: mide, no inventes un
presupuesto. No incluyas Library, cachés o credenciales en la entrega.

## Verification

Conserva rutas fuente/exportadas, opciones de importación, referencias y receta
de construcción. Reabre escena y prefab para comprobar persistencia. Ejecuta
el banco con consola legible, mira capturas y reproduce los clips: cuerpo bien
anclado, animaciones activas y sin pérdidas de material. Si faltan las texturas,
avatar o acciones, deja un fallo visible con su causa y tarea. No declares
importación completa basándote únicamente en que el FBX aparece en Assets.
