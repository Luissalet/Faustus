# Explicit tool discovery

Faustus offers connected tools named literally in the current user request before inference. A full function name is matched exactly; a short MCP alias is used only when it is unambiguous. Disabled tools and connector permissions remain in force. A caller-provided tool scope stays fixed.

Named tools remain available during tool exposure and sticky selection. This does not execute a tool or create an approval.

`lookup_tools` can recover connected MCP schemas with the existing lexical scorer when embeddings are unavailable or return no candidates. `category: "mcp:cicero"` limits results to that connected server. The normal result cap and permission filter still apply.

In the recorded creative workflow, `deck_export` was implicit in “export the presentation to PPTX”; literal-name preloading alone does not resolve that intent. The category/offline search regression covers the exporter separately. This improves schema discovery, not model decode speed or GPU placement.

Live MCP lexical matches are also merged when semantic retrieval returns a non-empty but incomplete list. Unscoped lookup keeps up to four accepted MCP matches and room for native tools. Category-scoped lookup applies its filters before counting candidates. `category: "mcp"` lists connected server categories and a bounded tool catalogue, or restricts a query to MCP tools.
