"""Dedicated typed media tools; no shell or model invocation required."""
import json


class InspectMediaTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.media_inspection import inspect_media, MediaInspectionError
        try:
            args = json.loads(content)
            if not isinstance(args, dict) or set(args) - {'path'}:
                raise ValueError('Expected an object with only a local file path')
            result = await inspect_media(args.get('path'))
            return {'output': json.dumps(result, ensure_ascii=False, allow_nan=False),
                    'media': result, 'exit_code': 0}
        except MediaInspectionError as exc:
            return {'error': f'inspect_media: {exc}', 'error_code': exc.code, 'exit_code': 1}
        except (ValueError, TypeError):
            return {'error': 'inspect_media: expected {"path":"local media file"}.', 'error_code': 'invalid_arguments', 'exit_code': 1}
        except OSError:
            return {'error': 'inspect_media: the installed media probe could not be started. Check the local runtime.', 'error_code': 'probe_unavailable', 'exit_code': 1}


class MediaTransformTool:
    def __init__(self, *, preview=False):
        self.preview = preview

    async def execute(self, content: str, ctx: dict) -> dict:
        from src.media_inspection import MediaInspectionError
        from src.media_transforms import MediaTransformError, plan_media_transform, transform_media
        name = 'plan_media_transform' if self.preview else 'transform_media'
        try:
            args = json.loads(content)
            result = (await plan_media_transform(args) if self.preview else
                      await transform_media(args, ctx.get('progress_cb')))
            return {'output': json.dumps(result, ensure_ascii=False, allow_nan=False),
                    'media_transform': result, 'exit_code': 0}
        except (MediaTransformError, MediaInspectionError) as exc:
            return {'error': f'{name}: {exc}', 'error_code': exc.code, 'exit_code': 1}
        except (TypeError, ValueError):
            return {'error': f'{name}: expected a JSON media recipe with source, path and format.', 'error_code': 'invalid_arguments', 'exit_code': 1}
        except OSError:
            return {'error': f'{name}: local file or runtime unavailable. No successful publication was confirmed.', 'error_code': 'io_error', 'exit_code': 1}
