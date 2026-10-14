"""Local visual index of screenshots: PDFs, web pages and pictures as strips, searched by meaning.

``service.VisualIndexService`` is the entry point (REST routes, agent tools and the MCP server all call it);
``lookpage`` reads a web page that is mostly tables or charts through the vision model (Deep Research and ``web_fetch``).
Nothing in this package imports ``torch`` or ``transformers`` at import time.
"""
