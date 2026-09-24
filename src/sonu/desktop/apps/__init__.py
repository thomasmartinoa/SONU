"""Installed applications: XDG desktop entries, name resolution and window-class matching."""

from sonu.desktop.apps.catalog import AppCatalog, application_dirs
from sonu.desktop.apps.classes import expected_classes, window_matches
from sonu.desktop.apps.entry import AppEntry, DesktopEntryError, parse_desktop_entry, parse_exec
from sonu.desktop.apps.resolver import AppResolver, Resolution, normalize

__all__ = [
    "AppCatalog",
    "AppEntry",
    "AppResolver",
    "DesktopEntryError",
    "Resolution",
    "application_dirs",
    "expected_classes",
    "normalize",
    "parse_desktop_entry",
    "parse_exec",
    "window_matches",
]
