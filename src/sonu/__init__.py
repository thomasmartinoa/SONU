"""SONU — System-Oriented Neural Utility.

A local-first, privacy-first AI operator for the Linux desktop.
"""

from typing import Any

__all__ = ["__version__"]


def __getattr__(name: str) -> Any:
    """``__version__`` is looked up only when asked: importlib.metadata costs ~70 ms, and the
    thin client (``sonu <request>`` with the daemon running) must start in a few milliseconds."""
    if name != "__version__":
        raise AttributeError(name)
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("sonu")
    except PackageNotFoundError:  # running from a source tree without an install
        return "0.0.0+unknown"
