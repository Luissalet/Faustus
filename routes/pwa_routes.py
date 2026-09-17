"""routes/pwa_routes.py — manifest and service worker at the origin root (lot P-B fills this in)."""
from fastapi import APIRouter


def setup_pwa_routes() -> APIRouter:
    return APIRouter(tags=["pwa"])
