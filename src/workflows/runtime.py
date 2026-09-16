"""One set of production capabilities for HTTP and background continuation."""
from src.workflows.handlers import default_handlers


def production_handlers():
    from src.workflows.artifacts import save
    from src.workflows.delivery import send
    from src.workflows.skills import run

    # WP22 (creator/production_plan.py): a plan step compiles to a 'skill'
    # node marked `config.creator_plan_step`. The background
    # WorkflowScheduler resumes ANY running/paused run with THIS handler
    # set, not only the one the run was started under — so a production
    # step must be routed to its own runner here too, or a restart would
    # hand it to the container-skill runner instead. Additive: every
    # non-creator skill node is unaffected, same `run` as before.
    def skill(node, context):
        if bool((node.config or {}).get("creator_plan_step")):
            from src.creator.production_plan import creator_step_handler
            return creator_step_handler(node, context)
        return run(node, context)

    return default_handlers(artifact_store=save, deliver=send, skill=skill)
