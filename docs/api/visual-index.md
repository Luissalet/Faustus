# Visual index and "look at the page"

PDF pages, web pages and pictures kept as screenshot strips and searched by meaning, plus the same capture used to read
pages whose text lacks their data. Code: `src/visual_index/` (`tiling.py`, `render.py`, `embedder.py`, `store.py`,
`service.py`, `vision.py`, `lookpage.py`, `subprocess_embedder.py`, `worker.py`); routes `routes/visual_index_routes.py`; tools
`src/agent_tools/visual_index_tool.py`; Studio screen `studio/src/screens/visual/VisualIndex.tsx` (`/visual-index`).

## Strips

A source is rendered (PDF at 200 dpi with pypdfium2; a web page by the browser Faustus already has, 875 px wide, in tiles of
at most 8192 px; a picture as it is) and cut into strips of at most 1024 px high (and 875 px wide, so a wider page is also
cut across), at least 28 px on each side. The rows and columns of a page are split evenly (a 2200 px page gives three
strips of about 733 px, not 1024 + 1024 + 152), so a running footer never becomes a strip of its own. Strips of one flat
colour, or with under 0.4 % ink (a page number, a footer line), are left out: they only matched every query a little. Each strip has the MD5 of its pixels:
indexing a source again embeds only the strips whose MD5 changed and drops the ones that are gone; when nothing changed the result says `unchanged: true`.

## Embedder

`Qwen/Qwen3-VL-Embedding-2B` through `transformers` (optional: `python -m pip install torch "transformers>=4.57"`; `pypdfium2`
is in `requirements-optional.txt`). It is loaded on first use and the weights (about 4 to 4.5 GB) are downloaded into
`data/models/visual-embedding` (`visual_index_models_dir`). Last token, L2-normalised, 2048 dimensions; queries carry the
retrieval instruction. GPUs 0 and 1 are never used: the model asks the GPU arbiter for memory on `visual_index_gpus` (default
`2,3`), else runs on the CPU, and is released after `visual_index_idle_unload_seconds`. Without `torch`/`transformers` the
server starts normally and the calls answer `dependencies_missing` (HTTP 503) with the install command.

### Embedder in another Python (`visual_index_python`)

Set `visual_index_python` to the full path of a `python.exe` (or of the environment folder) and the embedder runs there, in
`src/visual_index/worker.py`, instead of inside Faustus. Use it when Faustus' own environment has a CPU-only `torch` that must
stay that way (the voice, transcription and gallery code would start using GPU 0 if it were a CUDA build). That Python **must have
`torch` with CUDA** (to use a GPU), **`transformers>=4.57`**, `pillow` and `numpy`; nothing is installed for you, and a missing
package is reported with the exact `pip install` line for that interpreter. Empty (default) keeps the model in the Faustus process.

- The card is chosen as before (never GPU 0 or 1; `visual_index_gpus`; a lease from the arbiter; no free card means the CPU),
  counting the cards of the worker's Python. The worker starts with `CUDA_DEVICE_ORDER=PCI_BUS_ID` and
  `CUDA_VISIBLE_DEVICES=<n>`, so inside it the chosen card is `cuda:0` and no other exists.
- The worker is closed after `visual_index_idle_unload_seconds` without use (the lease goes back); it is restarted once if it dies
  during a request; a request with no answer after `visual_index_worker_timeout_seconds` (900) stops it and the next call starts
  it again.
- Protocol (one JSON object per line on stdin/stdout, logs on stderr): `{"op":"info"|"load"}`, `{"op":"embed_images","paths":[png...]}`
  -> `vectors_b64` (float32, shape `[n, dim]`), `{"op":"embed_query","text":...}` -> `vector_b64`, `{"op":"close"}`; errors are
  `{"ok":false,"error":...,"code":...}`. `python worker.py --probe` prints what that interpreter has.
- Check it: `GET /api/visual-index/status?probe=1` (or the tool `visual_index_list {"status": true, "probe": true}`) returns
  `embedder.mode` (`in_process` / `subprocess`), `python`, `python_info` (torch, CUDA, cards, transformers), `loaded`, and the real
  `device` (`cuda:2`, host numbering) once the model is loaded.

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
| `visual_index_list` | `collection?`, or `status?` / `probe?` | collections, or the sources of one; with `status` the embedder state (mode, python, device, loaded), with `probe` it also checks the worker Python |
| `visual_index_remove` | `collection`, `source?` | removes a source, or the whole collection |

`view_tile` is capped at `visual_index_view_limit_per_turn` (6) strips per turn.

## REST

`GET /api/visual-index/status` (`?probe=1` also checks `visual_index_python`), `GET /api/visual-index/collections`, `GET|DELETE /api/visual-index/collections/{name}`
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
