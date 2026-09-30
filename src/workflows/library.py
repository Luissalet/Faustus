"""
workflows/library.py — saved workflows, per owner.

Until now a route took a definition inline and forgot it. Anything that wants
to call a workflow by name (a published tool, an evaluation, a schedule) needs
the definition kept somewhere with an owner, a name, and a switch for whether
other callers may start it.

A saved workflow is a *definition*, nothing more: runs still snapshot the
definition they start from (`WorkflowRunRow.definition_json`), so saving over a
name never changes a run in flight. `enabled` is the one flag with teeth — it
publishes the workflow as a tool for outside callers (`published.py`) — and it
defaults to off, because publishing is what lets another assistant start real
work on this machine.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Mapping, Optional

from src.contracts import ContractError, WorkflowDefinition

from . import schema_check

__all__ = ["WorkflowLibrary", "LibraryError", "slug", "tool_name", "RESERVED_INPUTS",
           "TOOL_PREFIX", "MAX_PER_OWNER"]

#: Every published tool starts with this. Short, and never a name the
#: coordinator-side workflow tools (`workflow_start`, ...) already use.
TOOL_PREFIX = "wf_"

#: Argument names a published tool keeps for itself; a workflow cannot declare
#: an input called one of these, or a caller could not tell which was meant.
RESERVED_INPUTS = ("overrides", "wait_seconds", "idempotency_key")

#: How many saved workflows one owner keeps. A library is something a person
#: curates; a script filling it is a bug worth stopping.
MAX_PER_OWNER = 200

_NAME = re.compile(r"^[a-z0-9][a-z0-9_]{0,47}$")


class LibraryError(ValueError):
    """The request cannot be honoured; the message says what to change."""


def slug(text: str) -> str:
    """`report.publish` -> `report_publish`: lower case, `[a-z0-9_]`, at most 48."""
    out = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return out[:48].strip("_")


def tool_name(name: str) -> str:
    return TOOL_PREFIX + name


def _view(row, *, with_definition: bool = False) -> Dict[str, Any]:
    definition = json.loads(row.definition_json or "{}")
    inputs = definition.get("inputs") or {}
    out = {
        "name": row.name, "tool": tool_name(row.name), "title": row.title or "",
        "description": row.description or "", "workflow_id": row.workflow_id,
        "version": row.workflow_version, "enabled": bool(row.enabled),
        "allow_overrides": bool(row.allow_overrides), "publishable": bool(inputs),
        "nodes": len(definition.get("nodes") or []),
        "updated_at": row.updated_at.isoformat() + "Z" if getattr(row, "updated_at", None) else "",
    }
    if with_definition:
        out["definition"] = definition
    return out


def check_inputs_schema(definition: WorkflowDefinition) -> None:
    """The declared inputs schema must be one the validator can honour in full
    and must not use a name a published tool keeps for itself."""
    if not definition.inputs:
        return
    problems = schema_check.schema_problems(definition.inputs)
    if problems:
        raise LibraryError("the `inputs` schema cannot be checked as written: " + "; ".join(problems[:4]))
    reserved = [k for k in (definition.inputs.get("properties") or {}) if k in RESERVED_INPUTS]
    if reserved:
        raise LibraryError(f"an input cannot be called {reserved[0]!r}: a published tool keeps "
                           f"{list(RESERVED_INPUTS)} for itself")


class WorkflowLibrary:
    """Rows in, plain dicts out. Every method is owner-scoped: a name is unique
    per owner, and another owner's workflow does not exist as far as this
    object can tell."""

    def save(self, owner: str, definition: WorkflowDefinition, *, name: str = "",
             enabled: Optional[bool] = None, allow_overrides: Optional[bool] = None) -> Dict[str, Any]:
        from core.database import SavedWorkflowRow, SessionLocal
        if not owner:
            raise LibraryError("a saved workflow needs an owner")
        chosen = name.strip() or slug(definition.id)
        if not _NAME.match(chosen):
            raise LibraryError(
                f"{chosen!r} is not a usable name: lower-case letters, digits and underscores, "
                "starting with a letter or digit, at most 48 characters")
        check_inputs_schema(definition)
        if enabled and not definition.inputs:
            raise LibraryError("a workflow is published as a tool only if it declares `inputs` "
                               "(a JSON schema for what a run takes)")
        db = SessionLocal()
        try:
            row = (db.query(SavedWorkflowRow)
                   .filter(SavedWorkflowRow.owner == owner, SavedWorkflowRow.name == chosen).first())
            if row is None:
                if db.query(SavedWorkflowRow).filter(SavedWorkflowRow.owner == owner).count() >= MAX_PER_OWNER:
                    raise LibraryError(f"the library is full ({MAX_PER_OWNER} workflows); delete one first")
                row = SavedWorkflowRow(id=f"wfl_{uuid.uuid4().hex[:20]}", owner=owner, name=chosen,
                                       enabled=False, allow_overrides=True, schema_version=1)
                db.add(row)
            row.title = definition.title
            row.description = definition.description
            row.workflow_id = definition.id
            row.workflow_version = definition.version
            row.definition_json = json.dumps(definition.to_dict(), ensure_ascii=False, sort_keys=True)
            if enabled is not None:
                row.enabled = bool(enabled)
            elif not definition.inputs:
                row.enabled = False            # a saved-over definition that lost its inputs is unpublished
            if allow_overrides is not None:
                row.allow_overrides = bool(allow_overrides)
            db.commit()
            db.refresh(row)
            return _view(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, owner: str, name: str, *, with_definition: bool = True) -> Optional[Dict[str, Any]]:
        from core.database import SavedWorkflowRow, SessionLocal
        db = SessionLocal()
        try:
            row = (db.query(SavedWorkflowRow)
                   .filter(SavedWorkflowRow.owner == owner, SavedWorkflowRow.name == name).first())
            return _view(row, with_definition=with_definition) if row else None
        finally:
            db.close()

    def definition(self, owner: str, name: str) -> Optional[WorkflowDefinition]:
        found = self.get(owner, name)
        if found is None:
            return None
        try:
            return WorkflowDefinition.parse(found["definition"])
        except ContractError as exc:                   # a stored row the contract no longer accepts
            raise LibraryError(f"the saved workflow {name!r} no longer validates: {exc.path}: {exc.message}")

    def list(self, owner: str, *, enabled_only: bool = False) -> List[Dict[str, Any]]:
        from core.database import SavedWorkflowRow, SessionLocal
        db = SessionLocal()
        try:
            query = db.query(SavedWorkflowRow).filter(SavedWorkflowRow.owner == owner)
            if enabled_only:
                query = query.filter(SavedWorkflowRow.enabled.is_(True))
            return [_view(r) for r in query.order_by(SavedWorkflowRow.name.asc()).all()]
        finally:
            db.close()

    def update(self, owner: str, name: str, *, enabled: Optional[bool] = None,
               allow_overrides: Optional[bool] = None) -> Dict[str, Any]:
        from core.database import SavedWorkflowRow, SessionLocal
        db = SessionLocal()
        try:
            row = (db.query(SavedWorkflowRow)
                   .filter(SavedWorkflowRow.owner == owner, SavedWorkflowRow.name == name).first())
            if row is None:
                raise LibraryError(f"no saved workflow named {name!r}")
            if enabled:
                inputs = (json.loads(row.definition_json or "{}")).get("inputs")
                if not inputs:
                    raise LibraryError("a workflow is published as a tool only if it declares `inputs`")
            if enabled is not None:
                row.enabled = bool(enabled)
            if allow_overrides is not None:
                row.allow_overrides = bool(allow_overrides)
            db.commit()
            db.refresh(row)
            return _view(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def delete(self, owner: str, name: str) -> bool:
        from core.database import SavedWorkflowRow, SessionLocal
        db = SessionLocal()
        try:
            n = (db.query(SavedWorkflowRow)
                 .filter(SavedWorkflowRow.owner == owner, SavedWorkflowRow.name == name)
                 .delete(synchronize_session=False))
            db.commit()
            return bool(n)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
