"""Setting up SONU's browser extension (D70): the native messaging host's manifests, its launcher, and
the extension ready to add to the browsers.

Run by the user (``sonu browser install``) — it writes into their browsers' folders:

* Chromium-based browsers read ``<profile folder>/NativeMessagingHosts/`` (measured: Chromium 153
  reads it from its user data folder, not from ``$XDG_CONFIG_HOME/chromium``) — written only for the
  browsers whose folder exists.
* Firefox and Zen read ``~/.mozilla/native-messaging-hosts/`` (measured with Zen 1.23: not
  ``~/.config/zen`` or ``~/.config/mozilla``; creating ``~/.mozilla`` doesn't move Zen's profiles).

The extension itself is added in the browser by the user: Chromium "Load unpacked" (developer mode),
Zen/Firefox "Install Add-on From File" (unsigned: needs ``xpinstall.signatures.required`` false).
"""

from __future__ import annotations

import json
import shutil
import sys
import zipfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

HOST_NAME = "dev.sonu.browser"
CHROMIUM_ID = "iachfigjekokbhfjnieamekckdbignof"  # fixed by the manifest's "key"
GECKO_ID = "browser@sonu.local"
# Chromium-based browsers' profile folders (in ~/.config) — a manifest goes only where one exists.
CHROMIUM_FOLDERS = (
    "chromium",
    "google-chrome",
    "google-chrome-beta",
    "BraveSoftware/Brave-Browser",
    "vivaldi",
    "microsoft-edge",
)
FIREFOX_PROFILES = (".config/zen", ".zen", ".mozilla/firefox", ".config/mozilla/firefox")


@dataclass(frozen=True, slots=True)
class Setup:
    launcher: Path
    extension: Path  # the unpacked extension (Chromium: Load unpacked)
    xpi: Path  # the same, packed (Zen/Firefox: Install Add-on From File)
    manifests: tuple[Path, ...]


def extension_files() -> dict[str, bytes]:
    folder = resources.files("sonu.browser") / "extension"
    return {name: (folder / name).read_bytes() for name in ("manifest.json", "background.js")}


def extension_version() -> str:
    """The version of the extension this SONU brings (a browser may still run an older one)."""
    return str(json.loads(extension_files()["manifest.json"])["version"])


def plan(home: Path, data_dir: Path) -> Setup:
    base = data_dir / "browser"
    manifests = [
        home / ".config" / name / "NativeMessagingHosts" / f"{HOST_NAME}.json"
        for name in CHROMIUM_FOLDERS
        if (home / ".config" / name).is_dir()
    ]
    if any((home / folder).is_dir() for folder in FIREFOX_PROFILES):
        manifests.append(home / ".mozilla" / "native-messaging-hosts" / f"{HOST_NAME}.json")
    return Setup(base / "sonu-browser-host", base / "extension", base / "sonu.xpi", tuple(manifests))


def host_manifest(launcher: Path, firefox: bool) -> dict[str, object]:
    manifest: dict[str, object] = {
        "name": HOST_NAME,
        "description": "SONU: your local assistant's link to its browser extension",
        "path": str(launcher),
        "type": "stdio",
    }
    if firefox:
        manifest["allowed_extensions"] = [GECKO_ID]
    else:
        manifest["allowed_origins"] = [f"chrome-extension://{CHROMIUM_ID}/"]
    return manifest


def install(setup: Setup, python: str = sys.executable) -> None:
    setup.launcher.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The browser starts this (no arguments of SONU's own can go in a manifest): SONU's own Python.
    setup.launcher.write_text(f"#!/bin/sh\nexec '{python}' -I -m sonu.browser.host \"$@\"\n")
    setup.launcher.chmod(0o700)
    if setup.extension.exists():
        shutil.rmtree(setup.extension)
    setup.extension.mkdir(parents=True)
    files = extension_files()
    with zipfile.ZipFile(setup.xpi, "w", zipfile.ZIP_DEFLATED) as xpi:
        for name, data in files.items():
            (setup.extension / name).write_bytes(data)
            xpi.writestr(name, data)
    for path in setup.manifests:
        path.parent.mkdir(parents=True, exist_ok=True)
        firefox = ".mozilla" in path.parts
        path.write_text(json.dumps(host_manifest(setup.launcher, firefox), indent=2) + "\n")


def uninstall(setup: Setup) -> list[Path]:
    removed = [path for path in setup.manifests if path.exists()]
    for path in removed:
        path.unlink()
    if setup.launcher.parent.exists():
        shutil.rmtree(setup.launcher.parent)
        removed.append(setup.launcher.parent)
    return removed
