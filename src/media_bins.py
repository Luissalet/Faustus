"""Family discovery finds environment overrides, managed binaries and Windows installs."""
from src.hoard_link.media import bins


def which(name):
    return bins.find(name).path
