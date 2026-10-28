"""Answer a multi-hop question from the Cerebro: plan, retrieve over graph and text, answer."""
import asyncio


class GraphAskTool:
    async def execute(self, content, ctx):
        from pydantic import ValidationError
        from src.agent_tools.reach_tools import _structured_output
        from src.brain import graph_ask
        from src.brain.graph_ask import GraphAskRequest
        try:
            request = GraphAskRequest.model_validate_json(content)
            data = await graph_ask.ask(
                (ctx or {}).get("owner"), request.question, plan_only=request.plan_only,
                max_steps=request.max_steps, top_k=request.top_k, alpha=request.alpha)
            return _structured_output(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = "; ".join(e["msg"] for e in exc.errors(include_input=False)) if isinstance(exc, ValidationError) else str(exc)
            return {"error": "graph_ask: " + reason, "exit_code": 1}
