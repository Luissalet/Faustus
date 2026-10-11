"""Native recovery of prior Hoard work from a general Faustus conversation."""
import json


class HoardProjectTool:
    def __init__(self, action):
        self.action = action

    async def execute(self, content, ctx):
        import asyncio
        from services.hoard_project_recovery import HoardProjectRecovery
        try:
            arguments = json.loads(content or '{}')
            operation = getattr(HoardProjectRecovery(), self.action)
            result = await asyncio.to_thread(operation, ctx.get('owner'), **arguments)
            return {'output': json.dumps(result, ensure_ascii=False), 'exit_code': 0, 'projects': result}
        except (ValueError, TypeError) as error:
            return {'error': str(error), 'exit_code': 1}
