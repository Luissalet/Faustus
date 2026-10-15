# Visual index and "look at the page"

PDF pages, web pages and pictures kept as screenshot strips and searched by meaning, plus the same capture used to read
pages whose text lacks their data. Code: `src/visual_index/` (`tiling.py`, `render.py`, `embedder.py`, `store.py`,
`service.py`, `vision.py`, `lookpage.py`); routes `routes/visual_index_routes.py`; tools
`src/agent_tools/visual_index_tool.py`; Studio screen `studio/src/screens/visual/VisualIndex.tsx` (`/visual-index`).

## Strips

A source is rendered (PDF at 200 dpi with pypdfium2; a web page by the browser Faustus already has, 875 px wide, in tiles of
at most 8192 px; a picture as it is) and cut into strips of at most 1024 px high (and 875 px wide, so a wider page is also
cut across), at least 28 px on each side. Strips of one flat colour are left out. Each strip has the MD5 of its pixels:
indexing a source again embeds only the strips whose MD5 changed and drops the ones that are gone.

## Embedder

`Qwen/Qwen3-VL-Embedding-2B` through `transformers` (optional: `python -m pip install torch "transformers>=4.57"`; `pypdfium2`
is in `requirements-optional.txt`). It is loaded on first use and the weights (about 4 to 4.5 GB) are downloaded into
`data/models/visual-embedding` (`visual_index_models_dir`). Last token, L2-normalised, 2048 dimensions; queries carry the
retrieval instruction. GPUs 0 and 1 are never used: the model asks the GPU arbiter for memory on `visual_index_gpus` (default
`2,3`), else runs on the CPU, and is released after `visual_index_idle_unload_seconds`. Without `torch`/`transformers` the
server starts normally and the calls answer `dependencies_missing` (HTTP 503) with the install command.

## Store

`data/visual_index/<owner>/<collection>/`: `meta.json`, `vectors.npy` (float16), `strips.json`, `sources.json`, `strips/<id>.png`.
Flat inner-product search with numpy. A collection remembers its embedder and refuses another one. Collections belong to the
user who made them. A strip id is `<collection>~<12 hex>`.

## Tools (deferred family `visual_index`)

| Tool | Arguments | Result |
| --- | --- | --- |
| `visual_index_add` | `paths` or `urls` (or `sources`), `collection?`, `pages?`, `force?` | per source: strips, embedded, reused, removed |
| `visual_search` | `query`, `k?` (max 20), `collection?` | strips with `source`, `page`, `y_offset`, `score`, image `path`, `id` |
| `view_tile` | `id`, `question?` | the strip as an image (main model sees) or what the vision model read; says so when nothing can look |
| `visual_index_list` | `collection?` | collections, or the sources of one |
| `visual_index_remove` | `collection`, `source?` | removes a source, or the whole collection |

`view_tile` is capped at `visual_index_view_limit_per_turn` (6) strips per turn.

## REST

`GET /api/visual-index/status`, `GET /api/visual-index/collections`, `GET|DELETE /api/visual-index/collections/{name}`
(`?source=` removes one source), `POST /api/visual-index/add` `{sources|paths|urls, collection?, pages?, force?}`,
`POST /api/visual-index/search` `{query, k?, collection?}`, `GET /api/visual-index/tiles/{id}`, `GET .../tiles/{id}/image`,
`POST .../tiles/{id}/read` `{question?}`. A local path in `add` needs an admin; a URL does not.

## Look at the page

`lookpage.assess_page(text, html)` flags a page whose text probably lacks its data: no or very little text
(`research_look_min_chars`, 600), a canvas, tables that did not come out in the text, tables or charts built by a script,
or big pictures with little text. Deep Research (`research_look_at_pages`) and `web_fetch` (`web_fetch_look_at_page`, plus
`look` and `question` arguments) then capture the page in strips and show them one by one to the resident vision model with
the question; a strip with nothing relevant answers `NONE` and is dropped. What is read is added to the evidence marked as
read from a screenshot (`from_capture`, `[from a screenshot]`), with the model that read it, and is treated as untrusted page
content. Limits: 4 strips per page (`research_look_max_strips_per_page`), 16 per run (`research_look_max_strips_per_run`),
8 pages per run (`research_look_max_pages_per_run`), 3 tiles (`research_look_max_tiles`), 180 s per page
(`research_look_timeout_seconds`).

With no vision model nothing is captured and nothing is invented: the research report ends with a section listing the pages
whose data is visual and could not be read, and `web_fetch` adds the same sentence to its result.
