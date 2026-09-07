"""Header-only image/WAV probe, run in a disposable child process.

No model, shell, network, image rendering or writes. Keeping parsers out of the
server process lets the caller terminate a malformed/slow input on cancellation.
"""
import json
import sys
import wave
import warnings


def probe_image(path):
    from PIL import Image
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as image:
            width, height = image.size
            # Read orientation only, never expose arbitrary EXIF/GPS/prompts.
            orientation = image.getexif().get(274, 1)
            swapped = orientation in (5, 6, 7, 8)
            return {
                'kind': 'image', 'format': image.format, 'width': width, 'height': height,
                'display_width': height if swapped else width,
                'display_height': width if swapped else height,
                'orientation': orientation if isinstance(orientation, int) else None,
                'mode': image.mode,
                'has_alpha_channel': 'A' in image.getbands() or 'transparency' in image.info,
                'limitations': ['Header metadata only; pixels and visual content have not been analyzed. Frame count and animation duration were not scanned.'],
            }


def probe_wav(path):
    with wave.open(path, 'rb') as audio:
        rate = audio.getframerate()
        return {
            'kind': 'audio', 'format': 'WAV', 'channels': audio.getnchannels(),
            'sample_rate_hz': rate, 'bits_per_sample': audio.getsampwidth() * 8,
            'duration_seconds': audio.getnframes() / rate if rate else None,
            'codec': audio.getcomptype(),
            'limitations': ['PCM header metadata only; audio has not been listened to or transcribed.'],
        }


if __name__ == '__main__':
    try:
        if len(sys.argv) != 3 or sys.argv[1] not in {'image', 'wav'}:
            raise ValueError('Invalid probe arguments')
        result = probe_image(sys.argv[2]) if sys.argv[1] == 'image' else probe_wav(sys.argv[2])
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    except Exception as exc:
        # Do not echo arbitrary parser messages / embedded metadata to the model.
        print(json.dumps({'probe_error': type(exc).__name__}))
        raise SystemExit(1)
