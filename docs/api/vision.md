# Vision routing

When the active chat model cannot see images, images reach it as text written
by an auxiliary vision-language model (VLM). This page covers how that VLM is
chosen, what the model receives in place of images, and the two API surfaces
that expose it: `GET /api/vision/status` and the `models_vision` field of
`GET /api/models`.

Code: `src/vision_routing.py` (resolver, capability-from-cache and history
filter), `src/document_processor.py::_resolve_vl_model` (every VLM caller goes
through it), `src/llm_core.py::_vision_filter_for_route` (per-request filter).

## Which VLM answers

One resolver serves chat attachments, tool-result images (screenshots, a
`read_file` on a picture), `inspect_image`, the gallery and the PDF helpers.

1. **Configured** (`source: "configured"`)
   - `vision_endpoint_id` set: `vision_model` is resolved on that endpoint.
     With no model, the endpoint's vision-capable model is used (server
     report first, then known names).
   - Only `vision_model` set: resolved across the user's endpoints as before
     (`"model"` or `"model@Endpoint name"`).
   - A configured model that cannot be resolved is reported, never silently
     replaced by an auto-detected one.
2. **Auto** (`source: "auto"`), when nothing is configured:
   1. a model the user's **local** endpoints report as vision-capable: Ollama
      `/api/show` capabilities, llama.cpp `/props` `modalities.vision`, LM
      Studio `capabilities.vision`;
   2. a known VLM name among the endpoints' cached model lists, local
      endpoints first: `qwen3-vl`, `qwen2.5vl`, `qwen2.5-vl`, `gemma3`,
      `llama3.2-vision`, `minicpm-v`, `moondream`, `llava`, `pixtral`,
      `qwen2-vl`, then the hosted names (`gpt-4o`, `gpt-4.1`, `gemini-…`);
   3. the older live lookup of the same names, only when an endpoint has no
      cached model list.

   An endpoint the privacy profile refuses for `ocr_vision` (for example a
   cloud endpoint under `local_only`) is never chosen by auto-detection. The
   auto result is cached per user for 60 s (a miss for 20 s).
3. **None** (`source: "none"`): no VLM is available. Tool images then stay a
   one-line note; attachments carry the reason in brackets.

The call itself still goes through `assert_outbound("ocr_vision", …)` and the
`vision_model_fallbacks` chain, as before.

## What a text-only model receives

- **New attachments**: the description (cached in `uploads/.vision/<id>.txt`,
  editable from the attachment menu) is added to the message. The VLM call
  runs in a worker thread, so a slow CPU model does not stall other requests.
- **Tool images**: the description the VLM wrote for the image, with a hint to
  use `inspect_image` for details.
- **Images already in the conversation** (sent while a vision model was
  active, or a fallback to a text-only model mid-turn): before every request —
  plain chat and each agent route, fallbacks included — image blocks become
  `"[Image: <name> — described for a model without vision]\n<description>"`
  when a cached description exists, otherwise
  `"[image: <name> — not shown, model has no vision]"`. The saved conversation
  is not changed; switching back to a vision model sends the images again.

## Settings

| Key | Default | Per user | Meaning |
|---|---|---|---|
| `vision_enabled` | `true` | yes | Image analysis on/off. |
| `vision_model` | `""` | yes | Configured VLM; empty = auto. |
| `vision_endpoint_id` | `""` | yes | Endpoint `vision_model` is resolved on; empty = any. |
| `vision_model_fallbacks` | `[]` | yes | Ordered `{endpoint_id, model}` chain tried when the VLM fails. |
| `vision_history_filter` | `true` | no | Replace image blocks for text-only routes (above). |

Studio: Settings → Default AI → *Vision model* (endpoint + model pair) with a
status line fed by `GET /api/vision/status`.

## `GET /api/vision/status`

Authenticated; resolved with the caller's own endpoints and preferences.

```json
{
  "enabled": true,
  "source": "auto",
  "model": "qwen2.5vl:7b",
  "endpoint_id": "ep_123",
  "endpoint_name": "Local Ollama",
  "local": true,
  "configured_model": "",
  "configured_endpoint_id": "",
  "error": ""
}
```

`source` is `configured`, `auto` or `none`; with `none`, `error` says why.
Credentials are never included.

## `models_vision` in `GET /api/models`

Each endpoint item carries `models_vision`: the subset of its `models` and
`models_extra` known to accept images.

```json
{
  "endpoint_id": "ep_123",
  "models": ["qwen3:8b", "qwen2.5vl:7b", "gemma3:1b"],
  "models_extra": [],
  "models_vision": ["qwen2.5vl:7b"]
}
```

It is computed only from what is already known — the Ollama capability cache,
the LM Studio and llama.cpp probe caches, then the model-name heuristic — so
the listing makes no extra network call. A server report wins over the name
(a `gemma3:1b` that Ollama reports as text-only is left out). Offline
endpoints have `models_vision: []`. A client looking for a VLM (for example a
plugin) can pick from this list, or ask `GET /api/vision/status` for the one
this instance would use.
