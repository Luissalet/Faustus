"""Single machine-readable owner/tool contract; no model or network activity.

Node ships a byte-identical copy of _data/family-services.json. Its parity is
checked by tests and scripts/sync_vendored.py before distribution.
"""
import json
from pathlib import Path
from types import MappingProxyType

_DATA = json.loads((Path(__file__).parent / "_data" / "family-services.json").read_text(encoding="utf-8"))
OWNERS = MappingProxyType({name: spec["owner"] for name, spec in _DATA["services"].items()})
SERVICES = tuple((name, spec["owner"], tuple(spec["tools"])) for name, spec in _DATA["services"].items())
