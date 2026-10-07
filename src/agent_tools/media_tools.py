"""Dedicated typed media tools; no shell or model invocation required."""
import json


class ImageJobTool:
    """Check or cancel an owned image request without submitting a new render."""

    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = json.loads(content)
            if (not isinstance(args, dict) or 'request_id' not in args or set(args) - {'request_id', 'action'}
                    or not isinstance(args['request_id'], str)
                    or args.get('action', 'status') not in ('status', 'cancel')):
                raise ValueError('Expected an image request_id and an optional status/cancel action')
        except (ValueError, TypeError):
            return {'error': 'image_job requires {"request_id":"the previous image request ID",'
                             ' "action":"status"|"cancel"}', 'exit_code': 1}
        if args.get('action') == 'cancel':
            from src.prospero_images import cancel_image
            return await cancel_image(args['request_id'], ctx.get('session_id'), ctx.get('owner'))
        from src.prospero_images import resume_image
        return await resume_image(args['request_id'], ctx.get('session_id'), ctx.get('owner'))


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


class InspectDeliverableTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.deliverable_inspection import inspect_deliverable, DeliverableInspectionError
        try:
            args = json.loads(content)
            if (not isinstance(args, dict) or set(args) - {'path', 'max_content_chars'}
                    or not isinstance(args.get('path'), str)):
                raise ValueError('Expected a local file path and optional content limit')
            result = await inspect_deliverable(args['path'], max_content_chars=args.get('max_content_chars', 24000))
            return {'output': json.dumps(result, ensure_ascii=False, allow_nan=False),
                    'deliverable': result, 'exit_code': 0}
        except DeliverableInspectionError as exc:
            return {'error': f'inspect_deliverable: {exc}', 'error_code': exc.code, 'exit_code': 1}
        except (ValueError, TypeError):
            return {'error': 'inspect_deliverable: expected {"path":"local file", "max_content_chars":24000}.',
                    'error_code': 'invalid_arguments', 'exit_code': 1}


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
