"""`youtube` channel: (1) Faustus's existing `services/youtube/youtube_handler.py`
(transcript + yt-dlp comments), (2) `youtube-transcript-api` directly (when
installed but the handler's global init hasn't run), (3) Jina Reader on the
watch page as a last resort (title/description only, no transcript).
"""
from __future__ import annotations

from typing import Any

from src.reach.base import Availability, Backend, Channel, ReachBackendError, ReachResult
from src.reach.http_client import make_client
from src.reach.web import JinaReaderBackend, _normalize_url


def _video_id(url_or_id: str) -> str:
    from services.youtube.youtube_handler import extract_youtube_id, is_youtube_url

    raw = (url_or_id or "").strip()
    if not raw:
        raise ReachBackendError("empty url/id")
    if is_youtube_url(raw):
        vid = extract_youtube_id(raw)
        if not vid:
            raise ReachBackendError(f"could not extract a video id from {raw!r}")
        return vid
    # Bare id (11 chars, no scheme/slash) is accepted directly.
    if "/" not in raw and "://" not in raw:
        return raw
    raise ReachBackendError(f"not a recognizable YouTube URL: {raw!r}")


class FaustusYoutubeHandlerBackend(Backend):
    """Reuses `services/youtube/youtube_handler.py` end to end: transcript
    via `youtube_transcript_api` (through `init_youtube`/`extract_transcript_async`)
    plus top comments via `yt-dlp`."""

    name = "faustus_youtube_handler"

    async def _probe(self, live: bool) -> Availability:
        from services.youtube.youtube_handler import init_youtube, YOUTUBE_AVAILABLE

        init_youtube()
        from services.youtube import youtube_handler as _yh

        if not _yh.YOUTUBE_AVAILABLE:
            return Availability(status="needs_config", reason="youtube_transcript_api not installed", checked_live=live)
        return Availability(status="ready", reason="youtube_transcript_api available", checked_live=live)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        from services.youtube.youtube_handler import (
            init_youtube, extract_transcript_async, fetch_youtube_comments,
        )

        video_id = _video_id(url_or_id)
        init_youtube()
        transcript = await extract_transcript_async(url_or_id, video_id)
        want_comments = bool(kwargs.get("comments", True))
        comments_data = {"success": False, "comments": []}
        if want_comments:
            try:
                comments_data = await fetch_youtube_comments(video_id, max_comments=int(kwargs.get("max_comments", 25)))
            except Exception:
                comments_data = {"success": False, "comments": []}

        if not transcript.get("success") and not comments_data.get("comments"):
            raise ReachBackendError(transcript.get("error") or "no transcript or comments available")

        items = [
            {"author": c.get("author", ""), "text": c.get("text", ""), "score": c.get("likes", 0), "at": ""}
            for c in comments_data.get("comments", [])
        ]
        title = comments_data.get("title", "")
        url = f"https://www.youtube.com/watch?v={video_id}"
        return ReachResult(
            channel="youtube", url=url, title=title, author=comments_data.get("channel", ""),
            text=transcript.get("transcript") or "", items=items,
            lang=transcript.get("language") or "", source_trust="public_api",
        )


class YoutubeTranscriptApiBackend(Backend):
    """Calls `youtube_transcript_api` directly, independent of the handler's
    module-global init -- covers the case where the package is installed but
    `init_youtube()` failed for an unrelated reason."""

    name = "youtube_transcript_api"

    async def _probe(self, live: bool) -> Availability:
        try:
            import youtube_transcript_api  # noqa: F401
        except ImportError:
            return Availability(status="needs_config", reason="pip install youtube-transcript-api", checked_live=live)
        return Availability(status="ready", checked_live=live)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError as exc:
            raise ReachBackendError("youtube-transcript-api not installed") from exc
        import asyncio

        video_id = _video_id(url_or_id)

        def _fetch():
            api = YouTubeTranscriptApi()
            return list(api.fetch(video_id))

        try:
            snippets = await asyncio.to_thread(_fetch)
        except Exception as exc:  # noqa: BLE001
            raise ReachBackendError(str(exc)) from exc
        text = " ".join((getattr(s, "text", "") or "").strip() for s in snippets).strip()
        if not text:
            raise ReachBackendError("empty transcript")
        return ReachResult(
            channel="youtube", url=f"https://www.youtube.com/watch?v={video_id}",
            text=text, source_trust="public_api",
        )


class YoutubeJinaBackend(Backend):
    """Last resort: Jina Reader on the watch page -- title/description only,
    no transcript, but better than a hard failure."""

    name = "jina_reader"

    async def _probe(self, live: bool) -> Availability:
        return await JinaReaderBackend()._probe(live)

    async def read(self, url_or_id: str, **kwargs: Any) -> ReachResult:
        video_id = _video_id(url_or_id)
        url = f"https://www.youtube.com/watch?v={video_id}"
        result = await JinaReaderBackend().read(url)
        result.channel = "youtube"
        return result


class YoutubeChannel(Channel):
    name = "youtube"
    backends = [FaustusYoutubeHandlerBackend(), YoutubeTranscriptApiBackend(), YoutubeJinaBackend()]
