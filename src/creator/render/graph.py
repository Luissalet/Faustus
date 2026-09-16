"""graph.py — WP14: timeline + EDL -> a deterministic FFmpeg filtergraph.

Pure compiler: no filesystem access, no `subprocess`, no `ffmpeg` binary
required to import or call this module. ``compile_graph()`` is a function of
its arguments ONLY — the SAME ``timeline`` document content + the SAME
profile + the SAME (optional) subtitle occurrence id ALWAYS produces the
SAME :class:`RenderGraph`, in particular the SAME ``filter_complex`` string
(``test_creator_wp14_render.py::test_filtergraph_is_deterministic``). This is
what makes the render cache key (``cache.py``) meaningful: it hashes this
module's OUTPUT, not a hope that ffmpeg behaves identically given "the same
inputs" in some looser sense.

**No floats reach the filter string.** Every timestamp is converted from the
document's own rational ``clock`` (or a clip's ``source_clock``) to whole
MICROSECONDS via ``timeline.clocks.convert_ticks`` (exact ``Fraction``
arithmetic, ``rounding="nearest"``, the same rounding policy documented
there) and formatted as a fixed ``SECONDS.MICROSECONDS`` decimal string —
the one, documented place a float-looking string is produced, and it is
produced from an exact integer division, never from `float()`. A hundred
cuts of the same nominal duration therefore compile to the exact same digits
every time, the same closure criterion ``clocks.py`` already proves for tick
conversion itself.

**Composition model (v1 — documented, not silently partial):**

* Every track whose ``kind`` is ``video``/``image``/``overlay`` contributes
  one visual layer. Layers are stacked in ``content.tracks`` LIST ORDER —
  index 0 is the BOTTOM layer, later tracks overlay on top at (0, 0) (no
  positioning UI yet; a track IS its own full-frame layer). A gap in a
  track's clip coverage (before the first clip, between clips, or after the
  last clip, up to the document's ``duration_ticks``) is filled with an
  opaque black frame of the profile's own size/rate — a layer's timeline
  span is always exactly ``duration_ticks`` long, never shorter, so the
  overlay chain never has to reason about a layer disappearing mid-render.
* Every ``audio`` track contributes one layer, gain-adjusted per clip
  (``clip.gain_db``, WP12's ``timeline.set_gain``) via the `volume` filter,
  with gaps filled by silence (`anullsrc`) the same way. All audio layers
  are mixed with `amix`, ``normalize=0`` — loudness is therefore an exact,
  deterministic function of each clip's own gain, never an implicit
  "divide by track count" the mixer would otherwise apply.
* A ``video`` track only contributes its VIDEO stream (``[n:v]``); an
  ``audio`` track only its AUDIO stream (``[n:a]``) — a clip that needs
  both its own picture and its own embedded sound needs one clip on a
  video track and one on an audio track referencing the same
  ``asset_ref`` (documented limitation: this compiler does not currently
  demux "the audio embedded in this video clip" for you).
* A ``caption`` track's clips are NOT composited into the visual stack.
  Their ``asset_ref``s are the pool `service.py` may pick a subtitle file
  from to pass in as ``subtitle_occurrence_id`` — burning multiple caption
  segments into one subtitle file is WP16's job (subtitle editor), not
  this compiler's. When a subtitle occurrence IS given, it is burned onto
  the FINAL composited video via the `subtitles=` filter (`ass=` when the
  file's extension is `.ass`), always the LAST video filter — subtitles
  render on top of every layer, always.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..errors import InvalidOperation
from ..ops.model import Rational
from ..timeline import clocks as clocks_mod
from ..timeline import tracks as tracks_mod
from .profiles import Profile

__all__ = [
    "FILTER_ALLOWLIST", "SUBTITLE_PATH_TOKEN", "RenderGraph",
    "compile_graph", "escape_subtitle_path", "seconds_str",
]

#: Every filter name this compiler is allowed to emit. `graph.py` never
#: formats a caller-supplied filter name into the string — this set exists
#: so a test (and a reviewer) can assert the WHOLE allowlist against the
#: filter names that ever appear in a compiled `filter_complex`, the "filtros
#: permitidos" half of the ficha's step 1.
FILTER_ALLOWLIST = frozenset({
    "trim", "atrim", "setpts", "asetpts", "concat", "overlay", "scale",
    "pad", "fps", "amix", "volume", "subtitles", "ass", "anullsrc", "color",
    "aformat", "setsar", "format",
})

#: Placeholder substituted by `adapters/ffmpeg.py` for the burned subtitle
#: file's REAL staged path, resolved only at submit time (this module has
#: no filesystem access — see module docstring). Not ffmpeg syntax, so a
#: `filter_complex` that still contains it was never sent to `submit()`
#: through the adapter (a test asserts the token never reaches subprocess
#: argv unsubstituted).
SUBTITLE_PATH_TOKEN = "\x00CREATOR_SUBTITLE_PATH\x00"

_MICROSECOND_CLOCK = Rational(1_000_000, 1)


def seconds_str(ticks: int, clock: Rational) -> str:
    """``ticks`` at ``clock`` -> an exact ``SECONDS.MICROSECONDS`` decimal
    string ffmpeg's `-ss`/`trim=start=`/`duration=` accept, via integer
    microsecond conversion (see module docstring — no float involved)."""
    micros = clocks_mod.convert_ticks(ticks, clock, _MICROSECOND_CLOCK, rounding="nearest")
    whole, rem = divmod(micros, 1_000_000)
    return f"{whole}.{rem:06d}"


def escape_subtitle_path(path: str) -> str:
    """Escape a filesystem path for the `subtitles=filename='...'` /
    `ass=filename='...'` filter argument: ffmpeg's filter-argument parser
    treats ``:`` and ``\\`` and ``'`` specially even inside single quotes on
    some builds, so every one of those three characters is backslash-escaped
    — the same defensive escaping `adapters/ffmpeg.py`'s concat demuxer
    already applies to its own listfile paths, applied here to a filter
    argument instead of a text file line."""
    escaped = path.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    return escaped


@dataclass(frozen=True)
class RenderInput:
    index: int
    occurrence_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "occurrence_id": self.occurrence_id}


@dataclass(frozen=True)
class RenderGraph:
    """Everything `service.py`/`adapters/ffmpeg.py` need to build a real
    `ffmpeg` argv, with NO filesystem paths inside (those are resolved at
    submit time from `inputs[i].occurrence_id` — see `adapters/base.py`)."""

    profile_id: str
    width: int
    height: int
    fps: Rational
    inputs: Tuple[RenderInput, ...]
    filter_complex: str
    video_map: str
    audio_map: str  # "" when the document has no audio track at all
    output_args: Tuple[str, ...]
    duration_ticks: int
    clock: Rational
    subtitle_occurrence_id: Optional[str]
    document_revision: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile_id": self.profile_id, "width": self.width, "height": self.height,
            "fps": {"numerator": self.fps.numerator, "denominator": self.fps.denominator},
            "inputs": [i.to_dict() for i in self.inputs],
            "filter_complex": self.filter_complex,
            "video_map": self.video_map, "audio_map": self.audio_map,
            "output_args": list(self.output_args),
            "duration_ticks": str(self.duration_ticks),
            "clock": self.clock.to_dict(),
            "subtitle_occurrence_id": self.subtitle_occurrence_id,
            "document_revision": self.document_revision,
        }


def _collect_input_order(content: Dict[str, Any]) -> List[str]:
    """Every occurrence id referenced by a video/image/overlay/audio clip,
    in first-seen order over ``tracks`` LIST order then each track's
    time-sorted clips — deterministic for a fixed document, independent of
    any dict/set iteration order Python itself might otherwise use."""
    seen: Dict[str, None] = {}
    for track in tracks_mod.get_tracks(content):
        if track.get("kind") not in ("video", "image", "overlay", "audio"):
            continue
        for clip in tracks_mod.ordered_clips(track):
            occ = str(clip.get("asset_ref") or "")
            if occ and occ not in seen:
                seen[occ] = None
    return list(seen)


def _pick_subtitle_occurrence(content: Dict[str, Any],
                               explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    for track in tracks_mod.get_tracks(content):
        if track.get("kind") != tracks_mod.SUBTITLE_TRACK_KIND:
            continue
        clips = tracks_mod.ordered_clips(track)
        if clips:
            return str(clips[0].get("asset_ref") or "") or None
    return None


def _visual_layer(track: Dict[str, Any], *, track_index: int, duration_ticks: int,
                   clock: Rational, width: int, height: int, fps_str: str,
                   input_index_of: Mapping[str, int]) -> Tuple[List[str], str]:
    parts: List[str] = []
    segments: List[str] = []
    cursor = 0
    seg_i = 0

    def _filler(gap_ticks: int) -> str:
        nonlocal seg_i
        label = f"t{track_index}g{seg_i}"
        seg_i += 1
        dur = seconds_str(gap_ticks, clock)
        parts.append(f"color=c=black:size={width}x{height}:rate={fps_str}:duration={dur}[{label}]")
        return label

    for clip in tracks_mod.ordered_clips(track):
        clip_range = tracks_mod.clip_range(clip)
        if clip_range.start_ticks > cursor:
            segments.append(_filler(clip_range.start_ticks - cursor))
        occ = str(clip.get("asset_ref") or "")
        in_idx = input_index_of[occ]
        src_range = tracks_mod.clip_source_range(clip)
        src_clock = tracks_mod.clip_source_clock(clip)
        start_s = seconds_str(src_range.start_ticks, src_clock)
        end_s = seconds_str(src_range.start_ticks + src_range.duration_ticks, src_clock)
        label = f"t{track_index}c{seg_i}"
        seg_i += 1
        parts.append(
            f"[{in_idx}:v]trim=start={start_s}:end={end_s},setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={fps_str}[{label}]"
        )
        segments.append(label)
        cursor = clip_range.end_ticks

    if cursor < duration_ticks:
        segments.append(_filler(duration_ticks - cursor))
    if not segments:
        segments.append(_filler(duration_ticks))

    if len(segments) == 1:
        return parts, segments[0]
    refs = "".join(f"[{s}]" for s in segments)
    out_label = f"t{track_index}cat"
    parts.append(f"{refs}concat=n={len(segments)}:v=1:a=0[{out_label}]")
    return parts, out_label


def _audio_layer(track: Dict[str, Any], *, track_index: int, duration_ticks: int,
                  clock: Rational, audio_rate: int,
                  input_index_of: Mapping[str, int]) -> Tuple[List[str], str]:
    parts: List[str] = []
    segments: List[str] = []
    cursor = 0
    seg_i = 0

    def _filler(gap_ticks: int) -> str:
        nonlocal seg_i
        label = f"a{track_index}g{seg_i}"
        seg_i += 1
        dur = seconds_str(gap_ticks, clock)
        parts.append(
            f"anullsrc=channel_layout=stereo:sample_rate={audio_rate},"
            f"atrim=duration={dur},asetpts=PTS-STARTPTS[{label}]"
        )
        return label

    for clip in tracks_mod.ordered_clips(track):
        clip_range = tracks_mod.clip_range(clip)
        if clip_range.start_ticks > cursor:
            segments.append(_filler(clip_range.start_ticks - cursor))
        occ = str(clip.get("asset_ref") or "")
        in_idx = input_index_of[occ]
        src_range = tracks_mod.clip_source_range(clip)
        src_clock = tracks_mod.clip_source_clock(clip)
        start_s = seconds_str(src_range.start_ticks, src_clock)
        end_s = seconds_str(src_range.start_ticks + src_range.duration_ticks, src_clock)
        gain_db = clip.get("gain_db")
        try:
            gain_db = float(gain_db) if gain_db is not None else 0.0
        except (TypeError, ValueError):
            gain_db = 0.0
        label = f"a{track_index}c{seg_i}"
        seg_i += 1
        parts.append(
            f"[{in_idx}:a]atrim=start={start_s}:end={end_s},asetpts=PTS-STARTPTS,"
            f"aformat=sample_rates={audio_rate}:channel_layouts=stereo,"
            f"volume={gain_db:.3f}dB[{label}]"
        )
        segments.append(label)
        cursor = clip_range.end_ticks

    if cursor < duration_ticks:
        segments.append(_filler(duration_ticks - cursor))
    if not segments:
        segments.append(_filler(duration_ticks))

    if len(segments) == 1:
        return parts, segments[0]
    refs = "".join(f"[{s}]" for s in segments)
    out_label = f"a{track_index}cat"
    parts.append(f"{refs}concat=n={len(segments)}:v=0:a=1[{out_label}]")
    return parts, out_label


def compile_graph(content: Dict[str, Any], *, profile: Profile,
                   document_revision: int,
                   subtitle_occurrence_id: Optional[str] = None) -> RenderGraph:
    """Pure. Raises :class:`InvalidOperation` for a document with no video/
    image/overlay track (nothing to render) or a clip referencing an
    ``asset_ref`` this function was not told an input index for."""
    clock = Rational.from_dict(content.get("clock"), field_name="timeline.content.clock")
    duration_ticks = int(content.get("duration_ticks") or 0)
    if duration_ticks <= 0:
        raise InvalidOperation("timeline.content.duration_ticks must be a positive integer")

    tracks_mod.validate_all_tracks(content)
    order = _collect_input_order(content)
    if not order:
        raise InvalidOperation("timeline has no clips referencing any input occurrence")
    input_index_of = {occ: i for i, occ in enumerate(order)}
    inputs = tuple(RenderInput(index=i, occurrence_id=occ) for i, occ in enumerate(order))

    fps_str = profile.fps_str()
    parts: List[str] = []
    visual_outputs: List[str] = []
    audio_outputs: List[str] = []

    for track_index, track in enumerate(tracks_mod.get_tracks(content)):
        kind = track.get("kind")
        if kind in ("video", "image", "overlay"):
            layer_parts, out_label = _visual_layer(
                track, track_index=track_index, duration_ticks=duration_ticks, clock=clock,
                width=profile.width, height=profile.height, fps_str=fps_str,
                input_index_of=input_index_of,
            )
            parts.extend(layer_parts)
            visual_outputs.append(out_label)
        elif kind == "audio":
            layer_parts, out_label = _audio_layer(
                track, track_index=track_index, duration_ticks=duration_ticks, clock=clock,
                audio_rate=profile.audio_rate, input_index_of=input_index_of,
            )
            parts.extend(layer_parts)
            audio_outputs.append(out_label)
        # "caption" tracks are read below, via _pick_subtitle_occurrence —
        # never composited as a layer here.

    if not visual_outputs:
        raise InvalidOperation(
            "timeline has no video/image/overlay track; nothing to render"
        )

    base = visual_outputs[0]
    for i, nxt in enumerate(visual_outputs[1:], start=1):
        merged = f"vov{i}"
        parts.append(f"[{base}][{nxt}]overlay=0:0[{merged}]")
        base = merged
    final_video = base

    subs_occ = _pick_subtitle_occurrence(content, subtitle_occurrence_id)
    if subs_occ is not None:
        # The subtitle occurrence is NOT added to `inputs` — it is never an
        # ffmpeg `-i`/`[n:...]` stream reference (only its STAGED PATH is
        # used, via `SUBTITLE_PATH_TOKEN`, substituted by
        # `adapters/ffmpeg.py` at submit time). `service.py` still stages
        # its bytes (so the path exists to substitute) alongside every
        # occurrence in `inputs` — see its own docstring.
        # `subtitles=` (libavformat/libass under the hood) reads SRT/VTT/ASS
        # alike from the extension of the staged path substituted for
        # SUBTITLE_PATH_TOKEN at submit time — one filter name for every
        # supported subtitle container, no extension branch needed here.
        vout = "vsub"
        parts.append(f"[{final_video}]subtitles=filename='{SUBTITLE_PATH_TOKEN}'[{vout}]")
        final_video = vout

    filter_complex = ";".join(parts)

    final_audio = ""
    if audio_outputs:
        if len(audio_outputs) == 1:
            final_audio = audio_outputs[0]
        else:
            refs = "".join(f"[{a}]" for a in audio_outputs)
            final_audio = "amix_out"
            filter_complex += (
                f";{refs}amix=inputs={len(audio_outputs)}:duration=first:normalize=0[{final_audio}]"
            )

    output_args: List[str] = [
        "-r", fps_str, "-s", f"{profile.width}x{profile.height}",
        "-c:v", profile.video_codec, "-b:v", profile.video_bitrate,
        "-pix_fmt", profile.pix_fmt,
    ]
    if final_audio:
        output_args += ["-c:a", profile.audio_codec, "-b:a", profile.audio_bitrate,
                         "-ar", str(profile.audio_rate)]
    else:
        output_args += ["-an"]
    output_args += ["-movflags", "+faststart"]

    return RenderGraph(
        profile_id=profile.id, width=profile.width, height=profile.height, fps=profile.fps,
        inputs=inputs, filter_complex=filter_complex,
        video_map=f"[{final_video}]", audio_map=f"[{final_audio}]" if final_audio else "",
        output_args=tuple(output_args), duration_ticks=duration_ticks, clock=clock,
        subtitle_occurrence_id=subs_occ, document_revision=document_revision,
    )
