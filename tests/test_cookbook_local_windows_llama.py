"""Serving llama.cpp on this machine when this machine is Windows.

The command builder is the interface's (studio/src/lib/cookbook/serve.ts,
pinned by tests/test_studio_serve_js.py); these are the server-side halves it
depends on, and each was a real failure:

  - a local Windows launch must NOT be rewritten into the source bootstrap
    the remote path uses, because there is no build toolchain there;
  - the PATH it runs under has to include the user's own wrapper and the
    CUDA and Debug build outputs, or `llama-server` is simply not found;
  - the process check has to know the binary is called `llama-server.exe`.

Source-level, because these are shell strings assembled inside a route: there
is no seam to call. If one of these assertions goes red because the line was
deliberately reworded, read the assertion — it says what the string is for.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTES_SRC = ROOT / "routes" / "cookbook_routes.py"
HELPERS_SRC = ROOT / "routes" / "cookbook_helpers.py"


def test_local_windows_llama_server_skips_the_source_bootstrap():
    routes = ROUTES_SRC.read_text(encoding="utf-8")
    assert 'local_windows_llama_cmd = local_windows and ("llama_cpp" in req.cmd or "llama-server" in req.cmd)' in routes
    assert 'if ("llama_cpp" in req.cmd or "llama-server" in req.cmd) and not local_windows_llama_cmd:' in routes


def test_local_windows_llama_server_path_includes_the_wrapper_and_cuda_builds():
    routes = ROUTES_SRC.read_text(encoding="utf-8")
    assert "if local_windows:" in routes
    assert (
        'export PATH="$HOME/bin:$HOME/llama.cpp/build-cuda/bin/Release:'
        '$HOME/llama.cpp/build/bin/Release:$HOME/llama.cpp/build/bin/Debug:'
        '$HOME/llama.cpp/build/bin:$PATH"'
    ) in routes


def test_the_process_check_knows_the_windows_binary_name():
    assert '"llama-server.exe"' in HELPERS_SRC.read_text(encoding="utf-8")
