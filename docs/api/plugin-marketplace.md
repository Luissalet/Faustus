# Marketplace de plugins

En **Conectores → Marketplace de plugins**, descarga los hoards que quieras o
enlaza una carpeta que ya tengas. **HoardLink** (`hoardhub`) es obligatorio:
descargar cualquier hoard opcional adquiere primero HoardLink si falta.
WatchHoard y MyBookHoard son aplicaciones independientes y no forman parte
de este catálogo.

El catálogo compartido vive en `plugins/marketplace.json`; los manifiestos en
`plugins/<id>/plugin.json`. Ambos se versionan con Faustus. Los repositorios
descargados quedan en `plugins/<id>/repository/`, excluidos de Git, y los
enlaces locales se guardan en `<DATA_DIR>/plugin-marketplace-links.json`.
Así, quien clone Faustus recibe las referencias y el instalador, sin tus
rutas personales, datos ni copias de repositorios anidados.

Descargar sólo clona el código. No ejecuta scripts, instala dependencias ni
arranca procesos. El botón **Configurar conector** abre el formulario existente
con la carpeta del hoard rellena. Sigue las instrucciones del repositorio
original para preparar y arrancar cada aplicación. Los toolpacks stdio, como
CookHoard y GamerHoard, no necesitan un servidor web.

## Terminal

Desde el checkout de Faustus, con Python y Git disponibles:

```text
python scripts/plugin-marketplace.py list
python scripts/plugin-marketplace.py bootstrap
python scripts/plugin-marketplace.py install borges
python scripts/plugin-marketplace.py link borges "D:/Mis proyectos/Borges"
python scripts/plugin-marketplace.py unlink borges
python scripts/plugin-marketplace.py bootstrap --all
```

`bootstrap` adquiere sólo los obligatorios; `--all` añade los opcionales.
Los repositorios enlazados o descargados se reutilizan. Quitar un enlace
conserva sus archivos. Una ruta inválida o un destino ocupado requieren
corregir el enlace; el instalador no reemplaza carpetas ni actualiza checkouts
con modificaciones locales.

## API

Todas las rutas requieren el mismo acceso de administrador que los conectores.
Las operaciones de archivos/Git se ejecutan fuera del hilo del servidor.

| Método | Ruta | Resultado |
|---|---|---|
| GET | `/api/plugin-marketplace` | `root`, `plugins` y estado de cada fuente |
| POST | `/api/plugin-marketplace/{id}/install` | Adquiere requisitos y clona el ID del catálogo |
| POST | `/api/plugin-marketplace/{id}/link` | Valida y guarda `{"path":"..."}` |
| POST | `/api/plugin-marketplace/{id}/unlink` | Quita sólo el enlace, conserva los archivos |

Estados: `not_installed`, `cloned`, `linked`, `invalid_link`, `conflict`.
`local_path` señala la fuente efectiva y `clone_path` la ubicación prevista.
`required` y `can_install` describen los requisitos y acciones disponibles.
Una carpeta local debe presentar un manifiesto compatible con el ID; si no
tiene manifiesto, su origin Git debe coincidir con el repositorio del catálogo.
Los aliases SSH personales desconocidos no se adivinan.

El catálogo admite URLs HTTPS sin credenciales. El cliente no puede cambiar
la URL de un ID en una petición de instalación. Git se invoca sin shell,
con tiempo máximo de 120 segundos, sin preguntar por credenciales ni mostrar
stderr que pudiera contener secretos. Un fallo de un requisito detiene la
descarga del opcional. La serialización es del proceso de Faustus; no se
afirma una transacción entre procesos ni se ejecutan plugins para comprobar
su disponibilidad.

## Añadir un hoard

Añade un manifiesto validado y una entrada única en `plugins/marketplace.json`
con `id`, `repository_url` y `required`. Conserva el enlace al repositorio
original. El catálogo es de fuentes; listar o descargar un proyecto no
certifica que sus dependencias estén instaladas o que su servicio responda.

Fuentes: [HoardLink](https://github.com/Luissalet/HoardLink), los enlaces
individuales del catálogo y los `faustus-plugin.json` de sus aplicaciones.
Los manifiestos adicionales preservan las declaraciones originales.
