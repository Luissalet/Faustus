"""Stable routing for goals that outlive a project's workspace binding.

Lock routing before choosing a store. A writer holding an old project record
must see the managed anchor activated by a concurrent workspace rebind.
"""
from contextlib import contextmanager
import hashlib
import json
import os
import stat
import threading

from core.kernel_file_lock import KernelFileLock

MARKER = 'managed.v1'
_held = threading.local()


def managed_root():
    from core.constants import DATA_DIR
    return os.path.join(DATA_DIR, 'project_objectives')


def managed_dir(project):
    ident = (project or {}).get('id')
    if not isinstance(ident, str) or not ident.strip():
        return ''
    # Encode an exact tuple; separators, slashes and hostile names never enter
    # a filesystem path. A null legacy owner stays distinct from an empty one.
    identity = json.dumps([project.get('owner'), ident], ensure_ascii=True,
                          separators=(',', ':')).encode('utf-8')
    return os.path.join(managed_root(), hashlib.sha256(identity).hexdigest())


def _check(project):
    base = managed_dir(project)
    if not base:
        return
    for path, directory in ((managed_root(), True), (base, True),
                            (os.path.join(base, MARKER), False),
                            (os.path.join(base, 'routing.lock'), False)):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
                or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1)
                or (directory and not stat.S_ISDIR(info.st_mode))
                or (not directory and not stat.S_ISREG(info.st_mode))):
            raise PermissionError('Project objective routing must use regular, unlinked metadata paths')


def active(project):
    base = managed_dir(project)
    return bool(base and os.path.lexists(os.path.join(base, MARKER)))


def directory(project):
    workspace = (project or {}).get('workspace') or ''
    base = managed_dir(project)
    if base and (not workspace or active(project)):
        return base
    return os.path.join(workspace, '.odysseus') if workspace else ''


@contextmanager
def routing_guard(project):
    base = managed_dir(project)
    if not base:
        yield
        return
    _check(project)
    # realpath the directory, not a Windows byte-locked file.
    key = os.path.normcase(os.path.realpath(base))
    if key.startswith('\\\\?\\unc\\'):
        key = '\\\\' + key[8:]
    elif key.startswith('\\\\?\\'):
        key = key[4:]
    held = getattr(_held, 'paths', None)
    if held is None:
        held = _held.paths = set()
    if key in held:
        yield
        return
    with KernelFileLock(os.path.join(base, 'routing.lock')):
        _check(project)
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)


def activate(project, atomic_write):
    """Caller holds routing_guard and has already copied every source file.

    Marker last: a failed migration leaves partial copies unselected and can
    safely be retried from the still-authoritative portable files.
    """
    _check(project)
    base = managed_dir(project)
    if not base:
        raise ValueError('Managed objectives require a project identity')
    atomic_write(os.path.join(base, MARKER), b'1\n')
