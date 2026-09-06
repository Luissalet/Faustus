"""state_mirror/adapters/connections.py -- what is wired up, and nothing about
whether it works.

`connection_state.v1` for the two families of outside thing this install can be
configured to talk to: the model endpoints in `model_endpoints`, and the HTTP
integrations `src/integrations.py` keeps on disk.

**Configured, authenticated and reachable are three different facts.** They
are three fields for that reason and this adapter fills exactly one of them.
Knowing a credential is STORED is not knowing it is accepted -- a revoked
token sits in the file looking exactly like a live one -- and knowing a base
URL is set is not knowing anything answers at it. So `authenticated` and
`reachable` are omitted rather than defaulted, `contracts._flag` reads an
absent tri-state boolean as "not observed", and a caller about to route work
to an integration is told to go and check instead of being handed a `True`
nobody earned.

**No secret ever enters a state row, and that includes a shortened one.**
`integrations.load_integrations()` hands back records with `api_key`
DECRYPTED for runtime use, and `mask_integration_secret` produces `sk-a****`,
which is still four characters of a live credential. Neither goes anywhere
near an observation. Nor does `base_url`: the Discord webhook preset says in
so many words that the secret is embedded in the URL, so a base URL is a
credential on some rows and cannot be published on any. What this adapter
emits from a connection record is a boolean it computed from those values --
`bool(record.get("api_key"))` cannot carry the key -- and never a string that
came out of one.

The display name on the ENTITY is the exception, and a deliberate one: it is
the label the person typed for their own connection and it is what a row has
to be called to be recognisable. It is not read from a credential field.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from src.contracts.base import now_iso
from src.state_mirror.adapters.base import (
    Scope,
    ThreadedAdapter,
    entity,
    observation,
)
from src.state_mirror.contracts import StateEntity, StateObservation, entity_id

logger = logging.getLogger(__name__)

__all__ = ["ConnectionsAdapter", "UNOBSERVED_FIELDS", "SOURCE", "SCHEMA"]

SOURCE = "connections"
SCHEMA = "connection_state.v1"

#: `connection_state.v1` fields this adapter does not fill, and why. Three of
#: the four are refusals rather than gaps: the data is right there and
#: publishing it would be a lie about what was checked.
UNOBSERVED_FIELDS: Dict[str, str] = {
    "authenticated": "a stored credential is not an accepted one, and nothing "
                     "here presents it to anybody; this becomes observable the "
                     "day something calls the connection and reports back",
    "reachable": "nothing in a sweep opens a socket -- a probe per connection "
                 "on a timer is a cost the mirror is not allowed to impose on "
                 "the machine it describes",
    "granted_actions": "neither store records what a connection has been "
                       "permitted to do; integrations carry a base URL and an "
                       "auth mode, which is capability, not grant",
    "expires_at": "neither an integration record nor a model endpoint carries "
                  "an expiry; ProviderAuthSession keeps a last_refresh, which "
                  "is when a token was renewed and not when it dies",
}

#: The two families of connection, as id prefixes. Prefixed so a model
#: endpoint and an integration can never collide on an id, and so a reader of
#: an id can tell which store to go back to.
ENDPOINT_PREFIX = "endpoint:"
INTEGRATION_PREFIX = "integration:"

#: Auth modes that need no credential of their own. An integration set to one
#: of these is fully configured with an empty `api_key`, so "configured"
#: cannot simply mean "has a key".
CREDENTIAL_FREE_AUTH: Tuple[str, ...] = ("none", "")


def read_integrations() -> List[Dict[str, Any]]:
    """The integration records on disk. `[]` when the file is absent or broken.

    `load_integrations` already answers `[]` for both, and it already logs
    which. Every caller of this function must treat what comes back as
    credential-bearing: `api_key` arrives decrypted.
    """
    from src.integrations import load_integrations

    records = load_integrations()
    return [r for r in records if isinstance(r, dict)]


def read_endpoints() -> List[Dict[str, Any]]:
    """Model endpoints, reduced to the non-secret columns before they leave.

    The projection happens HERE rather than at the observation, so that no
    caller downstream is ever holding a row with `api_key` on it. `configured`
    is computed from whether a key exists, and the boolean is what travels.
    """
    from core.database import ModelEndpoint, SessionLocal

    out: List[Dict[str, Any]] = []
    session = SessionLocal()
    try:
        for row in session.query(ModelEndpoint).all():
            out.append({
                "id": str(getattr(row, "id", "") or ""),
                "name": str(getattr(row, "name", "") or ""),
                "owner": getattr(row, "owner", None),
                "enabled": bool(getattr(row, "is_enabled", True)),
                "has_base_url": bool(str(getattr(row, "base_url", "") or "").strip()),
                "has_key": bool(str(getattr(row, "api_key", "") or "").strip()),
                "has_session": bool(str(getattr(row, "provider_auth_id", "") or "").strip()),
            })
    finally:
        session.close()
    return out


def integration_configured(record: Dict[str, Any]) -> bool:
    """Whether this integration has everything it needs to be called.

    A base URL, and -- unless the auth mode says no credential is needed -- a
    stored key. Returns a boolean computed from those values and never any part
    of them, which is what makes it safe to put the answer in a state row.

    Deliberately not `enabled`: a connection the user switched off is still
    configured, and folding the two would lose the difference between "set up
    and paused" and "never set up".
    """
    if not str(record.get("base_url") or "").strip():
        return False
    auth = str(record.get("auth_type") or "").strip().lower()
    if auth in CREDENTIAL_FREE_AUTH:
        return True
    return bool(str(record.get("api_key") or "").strip())


def _owned_by(scope: Scope, owner: Any) -> bool:
    """Whether a row with this owner belongs in this scope's answer.

    A NULL owner is the historical shared row -- `ModelEndpoint.owner` says so
    -- and a shared endpoint is every owner's, so it is never filtered out.
    """
    if owner is None or not str(owner).strip():
        return True
    return str(owner).strip() == str(scope.owner or "").strip()


class ConnectionsAdapter(ThreadedAdapter):
    """Model endpoints and HTTP integrations, as `connection_state.v1` rows."""

    name = SOURCE
    schemas = (SCHEMA,)

    def _rows(self, scope: Scope) -> List[Tuple[str, str, Dict[str, Any]]]:
        """`(identifier, display name, state)` for every connection in scope.

        Only ever `configured` in the state. See `UNOBSERVED_FIELDS` for the
        other three and why each of them is absent rather than false. Each
        store is read on its own, so a database that will not open costs the
        endpoints and not the integrations beside them.
        """
        rows: List[Tuple[str, str, Dict[str, Any]]] = []

        for record in self._safe(read_integrations, default=()) or ():
            # A row that is not a record is skipped rather than crashed on:
            # `integrations.json` is a file a person can edit, and one bad
            # line there must not cost every other connection its row.
            if not isinstance(record, dict):
                continue
            identifier = str(record.get("id") or "").strip()
            if not identifier:
                continue
            rows.append((INTEGRATION_PREFIX + identifier,
                         str(record.get("name") or "").strip(),
                         {"configured": integration_configured(record)}))

        for endpoint in self._safe(read_endpoints, default=()) or ():
            if not isinstance(endpoint, dict):
                continue
            identifier = str(endpoint.get("id") or "").strip()
            if not identifier or not _owned_by(scope, endpoint.get("owner")):
                continue
            rows.append((ENDPOINT_PREFIX + identifier,
                         str(endpoint.get("name") or "").strip(),
                         {"configured": bool(endpoint.get("has_base_url"))}))
        return rows

    def discover(self, scope: Scope) -> List[StateEntity]:
        out: List[StateEntity] = []
        for identifier, display_name, _state in self._safe(self._rows, scope, default=()) or ():
            row = self._safe(
                entity, "connection", identifier,
                scope=scope,
                schema=SCHEMA,
                display_name=display_name or identifier,
                # A connection names a resource outside this machine, and
                # section 19 marks a field whose EXISTENCE is private as
                # restricted even when its value is not a secret.
                sensitivity="restricted",
                default=None,
            )
            if row is not None:
                out.append(row)
        return out

    def observe(self, scope: Scope) -> List[StateObservation]:
        stamp = now_iso()
        out: List[StateObservation] = []
        for identifier, _display_name, state in self._safe(self._rows, scope, default=()) or ():
            target = self._safe(entity_id, "connection", scope.owner, identifier,
                                namespace=scope.namespace, default="")
            if not target:
                continue
            obs = observation(
                target, SOURCE, state,
                scope=scope,
                schema=SCHEMA,
                # The configuration store is the authority on what is
                # configured, and this read is of that store.
                epistemic="observed",
                observed_at=stamp,
                partial=True,
                sensitivity="restricted",
            )
            if obs is not None:
                out.append(obs)
        return out
