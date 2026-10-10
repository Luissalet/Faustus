# Project action namespaces / Espacios de acciones de proyecto

## English

Faustus distinguishes mutations to its built-in Objectives board from generic
project or goal changes. When the caller's selected tools include an MCP
project/goal mutation tool, generic create and update requests use that
namespace even if `project_objectives` or ordinary tools such as `read_file`
and `web_search` are also selected. Read-only project tools do not claim this
scope. A named request for
the `project_objectives` tool or Faustus
Objectives board takes precedence, including when combined with project
creation; it still requires a successful built-in apply call. A capability or
listing question about the tool is not an invocation request. An unrelated
tool success is never treated as proof of an objectives-board change. With no
scoped native project mutation tool, ambiguous requests retain the existing
built-in requirement and unavailable-tool response.

## Español

Faustus distingue los cambios genéricos de proyecto o meta de las mutaciones
del tablero **Objectives**. Si las herramientas seleccionadas incluyen una
herramienta MCP de escritura de proyecto/meta, las peticiones genéricas de
creación o actualización usan ese espacio aunque también se hayan seleccionado
`project_objectives` u otras herramientas como `read_file` o `web_search`. Las
herramientas de proyecto de solo lectura no activan este ámbito. Una mención explícita
a `project_objectives` o al tablero Objectives de Faustus tiene prioridad,
incluso junto con la creación de un proyecto, y sigue requiriendo una llamada
interna `apply` correcta. Una pregunta sobre su uso o sobre sus opciones no es
una orden de invocación. El éxito de una herramienta ajena nunca demuestra
que se modificó el tablero. Sin una herramienta MCP de proyecto fijada, las
peticiones ambiguas mantienen el requisito y el aviso de indisponibilidad
existentes.
