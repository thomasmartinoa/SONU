"""Desktop entries modelled on a real Hyprland machine, for tests and the eval suite."""

from __future__ import annotations

from pathlib import Path

SAMPLE_APPS: dict[str, str] = {
    "code-oss.desktop": (
        "[Desktop Entry]\nName=Code - OSS\nGenericName=Text Editor\nExec=/usr/bin/code-oss %F\n"
        "Type=Application\nCategories=Utility;TextEditor;Development;IDE;\nKeywords=vscode;\n"
    ),
    "codium.desktop": (
        "[Desktop Entry]\nName=VSCodium\nExec=/usr/bin/codium %F\nType=Application\n"
        "StartupWMClass=VSCodium\nCategories=Utility;TextEditor;Development;IDE;\n"
    ),
    "chromium.desktop": (
        "[Desktop Entry]\nName=Chromium\nGenericName=Web Browser\nExec=/usr/bin/chromium %U\n"
        "Type=Application\nCategories=Network;WebBrowser;\nStartupWMClass=chromium\n"
    ),
    "zen.desktop": (
        "[Desktop Entry]\nName=Zen Browser\nExec=/usr/bin/zen-browser %u\nType=Application\n"
        "Categories=Network;WebBrowser;\nStartupWMClass=zen\n"
    ),
    "kitty.desktop": (
        "[Desktop Entry]\nType=Application\nName=kitty\nGenericName=Terminal emulator\nExec=kitty\n"
        "Categories=System;TerminalEmulator;\n"
    ),
    "thunar.desktop": (
        "[Desktop Entry]\nName=Thunar File Manager\nExec=thunar %F\nType=Application\n"
        "Categories=System;FileManager;\nStartupWMClass=Thunar\n"
    ),
    "whatsapp.desktop": "[Desktop Entry]\nName=WhatsApp Desktop\nExec=whatsapp-desktop\nType=Application\n",
    "btop.desktop": "[Desktop Entry]\nName=btop++\nExec=btop\nType=Application\nTerminal=true\n",
}


def write_desktop_files(applications_dir: Path, files: dict[str, str] | None = None) -> None:
    applications_dir.mkdir(parents=True, exist_ok=True)
    for name, content in (files or SAMPLE_APPS).items():
        (applications_dir / name).write_text(content)
