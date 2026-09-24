"""SONU — System-Oriented Neural Utility.

A local-first, privacy-first AI operator for the Linux desktop.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version

try:
    __version__ = _dist_version("sonu")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
