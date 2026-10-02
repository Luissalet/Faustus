"""Use Hoard service owners through Faustus's configured local Hub."""
from src import hoard_hub
from src.hoard_link import family, fam_web, fam_media, fam_docs
from pathlib import Path
import tempfile
import json
from urllib.parse import urlsplit


def _installed_hub_token():
    """Older installations have family MCP servers but no dedicated Hub connector."""
    try:
        from core.database import SessionLocal, McpServer
        from src.security_scan import _hoard_link_checkouts
        with SessionLocal() as session:
            servers = session.query(McpServer.name, McpServer.args, McpServer.env).filter(McpServer.is_enabled.is_(True)).all()
        paths = []
        for name, arguments, environment in servers:
            if "hoard" not in name.lower():
                continue
            env = json.loads(environment or "{}")
            explicit = env.get("HOARD_HUB_TOKEN_FILE")
            if explicit and Path(explicit).is_file():
                return str(explicit)
            for arg in json.loads(arguments or "[]"):
                if isinstance(arg, str) and Path(arg).is_absolute() and Path(arg).is_file():
                    paths.append(arg)
        for root in _hoard_link_checkouts(paths):
            token = Path(root) / "data" / "mcp-token"
            if token.is_file():
                return str(token)
    except Exception:
        return ""
    return ""


def configure():
    location = hoard_hub.hub_location()
    token_file = location.get("token_file")
    # Discovery authenticates only the default local Hub. Never attach an
    # unrelated installation's credential to a custom endpoint.
    parsed = urlsplit(location.get("url") or hoard_hub.DEFAULT_URL)
    if not token_file and parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1") and parsed.port == 8810:
        token_file = _installed_hub_token()
    return family.configure("faustus", token_file=token_file, hub=location.get("url"))


def fetch(url, **kwargs):
    configure()
    return fam_web.fetch(url, **kwargs)


def download(url, **kwargs):
    configure()
    return fam_media.download(url, **kwargs)


def transcribe(path, **kwargs):
    configure()
    return fam_media.transcribe(path, **kwargs)


def extract(path, **kwargs):
    configure()
    return fam_docs.extract(path, **kwargs)


def transcribe_bytes(data, *, language="", model=None, timeout_s=150):
    """A temporary file allows the owner to decode the original audio container."""
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as handle:
        handle.write(data)
        path = handle.name
    try:
        return transcribe(path, language=language or "auto", model=model, timeout_s=timeout_s, local_fallback=False)
    finally:
        Path(path).unlink(missing_ok=True)
