"""Start a headless, single-user Faustus server inside a Linux task container.

Used by the Harbor agent adapter (``faustus_agent.py``): the adapter copies a
self-contained Python runtime and this source tree to ``/opt/faustus``, then
runs this script in the background and drives the agent with
``scripts/faustus_run.py``.

    python serve_headless.py --port 7000 --data /tmp/faustus-data \
        --endpoint-url http://host:8002/v1 --model my-model

What it does before serving:

* sign-in off (one local user, the container is the sandbox), memory,
  retrieval and start-up warm-ups off, no connection to a vector store;
* registers the model endpoint given on the command line (or in
  ``FAUSTUS_ENDPOINT_URL`` / ``OPENAI_BASE_URL``) and makes it the default,
  with tool calling on;
* writes nothing outside ``--data``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_ROOT = Path(os.environ.get("FAUSTUS_APP_ROOT") or HERE.parent.parent)


def _args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=int(os.environ.get("APP_PORT", "7000")))
    ap.add_argument("--data", default=os.environ.get("FAUSTUS_DATA_DIR", "/tmp/faustus-data"))
    ap.add_argument("--endpoint-url", default=os.environ.get("FAUSTUS_ENDPOINT_URL") or os.environ.get("OPENAI_BASE_URL") or "")
    ap.add_argument("--model", default=os.environ.get("FAUSTUS_MODEL", ""))
    ap.add_argument("--api-key", default=os.environ.get("FAUSTUS_ENDPOINT_KEY") or os.environ.get("OPENAI_API_KEY") or "")
    ap.add_argument("--settings", default=os.environ.get("FAUSTUS_SETTINGS_JSON", ""),
                    help="JSON object merged into settings.json before start")
    return ap.parse_args(argv)


def _write_settings(path: Path, updates: dict) -> None:
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    settings.update(updates)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv=None) -> int:
    args = _args(argv)
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    settings_path = data / "settings.json"
    base_settings = {"warm_default_model": False, "memory_enabled": False, "auto_extract_memory": False,
                     "rag_enabled": False, "agent_auto_review": False}
    if args.settings:
        base_settings.update(json.loads(args.settings))
    _write_settings(settings_path, base_settings)

    os.environ.update(
        AUTH_ENABLED="false", ODYSSEUS_DATA_DIR=str(data), DATABASE_URL="sqlite:///" + (data / "app.db").as_posix(),
        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", CUDA_VISIBLE_DEVICES="", CHROMADB_HOST="127.0.0.1",
        CHROMADB_PORT="1", HOARD_HUB_URL="http://127.0.0.1:1", HOARD_EVENTS="0", HOARD_NOTIFY_DRY_RUN="1",
        ODYSSEUS_STARTUP_WARMUPS="0", ODYSSEUS_TOOL_INDEX_WARMUP="0", APP_PORT=str(args.port),
    )
    os.environ.setdefault("ODYSSEUS_INTERNAL_TOKEN", uuid.uuid4().hex)
    # No built-in MCP server by default: each is a process of its own, and the
    # agent's coding tools are native (src/builtin_mcp.py, FAUSTUS_BUILTIN_MCP).
    os.environ.setdefault("FAUSTUS_BUILTIN_MCP", "none")
    sys.path.insert(0, str(APP_ROOT))
    os.chdir(APP_ROOT)

    import src.chroma_client  # noqa: E402

    def _no_vector_store(*_a, **_k):
        raise RuntimeError("the headless agent server has no vector store")

    src.chroma_client.get_chroma_client = _no_vector_store
    import app  # noqa: E402
    from core.database import SessionLocal, ModelEndpoint  # noqa: E402

    if args.endpoint_url:
        base = args.endpoint_url.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        models = [args.model] if args.model else []
        with SessionLocal() as db:
            row = db.query(ModelEndpoint).filter(ModelEndpoint.id == "headless").first()
            if row is None:
                row = ModelEndpoint(id="headless", name="Agent endpoint", base_url=base)
                db.add(row)
            row.base_url = base
            row.is_enabled = True
            row.api_key = args.api_key or None
            row.endpoint_kind = "local"
            row.model_refresh_mode = "manual"
            row.supports_tools = True
            row.cached_models = json.dumps(models)
            row.pinned_models = json.dumps(models)
            db.commit()
        updates = {"default_endpoint_id": "headless"}
        if args.model:
            updates["default_model"] = args.model
        _write_settings(settings_path, updates)

    import uvicorn  # noqa: E402

    uvicorn.run(app.app, host="127.0.0.1", port=args.port, lifespan="on", log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
