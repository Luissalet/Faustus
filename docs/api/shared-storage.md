# Almacenamiento compartido con Atlas

Atlas's Hoard es el disco local de trabajo de la familia. Un proyecto contiene
una carpeta compartida y carpetas para los archivos propios de los participantes.
Paint, Gimp y un Hoard que enlace el archivo abren el mismo PNG desde la misma ruta.

El plugin local `atlas` está incluido en Faustus, con puerto predeterminado 5203,
salud `atlas-hoard` y puente MCP de once herramientas. Selecciona la carpeta de
Atlas al conectarlo; su entorno Python no necesita paquetes de ejecución extra.
No tiene una entrada de descarga pública sin un repositorio publicado.

El Hub descubre su manifiesto y ofrece `hub_workspace`. Crear un proyecto devuelve
sus rutas normales; guardar y registrar un archivo no cambia su contenido. Las
aplicaciones usan su propio token al consultar Atlas a través del Hub. La pertenencia
a proyectos limita la API; no crea una caja aislada ni nuevos permisos de Windows.

Los proyectos existentes permanecen en su sitio. Algunos importadores copian sus
medios; para un enlace vivo deben usar una operación que conserve la ruta original.
Lumiere incorpora `media_shared(file_id)` y renueva sus cachés al repetirlo tras
editar el archivo, conservando el medio en los montajes. Prospero mantiene el
comportamiento de su importador habitual. Las bases privadas no se migran.

Atlas permite reutilizar resultados con fuentes y salidas de igual hash y una
receta con versión, opciones y revisión de modelo. No produce ni evalúa esos
resultados por sí mismo. Su contexto de tarea está acotado a objetivo, ámbito y
referencias explícitas; no mezcla toda la memoria personal con el trabajo.

El Hub copia el disco de Atlas junto con sus metadatos y restaura los archivos
compartidos en otra carpeta para revisión. Las reservas comunes CPU/RAM/disco
son cooperativas y opcionales para consumidores; la GPU mantiene su árbitro.

BookHoard y WatchHoard son independientes y quedan fuera de esta integración.
