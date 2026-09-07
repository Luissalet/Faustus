"""Close disagreements only after their sources independently agree again.

The append-only observations retain all sources, unlike the reduced field which
keeps just one. No new probe, model inference or winner selected by arrival order.
"""
from src.state_mirror import freshness
from src.state_mirror.contracts import FieldState, epistemic_rank
from src.state_mirror.reducers import CONFLICTING_EPISTEMICS, _values_equal


def converged(conflict, observations, state, *, now=None):
    if not observations or len(observations) > 32:
        return False
    required = {str(c.get('source') or ''): c for c in conflict.claims}
    if '' in required or len(required) < 2:
        return False
    latest = {o.source: o for o in observations}
    if not set(required).issubset(latest):
        return False
    held = state.fields.get(conflict.field)
    if held is None:
        return False
    instant = freshness._now(now)
    for source, sample in latest.items():
        if (sample.entity_id != conflict.entity_id or sample.owner != conflict.owner
                or sample.namespace != conflict.namespace or sample.schema != state.schema):
            return False
        if source not in required and sample.epistemic not in CONFLICTING_EPISTEMICS:
            continue
        if conflict.field not in sample.state or sample.epistemic not in CONFLICTING_EPISTEMICS:
            return False
        observed = freshness._parse(sample.observed_at)
        if observed is None or observed > instant:
            return False
        original = required.get(source)
        if original:
            before = freshness._parse(original.get('observed_at'))
            if (before is None or observed <= before or
                    epistemic_rank(sample.epistemic) > epistemic_rank(original.get('epistemic'))):
                return False
        value = sample.state[conflict.field]
        measured = FieldState(value=value, epistemic=sample.epistemic,
            observed_at=sample.observed_at, ttl_seconds=sample.valid_for_seconds)
        if freshness.rate_field(measured, schema=sample.schema, field=conflict.field, now=now) != 'fresh':
            return False
        if not _values_equal(value, held.value):
            return False
    return True


def resolve_confirmed(store, state, *, now=None):
    """Return conflicts atomically settled against an unchanged observation log."""
    conflicts = store.conflicts(owner=state.owner, namespace=state.namespace,
                                entity_id=state.entity_id, open_only=True, limit=100)
    settled = []
    for conflict in conflicts:
        cursor = store.observation_cursor(state.entity_id)
        samples = store.latest_field_observations(state.entity_id, conflict.field)
        if not converged(conflict, samples, state, now=now):
            continue
        sources = sorted({c.get('source', '') for c in conflict.claims})
        resolution = ('Fresh observations agree on ' + conflict.field + ': ' + ', '.join(sources))[:512]
        if store.settle_conflict(conflict.id, status='resolved', resolution=resolution,
                                 observation_cursor=cursor):
            settled.append(conflict)
    return settled
