"""Read the existing local graph with both valid-time and recorded-time filters."""
import asyncio


class BrainGraphTool:
    async def execute(self, content, ctx):
        from src.brain.temporal_graph import GraphQuery, retrieve
        from src.agent_tools.reach_tools import _structured_output
        from pydantic import ValidationError
        try:
            query = GraphQuery.model_validate_json(content)
            data = await asyncio.to_thread(retrieve, (ctx or {}).get("owner"), query)
            data["edge_count"] = len(data["edges"])
            data["results"] = data.pop("edges")
            return _structured_output(data)
        except Exception as exc:
            reason = "; ".join(e["msg"] for e in exc.errors(include_input=False)) if isinstance(exc, ValidationError) else str(exc)
            return {"error": "brain_graph: " + reason, "exit_code": 1}
