"""Disposable image converter. Called only with a validated, fixed recipe."""
import json
import sys
import warnings


def convert(source, destination, options):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 16_000_000
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(source) as original:
            if getattr(original, 'n_frames', 1) != 1:
                raise ValueError('animated_or_multipage')
            if original.width * original.height > Image.MAX_IMAGE_PIXELS:
                raise ValueError('pixel_limit')
            image = ImageOps.exif_transpose(original)
            # Keep alpha, but never carry arbitrary metadata/EXIF into the output.
            image = image.convert('RGBA' if 'A' in image.getbands() or 'transparency' in image.info else 'RGB')
            if options.get('max_width') or options.get('max_height'):
                image.thumbnail((options.get('max_width') or image.width,
                                 options.get('max_height') or image.height), Image.Resampling.LANCZOS)
            if options['format'] == 'jpeg':
                if image.mode == 'RGBA':
                    background = options.get('background')
                    if background is None:
                        raise ValueError('background_required')
                    canvas = Image.new('RGB', image.size, background)
                    canvas.paste(image, mask=image.getchannel('A'))
                    image = canvas
                image = image.convert('RGB')
            image.info.clear()
            params = {'quality': options.get('quality', 90)} if options['format'] in {'jpeg', 'webp'} else {}
            image.save(destination, format={'png': 'PNG', 'jpeg': 'JPEG', 'webp': 'WEBP'}[options['format']], **params)
            print(json.dumps({'width': image.width, 'height': image.height}))


if __name__ == '__main__':
    try:
        convert(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]))
    except Exception as exc:
        # Only fixed codes, not decoder messages containing untrusted metadata.
        code = str(exc) if str(exc) in {'animated_or_multipage', 'pixel_limit', 'background_required'} else 'conversion_failed'
        print(json.dumps({'error_code': code}))
        raise SystemExit(1)
