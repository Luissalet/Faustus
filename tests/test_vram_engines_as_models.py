"""What a llama-server engine holds on each card counts as a model in the
VRAM bars, not as "other"."""
from routes.local_models_routes import _count_engines_as_models

MB = 1048576


def test_engine_share_moves_from_other_to_models():
    result = {"gpus": [
        {"index": 0, "used_bytes": 20000 * MB, "models_bytes": None, "other_bytes": 20000 * MB,
         "processes": [{"pid": 1, "label": "llama-server", "used_mb": 19000, "kind": "model"},
                       {"pid": 2, "label": "chrome.exe", "used_mb": 500, "kind": "other"}]},
        {"index": 1, "used_bytes": 3000 * MB, "models_bytes": 2000 * MB, "other_bytes": 1000 * MB,
         "processes": [{"pid": 3, "label": "Ollama", "used_mb": 2000, "kind": "model"}]},
    ]}
    _count_engines_as_models(result)
    first, second = result["gpus"]
    assert first["models_bytes"] == 19000 * MB and first["other_bytes"] == 1000 * MB
    assert second["models_bytes"] == 2000 * MB and second["other_bytes"] == 1000 * MB
    assert result["engines_bytes"] == 19000 * MB


def test_unknown_per_process_use_changes_nothing():
    result = {"gpus": [{"index": 0, "used_bytes": 5 * MB, "models_bytes": None, "other_bytes": 5 * MB,
                        "processes": [{"pid": 1, "label": "llama-server", "used_mb": None, "kind": "model"}]}]}
    _count_engines_as_models(result)
    assert result["gpus"][0]["models_bytes"] is None and "engines_bytes" not in result
