# Descubrimiento de herramientas explícitas

Faustus ofrece antes de inferir las herramientas conectadas que el usuario nombra literalmente en su petición actual. El nombre completo se compara exactamente; un alias MCP corto solo se usa si no es ambiguo. Se respetan las herramientas deshabilitadas y los permisos de conectores. El conjunto de herramientas fijado por el llamador no se amplía.

Los nombres explícitos se conservan durante la exposición y selección persistente. Esto no ejecuta ninguna herramienta ni crea una aprobación.

`lookup_tools` puede recuperar esquemas MCP conectados mediante el puntuador léxico existente cuando faltan embeddings o no devuelven candidatos. `category: "mcp:cicero"` limita los resultados a ese servidor conectado. Se mantienen el límite habitual y los filtros de permiso.

En el flujo creativo registrado, `deck_export` era implícito en «exporta la presentación a PPTX»; la precarga de nombres literales no resuelve por sí sola ese objetivo. La regresión de búsqueda por categoría y sin embeddings comprueba el exportador por separado. Esta mejora afecta al descubrimiento, no a la velocidad de generación ni al reparto de GPU.
