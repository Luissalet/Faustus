"""profiles.py — WP14: explicit output profiles for the timeline renderer.

Every profile fully names the encoder decisions a "silent quality change"
would otherwise hide: resolution, frame rate, aspect, video codec/bitrate/
pixel format, audio codec/bitrate/sample rate, and the container extension.
VID12's acceptance criterion ("un encoder ausente provoca cambio aprobado o
bloqueo, no otra calidad de salida silenciosa") is enforced by
``service.py`` checking the requested profile's ``video_codec``/
``audio_codec`` against ``ffmpeg -encoders`` before a render starts — this
module only carries the fixed, versioned catalogue of what a profile MEANS.

Nothing here reads the filesystem or spawns a process: a `Profile` is a
plain, hashable fact used both to build the filter graph (``graph.py``) and
to compute the render cache key (``cache.py``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Tuple

from ..errors import InvalidOperation
from ..ops.model import Rational


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    width: int
    height: int
    fps: Rational
    video_codec: str
    video_bitrate: str  # ffmpeg -b:v value, e.g. "8M"
    pix_fmt: str
    audio_codec: str
    audio_bitrate: str  # ffmpeg -b:a value, e.g. "192k"
    audio_rate: int
    container_ext: str

    def fps_str(self) -> str:
        return f"{self.fps.numerator}/{self.fps.denominator}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "width": self.width,
            "height": self.height,
            "fps": {"numerator": self.fps.numerator, "denominator": self.fps.denominator},
            "video_codec": self.video_codec, "video_bitrate": self.video_bitrate,
            "pix_fmt": self.pix_fmt, "audio_codec": self.audio_codec,
            "audio_bitrate": self.audio_bitrate, "audio_rate": self.audio_rate,
            "container_ext": self.container_ext,
        }

    def cache_fingerprint(self) -> Tuple[Any, ...]:
        """A stable, order-independent tuple every field of this profile
        maps to — the exact thing ``cache.py`` folds into a render's cache
        key (never the whole object's ``repr``, which is not a documented
        format)."""
        return (
            self.id, self.width, self.height, self.fps.numerator, self.fps.denominator,
            self.video_codec, self.video_bitrate, self.pix_fmt,
            self.audio_codec, self.audio_bitrate, self.audio_rate, self.container_ext,
        )


#: YouTube 16:9 1080p/4K, Shorts/Reels/TikTok 9:16, Instagram 1:1,
#: LinkedIn 16:9, Cinema 21:9 — the ficha's exact list. `id` is the wire/
#: cache-key value; never renamed once shipped (additive catalogue only).
_PROFILES: Tuple[Profile, ...] = (
    Profile("youtube_1080p", "YouTube 1080p (16:9)", 1920, 1080, Rational(30, 1),
            "libx264", "8M", "yuv420p", "aac", "192k", 48000, "mp4"),
    Profile("youtube_4k", "YouTube 4K (16:9)", 3840, 2160, Rational(30, 1),
            "libx264", "35M", "yuv420p", "aac", "192k", 48000, "mp4"),
    Profile("shorts_9x16", "YouTube Shorts (9:16)", 1080, 1920, Rational(30, 1),
            "libx264", "6M", "yuv420p", "aac", "128k", 48000, "mp4"),
    Profile("reels_9x16", "Instagram Reels (9:16)", 1080, 1920, Rational(30, 1),
            "libx264", "6M", "yuv420p", "aac", "128k", 48000, "mp4"),
    Profile("tiktok_9x16", "TikTok (9:16)", 1080, 1920, Rational(30, 1),
            "libx264", "6M", "yuv420p", "aac", "128k", 48000, "mp4"),
    Profile("instagram_1x1", "Instagram Feed (1:1)", 1080, 1080, Rational(30, 1),
            "libx264", "5M", "yuv420p", "aac", "128k", 48000, "mp4"),
    Profile("linkedin_16x9", "LinkedIn (16:9)", 1920, 1080, Rational(30, 1),
            "libx264", "6M", "yuv420p", "aac", "128k", 48000, "mp4"),
    Profile("cinema_21x9", "Cinema (21:9)", 2560, 1080, Rational(24, 1),
            "libx264", "20M", "yuv420p", "aac", "192k", 48000, "mp4"),
)

_BY_ID: Dict[str, Profile] = {p.id: p for p in _PROFILES}


def list_profiles() -> Tuple[Profile, ...]:
    return _PROFILES


def get_profile(profile_id: str) -> Profile:
    profile = _BY_ID.get(str(profile_id or "").strip())
    if profile is None:
        raise InvalidOperation(
            f"unknown render profile {profile_id!r}; known profiles: "
            f"{', '.join(sorted(_BY_ID))}"
        )
    return profile


__all__ = ["Profile", "list_profiles", "get_profile"]
