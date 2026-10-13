"""Use the shared Council repository with this Faustus installation's data path."""
import os
import sys
from src.constants import DATA_DIR
from src.hoard_link.coordination import council_store as _shared
_shared.COUNCIL_DB = os.path.join(DATA_DIR, "council.db")
sys.modules[__name__] = _shared
