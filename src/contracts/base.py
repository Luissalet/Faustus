"""Compatibility path for the family shared contract primitives."""
import sys
from src.hoard_link.coordination import base as _shared
sys.modules[__name__] = _shared
