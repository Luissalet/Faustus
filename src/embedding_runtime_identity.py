"""Side-effect-free identities of already materialized embedding runtimes."""
import hashlib
from dataclasses import dataclass, field


def _client_identity(client):
    values = vars(client)
    if any(key not in values for key in ('url', 'model', '_dim')):
        raise ValueError('embedding client query identity unavailable')
    if not isinstance(values['url'], str) or not isinstance(values['model'], str):
        raise ValueError('embedding client query identity malformed')
    dimension = values['_dim']
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
        raise ValueError('embedding client dimension unavailable')
    secret = values.get('api_key')
    if secret is not None and not isinstance(secret, str):
        raise ValueError('embedding client credentials malformed')
    for key in ('_batch_size', '_max_chars'):
        if key in values and (isinstance(values[key], bool) or
                not isinstance(values[key], int) or values[key] <= 0):
            raise ValueError('embedding client query bounds malformed')
    return (hashlib.sha256(values['url'].encode()).hexdigest(), values['model'],
            dimension, hashlib.sha256((secret or '').encode()).hexdigest(),
            id(values.get('_model')), id(values.get('_client')),
            values.get('_batch_size'), values.get('_max_chars'))


def _collection_identity(collection):
    values = vars(collection)
    model = values.get('_model')
    metadata = vars(model) if model is not None else {}
    return (id(values.get('_client')), tuple(str(metadata.get(key, ''))
            for key in ('id', 'name', 'tenant', 'database')))


@dataclass(frozen=True)
class RuntimeIdentity:
    keys: tuple
    references: tuple = field(compare=False, repr=False)


def vector_identity(vector):
    """Capture references/fields without getters that initialize models or stores.

    Health/lane properties are the existing side-effect-free runtime contract;
    client fields and Chroma namespace come from already materialized dicts.
    References prevent object id recycling while an identity remains live.
    """
    lanes = tuple(vector._lanes)
    if not vector.healthy or not lanes:
        raise ValueError('embedding runtime unavailable')
    keys = (id(vector), tuple(
        (id(lane), id(lane.collection), id(lane.client), lane.name,
         lane.collection_name, lane.model, lane.dimension, lane.fingerprint,
         hashlib.sha256(str(lane.url).encode()).hexdigest(), lane.healthy,
         _client_identity(lane.client), _collection_identity(lane.collection))
        for lane in lanes))
    references = (vector, *lanes, *(lane.collection for lane in lanes),
                  *(lane.client for lane in lanes),
                  *(vars(lane.client).get('_model') for lane in lanes),
                  *(vars(lane.client).get('_client') for lane in lanes),
                  *(vars(lane.collection).get('_client') for lane in lanes))
    return RuntimeIdentity(keys, references)
