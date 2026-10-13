"""Compatibility path for the family shared Council contracts."""
import sys
from src.hoard_link.coordination import council_contracts as _shared
sys.modules[__name__] = _shared
