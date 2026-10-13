"""Adapt native reservations to the family's existing global lease service."""
from __future__ import annotations

import uuid
import weakref
import os

_clients = weakref.WeakSet()


class ResourceClient:
    def __init__(self, workspace=''):
        self.workspace = os.path.realpath(workspace or os.getcwd())
        self.identity = 'native-' + uuid.uuid4().hex
        self.holders = set()
        _clients.add(self)

    def owner(self, worker):
        return self.identity + ':' + worker

    def worker(self, owner):
        prefix = self.identity + ':'
        return owner[len(prefix):] if owner.startswith(prefix) else owner

    def call(self, action, worker, resources=()):
        from src.shared_coordination import call
        result = call('hub_coordination_resources', {'action': action, 'owner': self.owner(worker),
                      'resources': list(resources), 'workspace': self.workspace})
        if not isinstance(result, dict) or (not result.get('ok') and 'conflicts' not in result):
            raise RuntimeError('Shared reservations are unavailable; existing ownership cannot be checked')
        if action == 'claim' and result.get('ok'):
            self.holders.add(worker)
        return result

    def renew(self):
        for worker in list(self.holders):
            self.call('renew', worker)


def client(workspace=''):
    from src.shared_coordination import status
    return ResourceClient(workspace) if status()['bound'] else None


def renew():
    for item in list(_clients):
        item.renew()


class ClaimsBackend:
    def __init__(self, shared):
        self.shared = shared

    def acquire(self, resource, holder):
        return bool(self.shared.call('claim', holder, [resource]).get('ok'))

    def release(self, resource, holder):
        return bool(self.shared.call('release', holder, [resource]).get('released'))

    def holder(self, resource):
        conflicts = self.shared.call('check', '__probe__', [resource]).get('conflicts') or []
        owner = conflicts[0]['owner'] if conflicts else ''
        return self.shared.worker(owner)
