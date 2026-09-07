"""One set of production capabilities for HTTP and background continuation."""
from src.workflows.handlers import default_handlers


def production_handlers():
    from src.workflows.artifacts import save
    from src.workflows.delivery import send
    from src.workflows.skills import run
    return default_handlers(artifact_store=save, deliver=send, skill=run)
