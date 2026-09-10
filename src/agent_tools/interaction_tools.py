import json
import logging
import uuid

from src.plan_state import from_markdown, parse_steps_input, stable_step_id, to_markdown

logger = logging.getLogger(__name__)


def _stable_option_id(label: str, order: int) -> str:
    """Mirrors `plan_state.stable_step_id`'s shape: same label at the same
    position always gets the same id, so a UI can key a click by `id` instead
    of matching label text back to the option it rendered."""
    return "opt_" + stable_step_id(label, order)[len("step_"):]


class AskUserTool:
    async def execute(self, content, ctx):
        """
        ask_user: the agent poses a multiple-choice question to the user to get a
        decision/clarification. This is a pure UI-control marker — no subprocess,
        no filesystem. It returns an `ask_user` payload that the agent loop turns
        into an `ask_user` SSE event and then ENDS the turn, so the chat waits for
        the user's selection (their choice arrives as the next message).

        CALL-07 / TASK-04: the payload carries a stable `question_id` (so a
        later answer can be matched back to *this* question rather than "the
        most recent one") and a stable `id` per option, plus
        `allow_free_text` — the UI already offers a free-text line beside the
        buttons, this just makes that explicit instead of implied. The
        original `{label, description}` option shape is unchanged; `id` is
        an addition, not a replacement, so an old client that ignores it
        keeps working.
        """
        question, options, multi = "", [], False
        raw = (content or "").strip()
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            parsed = {}

        allow_free_text = True
        if isinstance(parsed, dict):
            question = str(parsed.get("question", "")).strip()
            multi = bool(parsed.get("multi") or parsed.get("multiSelect"))
            if "allow_free_text" in parsed:
                allow_free_text = bool(parsed.get("allow_free_text"))
            for opt in (parsed.get("options") or []):
                if isinstance(opt, dict):
                    label = str(opt.get("label", "")).strip()
                    descr = str(opt.get("description", "")).strip()
                elif isinstance(opt, str):
                    label, descr = opt.strip(), ""
                else:
                    continue
                if label:
                    options.append({"label": label, "description": descr})
        else:
            question = raw

        if not question or len(options) < 2:
            return "ask_user: invalid", {
                "error": (
                    "ask_user needs a non-empty `question` and at least 2 `options` "
                    "(each an object with a `label`, optional `description`)."
                ),
                "exit_code": 1,
            }

        options = options[:6]  # keep the choice list sane
        for i, opt in enumerate(options):
            opt["id"] = _stable_option_id(opt["label"], i)
        question_id = f"qst_{uuid.uuid4().hex[:20]}"
        desc = f"ask_user: {question[:80]}"
        labels = ", ".join(o["label"] for o in options)
        result = {
            "ask_user": {
                "question_id": question_id,
                "question": question,
                "options": options,
                "multi": multi,
                "allow_free_text": allow_free_text,
            },
            "output": f"Asked the user: {question}\nOptions: {labels}\nAwaiting their selection.",
            "exit_code": 0,
        }
        logger.info("Tool executed: %s (%d options, multi=%s, id=%s)",
                    desc, len(options), multi, question_id)
        return desc, result

class UpdatePlanTool:
    async def execute(self, content, ctx):
        """
        update_plan: the agent writes back to the active plan — tick an item done
        or revise steps (e.g. when the user asks to change something). Pure UI
        marker: returns a `plan_update` payload the agent loop turns into a
        `plan_update` SSE event; the frontend replaces the stored plan and refreshes
        the docked plan window. Does NOT end the turn.

        TASK-01: no truncation, at any length — the 8,192-character cap this
        tool used to apply is gone (it silently amputated any plan longer
        than that). `plan_update.plan` stays the caller's markdown verbatim
        (or the checklist rendered from `steps`, when that's all that was
        sent); `steps` is the same plan parsed into stable-id, status,
        dependency and evidence structure, alongside `revision` and any
        `warnings` from lines that didn't parse as checklist items. Nothing
        about the existing `plan` field's shape changes, so an old renderer
        that reads only `plan` keeps working exactly as before.
        """
        raw = (content or "").strip()
        plan = ""
        steps_plan = None
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            parsed = {}

        if isinstance(parsed, dict) and parsed.get("plan"):
            plan = str(parsed.get("plan", "")).strip()
        elif isinstance(parsed, dict) and parsed.get("steps"):
            # Newer structured form: {"steps": [...]}. No markdown was given,
            # so `plan` (the field every existing client already reads) is
            # rendered from the structured steps instead of left empty.
            steps_plan = parse_steps_input(parsed)
            if steps_plan is not None:
                plan = to_markdown(steps_plan)
        else:
            plan = raw

        if not plan:
            return "update_plan: invalid", {
                "error": (
                    "update_plan needs a non-empty `plan` (the full updated checklist "
                    "as markdown) or a `steps` array."
                ),
                "exit_code": 1,
            }

        parsed_plan = steps_plan if steps_plan is not None else from_markdown(plan)
        done, total = parsed_plan.counts()
        desc = f"update_plan: {done}/{total} done" if total else "update_plan"
        payload = {"plan": plan}
        payload.update(parsed_plan.to_dict())  # steps, revision, warnings
        result = {
            "plan_update": payload,
            "output": f"Plan updated ({done}/{total} steps complete)." if total else "Plan updated.",
            "exit_code": 0,
        }
        logger.info("Tool executed: %s (revision=%s, warnings=%d)",
                    desc, parsed_plan.revision, len(parsed_plan.warnings))
        return desc, result