import os
import re
from pathlib import Path

from fastapi import HTTPException

from src.constants import GENERATED_IMAGES_DIR


GENERATED_IMAGE_DIR = Path(GENERATED_IMAGES_DIR)
# Hex names, or a canonical lowercase UUID: the Prospero studio names its
# gallery results after their gallery ID (str(uuid4())), and those files were
# refused with 400 here while the chat showed a broken image for every edit.
GENERATED_IMAGE_RE = re.compile(
    r"^(?:[a-f0-9]{8,64}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"
    r"\.(png|jpg|jpeg|webp|gif|mp4|mov|webm|mkv|m4v)$"
)
GENERATED_IMAGE_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
}


def resolve_generated_image_path(filename: str) -> Path:
    if not isinstance(filename, str) or not GENERATED_IMAGE_RE.fullmatch(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    root = GENERATED_IMAGE_DIR.resolve()
    path = (GENERATED_IMAGE_DIR / filename).resolve()
    try:
        if os.path.commonpath([str(root), str(path)]) != str(root):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Image not found")
    return path
