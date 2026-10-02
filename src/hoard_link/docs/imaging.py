"""Image helpers every app that touches photos re-wrote: open with the right orientation, thumbnails, EXIF, GPS
stripping, perceptual hashes, shrink-to-a-size-limit. Pillow is imported inside the functions (a missing Pillow
raises :class:`hoard_link.errors.Unavailable`, never ``ImportError`` at import); numpy is not needed.

``src`` arguments take a path (``str``/``Path``), ``bytes`` or an open binary file.

* :func:`open_oriented`, :func:`flatten_rgb`, :func:`register_heif` — the open / EXIF-rotate / normalise-mode /
  flatten-alpha sequence that was copied ~50 times.
* :func:`thumbnail` — long side ``size``, WebP by default, fast reduced JPEG decode.
* :func:`read_exif` — date with UTC offset, GPS as decimal degrees, camera, lens, ISO, exposure.
* :func:`strip_exif` — remove GPS (or everything), keeping the orientation so the picture still shows upright.
* :func:`phash` (64-bit DCT hash, the same value as ``imagehash.phash``), :func:`dhash` (Argus' 256-bit difference
  hash, hex), :func:`hamming`, :func:`content_hash` (streamed BLAKE2b).
* :func:`compress_to_limit` — make a PNG, JPEG or WebP fit a byte limit: lossless first, then quality, then size.

Replaces: Daguerre ``thumbnails.py`` / ``metadata.py`` / ``hashing.py`` / ``formats.py`` / ``phash.compute_phash``, Kafka
``workshop/imagetools.py`` and ``open_image``, Faustus ``gallery_helpers._extract_exif`` / ``strip_location_exif``,
Argus ``images.dhash`` / ``hamming``, and the open-orient-RGB-thumbnail boilerplate in Cicero, Vitruvius, Prospero and
Gepetto. (Argus' dhash and Daguerre's phash are different measures: do not compare them.)
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from ..errors import missing_dependency

__all__ = ["Packed", "register_heif", "open_oriented", "flatten_rgb", "thumbnail", "read_exif", "strip_exif", "phash",
           "dhash", "hamming", "content_hash", "compress_to_limit"]

Source = Union[str, Path, bytes, bytearray, memoryview, Any]
_heif: Optional[bool] = None


def _pil() -> Any:
    try:
        from PIL import Image
    except ImportError:
        raise missing_dependency("PIL", "image processing", pip_name="Pillow") from None
    return Image


def _resample() -> Any:
    Image = _pil()
    return getattr(Image, "Resampling", Image).LANCZOS


def register_heif() -> bool:
    """Teach Pillow to open HEIC/HEIF (and AVIF when the wheel supports it) if ``pillow-heif`` is installed.
    Returns whether it is available; safe to call repeatedly."""
    global _heif
    if _heif is None:
        try:
            import pillow_heif  # type: ignore

            pillow_heif.register_heif_opener()
            try:
                pillow_heif.register_avif_opener()
            except Exception:  # noqa: BLE001 - older wheels have no AVIF
                pass
            _heif = True
        except Exception:  # noqa: BLE001 - missing or broken wheel: report, do not crash
            _heif = False
    return _heif


def _open_raw(src: Source) -> Any:
    Image = _pil()
    register_heif()
    if isinstance(src, (bytes, bytearray, memoryview)):
        return Image.open(io.BytesIO(bytes(src)))
    if isinstance(src, (str, Path)):
        return Image.open(str(src))
    return Image.open(src)


def open_oriented(src: Source, *, max_pixels: Optional[int] = None, draft: Union[None, int, tuple] = None) -> Any:
    """A loaded Pillow image, upright according to its EXIF orientation, in ``RGB``, ``RGBA`` or ``L`` (palette,
    CMYK, 16-bit and the like are converted; transparency is kept as ``RGBA``). ``max_pixels`` refuses huge images
    (``ValueError``, checked before decoding). ``draft`` (a size or ``(w, h)``) asks JPEG for a reduced-size decode,
    which is much faster for thumbnails; the result is then at least that large, not necessarily full size.
    ``ValueError`` when the data is not an image."""
    Image = _pil()
    from PIL import ImageOps

    try:
        raw = _open_raw(src)
    except (OSError, ValueError) as exc:                  # PIL.UnidentifiedImageError is an OSError
        raise ValueError(f"not a readable image: {exc}") from exc
    try:
        if max_pixels is not None and raw.size[0] * raw.size[1] > max_pixels:
            raise ValueError(f"image has {raw.size[0] * raw.size[1]} pixels (limit {max_pixels})")
        if draft is not None:
            size = (draft, draft) if isinstance(draft, int) else tuple(draft)
            try:
                raw.draft("RGB", size)
            except Exception:  # noqa: BLE001 - not a JPEG, or no reduction possible
                pass
        try:
            img = ImageOps.exif_transpose(raw)
        except Exception:  # noqa: BLE001 - damaged EXIF must not lose the picture
            img = raw
        img.load()
        if img is raw:
            img = img.copy()
        return _normalise_mode(img)
    except (OSError, SyntaxError) as exc:                 # truncated or corrupt data
        raise ValueError(f"not a readable image: {exc}") from exc
    finally:
        try:
            raw.close()
        except Exception:  # noqa: BLE001
            pass


def _has_alpha(img: Any) -> bool:
    return img.mode in ("RGBA", "LA", "PA", "La", "RGBa") or (img.mode == "P" and "transparency" in img.info)


def _normalise_mode(img: Any) -> Any:
    if img.mode in ("RGB", "RGBA", "L"):
        return img
    return img.convert("RGBA" if _has_alpha(img) else "RGB")


def flatten_rgb(img: Any, bg: tuple = (255, 255, 255)) -> Any:
    """``img`` as plain ``RGB``: transparency is composited onto ``bg`` (white by default) instead of turning black."""
    Image = _pil()
    if _has_alpha(img):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, tuple(bg)[:3])
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return img if img.mode == "RGB" else img.convert("RGB")


def _save(img: Any, fmt: str, **kw: Any) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def thumbnail(src: Source, *, size: int = 512, fmt: str = "webp", quality: int = 80) -> bytes:
    """Encoded thumbnail whose long side is at most ``size`` (never enlarged), upright, EXIF dropped. ``fmt`` is
    ``webp`` (default; keeps transparency), ``jpeg``/``jpg`` (transparency flattened to white) or ``png``."""
    f = fmt.lower().lstrip(".")
    pil_fmt = {"jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP", "png": "PNG"}.get(f)
    if pil_fmt is None:
        raise ValueError(f"unsupported thumbnail format {fmt!r}; use webp, jpeg or png")
    img = open_oriented(src, draft=(size, size))
    img.thumbnail((size, size), _resample())
    if pil_fmt == "JPEG":
        return _save(flatten_rgb(img) if img.mode != "L" else img, "JPEG", quality=quality, optimize=True)
    if pil_fmt == "WEBP":
        return _save(img, "WEBP", quality=quality, method=4)
    return _save(img, "PNG", optimize=True)


# ---- EXIF ------------------------------------------------------------------------------------------

def _ratio(value: Any) -> Optional[float]:
    try:
        if hasattr(value, "numerator"):
            return float(value.numerator) / float(value.denominator or 1)
        if isinstance(value, tuple) and len(value) == 2:
            return float(value[0]) / float(value[1] or 1)
        return float(value)
    except (TypeError, ZeroDivisionError, ValueError):
        return None


def _dms(dms: Any, ref: Any) -> Optional[float]:
    try:
        d, m, s = (_ratio(v) for v in dms)
        if d is None or m is None or s is None:
            return None
        value = d + m / 60.0 + s / 3600.0
        if _clean(ref) in ("S", "W"):
            value = -value
        return round(value, 6)
    except (TypeError, ValueError):
        return None


def _exif_datetime(raw: Any, offset: Any) -> Optional[str]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = _dt.datetime.strptime(raw.strip().rstrip("\x00")[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None                                    # includes the "0000:00:00 00:00:00" some cameras write
    if parsed.year < 1850:
        return None
    iso = parsed.isoformat()
    if isinstance(offset, str) and offset.strip():
        try:
            o = offset.strip().rstrip("\x00")
            sign = 1 if o[0] == "+" else -1
            hh, mm = o[1:].split(":")
            iso = parsed.replace(tzinfo=_dt.timezone(_dt.timedelta(hours=int(hh), minutes=int(mm)) * sign)).isoformat()
        except (ValueError, IndexError):
            pass
    return iso


def _first(value: Any) -> Any:
    """Some tags (ISO on many cameras) are stored as a tuple of values."""
    if isinstance(value, (tuple, list)):
        return value[0] if value else None
    return value


def _int(value: Any) -> Optional[int]:
    try:
        v = _first(value)
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).replace("\x00", "").strip()
    return text[:120] or None


def _exposure(value: Any) -> Optional[str]:
    seconds = _ratio(_first(value))
    if seconds is None or seconds <= 0:
        return None
    if seconds < 1:
        return f"1/{round(1 / seconds)}"
    return f"{int(seconds)}s" if seconds == int(seconds) else f"{seconds:g}s"


def read_exif(src: Source) -> dict[str, Any]:
    """Photo metadata as a dict (every key present, ``None`` when unknown): ``width``, ``height`` (as stored, before
    orientation), ``orientation`` (1-8), ``date`` (ISO 8601, with the UTC offset when the camera wrote one),
    ``lat``, ``lon`` (decimal degrees; ``0,0`` "no fix" is dropped), ``camera`` (make + model), ``make``, ``model``,
    ``lens``, ``iso``, ``f_number``, ``exposure`` (``"1/250"``), ``focal_length``. ``ValueError`` if not an image."""
    from PIL import ExifTags

    try:
        img = _open_raw(src)
    except (OSError, ValueError) as exc:
        raise ValueError(f"not a readable image: {exc}") from exc
    with img:
        exif: dict[Any, Any] = {}
        gps: dict[Any, Any] = {}
        try:
            raw = img.getexif()
            for tag, value in raw.items():
                exif[ExifTags.TAGS.get(tag, tag)] = value
            try:                                         # DateTimeOriginal, exposure and lens live in the Exif sub-IFD
                for tag, value in raw.get_ifd(ExifTags.IFD.Exif).items():
                    exif[ExifTags.TAGS.get(tag, tag)] = value
            except Exception:  # noqa: BLE001
                pass
            try:
                for tag, value in (raw.get_ifd(ExifTags.IFD.GPSInfo) or {}).items():
                    gps[ExifTags.GPSTAGS.get(tag, tag)] = value
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 - unreadable EXIF is common, not fatal
            pass
        orientation = _int(exif.get("Orientation")) or 1
        if orientation not in range(1, 9):
            orientation = 1
        date = None
        for tag, off in (("DateTimeOriginal", "OffsetTimeOriginal"), ("DateTimeDigitized", "OffsetTimeDigitized"), ("DateTime", "OffsetTime")):
            date = _exif_datetime(exif.get(tag), exif.get(off) or exif.get("OffsetTime"))
            if date:
                break
        lat = lon = None
        if gps.get("GPSLatitude") and gps.get("GPSLongitude"):
            lat, lon = _dms(gps["GPSLatitude"], gps.get("GPSLatitudeRef")), _dms(gps["GPSLongitude"], gps.get("GPSLongitudeRef"))
            if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
                lat = lon = None
        make, model = _clean(exif.get("Make")), _clean(exif.get("Model"))
        camera = " ".join(x for x in ((make if not (model and make and model.lower().startswith(make.lower())) else None), model) if x) or None
        iso = _int(exif.get("ISOSpeedRatings"))
        if iso is None:
            iso = _int(exif.get("PhotographicSensitivity"))
        return {
            "width": img.size[0], "height": img.size[1], "orientation": orientation, "date": date, "lat": lat, "lon": lon,
            "camera": camera, "make": make, "model": model, "lens": _clean(exif.get("LensModel")), "iso": iso,
            "f_number": _ratio(_first(exif.get("FNumber"))), "exposure": _exposure(exif.get("ExposureTime")),
            "focal_length": _ratio(_first(exif.get("FocalLength"))),
        }


_GPS_IFD = 0x8825
_ORIENTATION = 0x0112


def strip_exif(src: Source, *, gps_only: bool = False, keep_orientation: bool = True, strict: bool = True) -> bytes:
    """The image re-encoded without location (``gps_only=True``: every other EXIF tag stays) or without any EXIF.
    With ``keep_orientation`` a stripped file keeps just the Orientation tag, so it still displays upright. When
    ``gps_only`` and there is no GPS tag the original bytes come back untouched. JPEG keeps its quantisation
    (``quality="keep"``), PNG is lossless, WebP is re-encoded at high quality; the ICC profile is kept. XMP and PNG
    text chunks are dropped (not copied). HEIC and other formats Pillow cannot write raise ``ValueError`` — or, with
    ``strict=False``, return the original bytes unchanged (never silently when strict)."""
    original = bytes(src) if isinstance(src, (bytes, bytearray, memoryview)) else None
    try:
        img = _open_raw(src)
        fmt = (img.format or "").upper()
        exif = img.getexif()
        if gps_only and _GPS_IFD not in exif:
            if original is not None:
                return original
            if isinstance(src, (str, Path)):
                return Path(src).read_bytes()
        if fmt not in ("JPEG", "PNG", "WEBP"):
            raise ValueError(f"cannot rewrite {fmt or 'unknown'} images")
        if gps_only:
            del exif[_GPS_IFD]
            out_exif = exif
        else:
            orientation = exif.get(_ORIENTATION)
            Image = _pil()
            out_exif = Image.Exif()
            if keep_orientation and orientation and int(orientation) != 1:
                out_exif[_ORIENTATION] = int(orientation)
        img.load()
        kw: dict[str, Any] = {}
        if img.info.get("icc_profile"):
            kw["icc_profile"] = img.info["icc_profile"]
        if len(out_exif):
            kw["exif"] = out_exif
        if fmt == "JPEG":
            kw.update(quality="keep", subsampling="keep", optimize=True)
            if img.info.get("progressive") or img.info.get("progression"):
                kw["progressive"] = True
        elif fmt == "WEBP":
            kw.update(quality=95, method=4)
        else:
            kw["optimize"] = True
        return _save(img, fmt, **kw)
    except (ValueError, OSError) as exc:
        if strict:
            if isinstance(exc, ValueError):
                raise
            raise ValueError(f"cannot rewrite the image: {exc}") from exc
        if original is not None:
            return original
        if isinstance(src, (str, Path)):
            return Path(src).read_bytes()
        raise


# ---- hashes ----------------------------------------------------------------------------------------

_DCT_N = 32
_DCT_LOW = 8
_DCT_COS = [[math.cos(math.pi * k * (2 * n + 1) / (2 * _DCT_N)) for n in range(_DCT_N)] for k in range(_DCT_LOW)]


def _as_image(img: Any) -> Any:
    Image = _pil()
    return img if isinstance(img, Image.Image) else open_oriented(img)


def phash(img: Any) -> int:
    """64-bit perceptual hash as an ``int``, equal to ``int(str(imagehash.phash(img)), 16)``: 32x32 grey, 2-D DCT,
    top-left 8x8 block compared to its median (bit order row by row, first bit most significant). ``img`` is a
    Pillow image (orient it first: :func:`open_oriented`) or anything :func:`open_oriented` takes. Compare two
    hashes with :func:`hamming`; at most ~6 differing bits is the usual "same picture" threshold."""
    grey = _as_image(img).convert("L").resize((_DCT_N, _DCT_N), _resample())
    px = grey.tobytes()
    rows = [[float(px[r * _DCT_N + c]) for c in range(_DCT_N)] for r in range(_DCT_N)]
    # D = C P C^T restricted to the first 8 frequencies (what imagehash keeps)
    cp = [[sum(_DCT_COS[k][r] * rows[r][c] for r in range(_DCT_N)) for c in range(_DCT_N)] for k in range(_DCT_LOW)]
    low = [[sum(cp[k][c] * _DCT_COS[l][c] for c in range(_DCT_N)) for l in range(_DCT_LOW)] for k in range(_DCT_LOW)]
    flat = [v for row in low for v in row]
    ordered = sorted(flat)
    median = (ordered[31] + ordered[32]) / 2.0
    bits = 0
    for v in flat:
        bits = (bits << 1) | (1 if v > median else 0)
    return bits


def dhash(img: Any, size: int = 16) -> str:
    """Difference hash as a lowercase hex string of ``size * size / 4`` digits (Argus stores this: 256 bits for
    ``size=16``): the picture is shrunk to ``(size+1) x size`` grey and each pixel is compared with its right
    neighbour."""
    grey = _as_image(img).convert("L").resize((size + 1, size), _resample())
    px = grey.tobytes()
    value = 0
    for r in range(size):
        base = r * (size + 1)
        for c in range(size):
            value = (value << 1) | (1 if px[base + c + 1] > px[base + c] else 0)
    return f"{value:0{size * size // 4}x}"


def hamming(a: Union[int, str], b: Union[int, str]) -> int:
    """Number of differing bits between two hashes of the same kind (two ``int`` from :func:`phash` or two hex
    strings from :func:`dhash`; hex strings of different length count as completely different)."""
    if isinstance(a, str) and isinstance(b, str):
        if len(a) != len(b):
            return max(len(a), len(b)) * 4
        return (int(a, 16) ^ int(b, 16)).bit_count()
    x = int(a, 16) if isinstance(a, str) else int(a)
    y = int(b, 16) if isinstance(b, str) else int(b)
    return (x ^ y).bit_count()


def content_hash(path: Union[str, Path], algo: str = "blake2b", digest: int = 32) -> str:
    """Hex digest of a file read in 1 MB pieces (for exact-duplicate detection and following a moved file).
    ``algo`` is any :mod:`hashlib` name; ``digest`` (bytes) applies to ``blake2b`` / ``blake2s``."""
    if algo in ("blake2b", "blake2s"):
        h = getattr(hashlib, algo)(digest_size=digest)
    else:
        h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---- compress to a size limit -----------------------------------------------------------------------

PNG_COLOURS = (256, 192, 128, 96, 64, 48, 32, 16, 8)
JPEG_QUALITIES = (95, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40)
WEBP_QUALITIES = (90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40)
SCALES = (0.90, 0.80, 0.70, 0.60, 0.50, 0.40, 0.30, 0.25, 0.20, 0.15, 0.10)


@dataclass
class Packed:
    ok: bool                 # fits the limit
    data: bytes              # the smallest result found (empty for animated images)
    strategy: str            # what worked: lossless, quantize-64, quality-80, resize-0.50+q60, exhausted, animated
    note: str = ""
    fmt: str = ""            # png | jpeg | webp

    @property
    def size(self) -> int:
        return len(self.data)


def _quantize(img: Any, colours: int) -> Any:
    Image = _pil()
    try:
        if img.mode == "RGBA":
            return img.quantize(colors=colours, method=Image.Quantize.FASTOCTREE)
        return img.quantize(colors=colours, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG)
    except (AttributeError, ValueError):
        return img.convert("P", palette=Image.ADAPTIVE, colors=colours)


def _keep(img: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if img.info.get("icc_profile"):
        out["icc_profile"] = img.info["icc_profile"]
    if img.info.get("exif"):
        out["exif"] = img.info["exif"]
    return out


def _resized(img: Any, scale: float) -> Any:
    return img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), _resample())


_TOO_BIG = "still over the limit without losing quality"


def _png(img: Any, limit: int, lossless_only: bool) -> Packed:
    keep = {k: v for k, v in _keep(img).items() if k == "icc_profile"}
    if img.info.get("dpi"):
        keep["dpi"] = img.info["dpi"]
    data = _save(img, "PNG", optimize=True, compress_level=9, **keep)
    if len(data) <= limit:
        return Packed(True, data, "lossless", fmt="png")
    if lossless_only:
        return Packed(False, data, "lossless", _TOO_BIG, "png")
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    base = img.convert("RGBA") if has_alpha else img.convert("RGB")
    best = data
    for n in PNG_COLOURS:
        data = _save(_quantize(base, n), "PNG", optimize=True, compress_level=9)
        best = min(best, data, key=len)
        if len(data) <= limit:
            return Packed(True, data, f"quantize-{n}", fmt="png")
    for scale in SCALES:
        data = _save(_quantize(_resized(base, scale), 64), "PNG", optimize=True, compress_level=9)
        best = min(best, data, key=len)
        if len(data) <= limit:
            return Packed(True, data, f"resize-{scale:.2f}+q64", fmt="png")
    return Packed(False, best, "exhausted", "does not fit even after reducing size and colours", "png")


def _jpeg(img: Any, limit: int, lossless_only: bool) -> Packed:
    if img.mode not in ("RGB", "L", "CMYK"):
        img = flatten_rgb(img)
    keep = _keep(img)
    try:
        data = _save(img, "JPEG", quality="keep" if getattr(img, "format", None) == "JPEG" else 95, optimize=True, progressive=True, **keep)
    except (ValueError, OSError):
        data = _save(img, "JPEG", quality=95, optimize=True, progressive=True, **keep)
    if len(data) <= limit:
        return Packed(True, data, "optimize", fmt="jpeg")
    if lossless_only:
        return Packed(False, data, "optimize", _TOO_BIG, "jpeg")
    best = data
    for q in JPEG_QUALITIES:
        data = _save(img, "JPEG", quality=q, optimize=True, progressive=True, **keep)
        best = min(best, data, key=len)
        if len(data) <= limit:
            return Packed(True, data, f"quality-{q}", fmt="jpeg")
    for scale in SCALES:
        data = _save(_resized(img, scale), "JPEG", quality=60, optimize=True, progressive=True, **keep)
        best = min(best, data, key=len)
        if len(data) <= limit:
            return Packed(True, data, f"resize-{scale:.2f}+q60", fmt="jpeg")
    return Packed(False, best, "exhausted", "does not fit even after reducing quality and size", "jpeg")


def _webp(img: Any, limit: int, lossless_only: bool) -> Packed:
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if _has_alpha(img) else "RGB")
    keep = _keep(img)
    data = _save(img, "WEBP", lossless=True, quality=100, method=6, **keep)
    if len(data) <= limit:
        return Packed(True, data, "lossless", fmt="webp")
    if lossless_only:
        return Packed(False, data, "lossless", _TOO_BIG, "webp")
    best = data
    for q in WEBP_QUALITIES:
        data = _save(img, "WEBP", quality=q, method=4, **keep)
        best = min(best, data, key=len)
        if len(data) <= limit:
            return Packed(True, data, f"quality-{q}", fmt="webp")
    for scale in SCALES:
        data = _save(_resized(img, scale), "WEBP", quality=60, method=4)
        best = min(best, data, key=len)
        if len(data) <= limit:
            return Packed(True, data, f"resize-{scale:.2f}+q60", fmt="webp")
    return Packed(False, best, "exhausted", "does not fit even after reducing quality and size", "webp")


def compress_to_limit(src: Source, limit_bytes: int, *, lossless_only: bool = False) -> Packed:
    """Re-encode a PNG, JPEG or WebP so it is at most ``limit_bytes``, trying in order: PNG lossless optimise, then
    256…8 colours, then smaller + 64 colours; JPEG re-encode at the same quality, then quality 95…40, then smaller at
    q60; WebP lossless, then quality 90…40, then smaller. ICC profile and EXIF are kept (except when resizing a
    PNG). ``lossless_only`` stops after the first step and reports ``ok=False`` instead of degrading. Never touches
    the source. Animated images come back as ``Packed(False, b"", "animated")``. ``ValueError`` for anything that
    is not a PNG, JPEG or WebP image."""
    try:
        img = _open_raw(src)
    except (OSError, ValueError) as exc:
        raise ValueError(f"not a readable image: {exc}") from exc
    fmt = (img.format or "").upper()
    if fmt not in ("PNG", "JPEG", "WEBP"):
        raise ValueError(f"only PNG, JPEG and WebP can be compressed, not {fmt or 'this format'}")
    if getattr(img, "n_frames", 1) > 1:
        return Packed(False, b"", "animated", "animated image: left untouched", fmt.lower())
    try:
        img.load()
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"not a readable image: {exc}") from exc
    return {"PNG": _png, "JPEG": _jpeg, "WEBP": _webp}[fmt](img, int(limit_bytes), lossless_only)
