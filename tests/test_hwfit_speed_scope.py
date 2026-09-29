"""Speed estimates expose their basis instead of implying measured throughput."""

from services.hwfit.fit import analyze_model


MODEL = {"name": "Test-7B", "parameter_count": "7B",
         "parameters_raw": 7_000_000_000, "context_length": 8192}


def test_gpu_speed_is_explicitly_advisory_and_context_independent():
    system = {"backend": "cuda", "gpu_name": "NVIDIA RTX 4090",
              "gpu_vram_gb": 24, "available_ram_gb": 64, "has_gpu": True}
    short = analyze_model(MODEL, system, target_context=2048)
    long = analyze_model(MODEL, system, target_context=8192)

    assert short["speed_estimate"] == {
        "basis": "bandwidth_heuristic", "confidence": "low",
        "measured": False, "context_depth_modeled": False}
    assert long["speed_estimate"] == short["speed_estimate"]
    assert short["speed_tps"] == long["speed_tps"]


def test_cpu_speed_declares_parameter_fallback():
    system = {"backend": "cpu_x86", "gpu_name": None, "gpu_vram_gb": 0,
              "available_ram_gb": 64, "has_gpu": False}
    result = analyze_model(MODEL, system)
    assert result["speed_estimate"]["basis"] == "parameter_fallback"
    assert result["speed_estimate"]["measured"] is False
