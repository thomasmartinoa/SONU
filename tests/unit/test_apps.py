from __future__ import annotations

from pathlib import Path

import pytest

from sonu.desktop.apps import (
    AppCatalog,
    AppResolver,
    DesktopEntryError,
    application_dirs,
    expected_classes,
    parse_exec,
    window_matches,
)

# Desktop files modelled on the author's machine.
USER_APPS = {
    "code-oss.desktop": """[Desktop Entry]
Name=Code - OSS
Comment=Code Editing. Redefined.
GenericName=Text Editor
Exec=/usr/bin/code-oss %F
Icon=com.visualstudio.code.oss
Type=Application
Categories=Utility;TextEditor;Development;IDE;
Keywords=vscode;
[Desktop Action new-empty-window]
Name=New Empty Window
Exec=/usr/bin/code-oss --new-window %F
""",
    "code-oss-url-handler.desktop": """[Desktop Entry]
Name=Code - OSS - URL Handler
Exec=/usr/bin/code-oss --open-url %U
Type=Application
NoDisplay=true
""",
    "codium.desktop": """[Desktop Entry]
Name=VSCodium
Exec=/usr/bin/codium %F
Type=Application
StartupWMClass=VSCodium
Categories=Utility;TextEditor;Development;IDE;
""",
    "chromium.desktop": """[Desktop Entry]
Version=1.0
Name=Chromium
Name[de]=Chromium-Webbrowser
GenericName=Web Browser
Exec=/usr/bin/chromium %U
Type=Application
Categories=Network;WebBrowser;
StartupWMClass=chromium
""",
    "zen.desktop": """[Desktop Entry]
Name=Zen Browser
Exec=/usr/bin/zen-browser %u
Type=Application
Categories=Network;WebBrowser;
StartupWMClass=zen
""",
    "kitty.desktop": """[Desktop Entry]
Type=Application
Name=kitty
GenericName=Terminal emulator
TryExec=kitty
Exec=kitty
Categories=System;TerminalEmulator;
""",
    "whatsapp.desktop": """[Desktop Entry]
Name=WhatsApp Desktop
Exec=whatsapp-desktop
Type=Application
""",
    # Installed by kitty itself (setup.py): a hidden URL handler that runs the same program.
    "kitty-open.desktop": """[Desktop Entry]
Version=1.0
Type=Application
Name=kitty URL Launcher
GenericName=Terminal emulator
Comment=Open URLs with kitty
StartupNotify=true
TryExec=kitty
Exec=kitty +open %U
Icon=kitty
Categories=System;TerminalEmulator;
NoDisplay=true
""",
    "thunar.desktop": """[Desktop Entry]
Name=Thunar File Manager
Exec=thunar %F
Type=Application
Categories=System;Utility;Core;GTK;FileTools;FileManager;
StartupWMClass=Thunar
""",
    # Visible, and runs the same program as Thunar.
    "thunar-bulk-rename.desktop": """[Desktop Entry]
Name=Bulk Rename
Exec=thunar --bulk-rename %F
Type=Application
Categories=System;Utility;Core;GTK;FileTools;
""",
}


def _write(directory: Path, files: dict[str, str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(content)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    user = tmp_path / "home" / ".local" / "share"
    system = tmp_path / "usr" / "share"
    _write(user / "applications", USER_APPS)
    _write(
        system / "applications",
        {
            # Shadowed by the user's own chromium.desktop (same desktop-file ID).
            "chromium.desktop": "[Desktop Entry]\nName=System Chromium\nExec=chromium\nType=Application\n",
            # Hidden by a user override below.
            "htop.desktop": "[Desktop Entry]\nName=htop\nExec=htop\nType=Application\nTerminal=true\n",
            "gnome-only.desktop": (
                "[Desktop Entry]\nName=GNOME Thing\nExec=gthing\nType=Application\nOnlyShowIn=GNOME;\n"
            ),
            "broken.desktop": '[Desktop Entry]\nName=Broken\nExec="unterminated\nType=Application\n',
            "org.kde.dolphin.desktop": (
                "[Desktop Entry]\nName=Dolphin\nExec=dolphin %u\nType=Application\n"
                "Categories=System;FileManager;\n"
            ),
            "sub/nested.desktop": "[Desktop Entry]\nName=Nested\nExec=nested\nType=Application\n",
        },
    )
    (user / "applications" / "htop.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=htop\nExec=htop\nHidden=true\n"
    )
    return {
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_DIRS": str(system),
        "XDG_CURRENT_DESKTOP": "Hyprland",
    }


@pytest.fixture
def catalog(env: dict[str, str]) -> AppCatalog:
    return AppCatalog.load(env, which=lambda name: f"/usr/bin/{name}")


# ---------------------------------------------------------------- Exec parsing
@pytest.mark.parametrize(
    ("exec_value", "argv", "accepts"),
    [
        ("/usr/bin/code-oss %F", ("/usr/bin/code-oss",), "files"),
        ("chromium %U", ("chromium",), "urls"),
        ('sh -c "echo \\"hi\\" \\$HOME"', ("sh", "-c", 'echo "hi" $HOME'), "none"),
        ("app --icon %i --name %c %k", ("app", "--icon", "--name"), "none"),
        ("app --percent=100%%", ("app", "--percent=100%"), "none"),
        ("  spaced   out  ", ("spaced", "out"), "none"),
    ],
)
def test_parse_exec(exec_value: str, argv: tuple[str, ...], accepts: str) -> None:
    assert parse_exec(exec_value) == (argv, accepts)


@pytest.mark.parametrize("exec_value", ['"unterminated', "%F", ""])
def test_parse_exec_rejects_garbage(exec_value: str) -> None:
    with pytest.raises(DesktopEntryError):
        parse_exec(exec_value)


# ---------------------------------------------------------------- catalog
def test_application_dirs_order(env: dict[str, str]) -> None:
    dirs = application_dirs(env)
    assert dirs[0] == Path(env["HOME"]) / ".local" / "share" / "applications"
    assert dirs[1] == Path(env["XDG_DATA_DIRS"]) / "applications"


def test_catalog_contents(catalog: AppCatalog) -> None:
    ids = {entry.desktop_id for entry in catalog.entries}
    assert "code-oss.desktop" in ids
    assert "sub-nested.desktop" in ids  # nested dirs become "-" in the ID
    assert "htop.desktop" not in ids  # hidden by the user's override
    assert "gnome-only.desktop" not in ids  # OnlyShowIn=GNOME, we're on Hyprland
    assert "broken.desktop" not in ids
    assert catalog.skipped == 1
    chromium = catalog.by_id("chromium")
    assert chromium is not None and chromium.name == "Chromium"  # user's file shadows the system one
    code = catalog.by_id("code-oss.desktop")
    assert code is not None
    assert code.argv == ("/usr/bin/code-oss",)
    assert code.executable == "code-oss"
    assert code.keywords == ("vscode",)


def test_try_exec_hides_uninstalled_apps(env: dict[str, str]) -> None:
    catalog = AppCatalog.load(env, which=lambda name: None if name == "kitty" else f"/usr/bin/{name}")
    assert catalog.by_id("kitty") is None


# ---------------------------------------------------------------- resolution
@pytest.fixture
def resolver(catalog: AppCatalog) -> AppResolver:
    return AppResolver(catalog, role_defaults={"browser": "zen-browser", "terminal": "kitty"})


@pytest.mark.parametrize(
    ("said", "desktop_id"),
    [
        ("Chromium", "chromium.desktop"),
        ("chromium browser", "chromium.desktop"),
        ("code", "code-oss.desktop"),
        ("VSCodium", "codium.desktop"),
        ("zen", "zen.desktop"),
        ("the browser", "zen.desktop"),  # rice default
        ("my terminal", "kitty.desktop"),
        ("WhatsApp", "whatsapp.desktop"),
        ("dolphin", "org.kde.dolphin.desktop"),
        ("kity", "kitty.desktop"),  # typo — but only if it's the single close match
    ],
)
def test_resolves(resolver: AppResolver, said: str, desktop_id: str) -> None:
    resolution = resolver.resolve(said)
    if said == "kity":
        assert resolution.status == "ambiguous"
        assert resolution.candidates[0].desktop_id == desktop_id
        return
    assert resolution.status == "found", resolution
    assert resolution.entry is not None
    assert resolution.entry.desktop_id == desktop_id


def test_vs_code_is_ambiguous_with_two_builds(resolver: AppResolver) -> None:
    resolution = resolver.resolve("VS Code")
    assert resolution.status == "ambiguous"
    assert {candidate.desktop_id for candidate in resolution.candidates} == {
        "code-oss.desktop",
        "codium.desktop",
    }
    assert "Code - OSS" in resolution.describe_candidates()


def test_user_alias_settles_ambiguity(catalog: AppCatalog) -> None:
    resolver = AppResolver(catalog, aliases={"VS Code": "codium.desktop"})
    resolution = resolver.resolve("vs code")
    assert resolution.status == "found"
    assert resolution.entry is not None and resolution.entry.desktop_id == "codium.desktop"


def test_unknown_app(resolver: AppResolver) -> None:
    resolution = resolver.resolve("photoshop")
    assert resolution.status == "not_found"


def test_browser_without_default_is_ambiguous(catalog: AppCatalog) -> None:
    resolution = AppResolver(catalog).resolve("browser")
    assert resolution.status == "ambiguous"
    assert {c.desktop_id for c in resolution.candidates} == {"chromium.desktop", "zen.desktop"}


def test_role_candidates_skip_hidden_helpers(resolver: AppResolver) -> None:
    resolution = resolver.resolve("file manager")
    assert resolution.status == "ambiguous"
    assert {c.desktop_id for c in resolution.candidates} == {"thunar.desktop", "org.kde.dolphin.desktop"}
    terminal = AppResolver(resolver.catalog).resolve("terminal")  # no rice default
    assert terminal.entry is not None and terminal.entry.desktop_id == "kitty.desktop"


def test_no_display_entries_need_an_exact_name(resolver: AppResolver) -> None:
    resolution = resolver.resolve("url handler")
    assert resolution.status != "found"


@pytest.mark.parametrize(
    ("said", "desktop_id"),
    [
        # Found on real hardware: "open kitty" asked "kitty or kitty URL Launcher?"
        ("kitty", "kitty.desktop"),
        ("kitty terminal", "kitty.desktop"),
        # A visible helper that runs the same program must not make the app's own name ambiguous.
        ("thunar", "thunar.desktop"),
        ("bulk rename", "thunar-bulk-rename.desktop"),
        # A hidden helper can still be named exactly.
        ("kitty URL Launcher", "kitty-open.desktop"),
    ],
)
def test_helper_entries_do_not_shadow_the_app(resolver: AppResolver, said: str, desktop_id: str) -> None:
    resolution = resolver.resolve(said)
    assert resolution.status == "found", resolution
    assert resolution.entry is not None and resolution.entry.desktop_id == desktop_id


# ---------------------------------------------------------------- window classes
def test_window_class_matching(catalog: AppCatalog) -> None:
    zen = catalog.by_id("zen")
    code = catalog.by_id("code-oss")
    codium = catalog.by_id("codium")
    whatsapp = catalog.by_id("whatsapp")
    assert zen and code and codium and whatsapp
    assert window_matches(zen, ["zen"])
    assert window_matches(code, ["code-oss"])
    assert window_matches(codium, ["VSCodium"])
    assert window_matches(whatsapp, ["WhatsApp Desktop"])
    assert not window_matches(zen, ["chromium"])
    assert not window_matches(codium, ["code-oss"])
    assert not window_matches(code, ["co"])  # too short to count as a prefix
    assert window_matches(code, ["weird-class"], learned=["Weird-Class"])
    assert expected_classes(zen)[0] == "zen"


def test_default_browser_found_by_name_when_binary_differs(tmp_path: Path) -> None:
    apps = tmp_path / "share" / "applications"
    _write(
        apps,
        {
            "zen.desktop": "[Desktop Entry]\nName=Zen Browser\nExec=/opt/zen/zen-bin %u\nType=Application\n"
            "Categories=Network;WebBrowser;\n",
            "chromium.desktop": USER_APPS["chromium.desktop"],
        },
    )
    catalog = AppCatalog.load(
        {
            "HOME": str(tmp_path),
            "XDG_DATA_HOME": str(tmp_path / "share"),
            "XDG_DATA_DIRS": str(tmp_path / "none"),
        },
        which=lambda name: name,
    )
    resolution = AppResolver(catalog, role_defaults={"browser": "zen-browser"}).resolve("browser")
    assert resolution.status == "found"
    assert resolution.entry is not None and resolution.entry.desktop_id == "zen.desktop"


@pytest.mark.parametrize(
    ("app", "request_text", "expected"),
    [
        ("code", "open VS Code on 3", "vs code"),  # the model shortened the user's words
        ("Code", "please open vscode", "Code"),  # "vscode" is one word: nothing longer to prefer
        ("code", "open code on 3", "code"),  # the user really said "code"
        ("chromium", "open chromium", "chromium"),
        ("browser", "open the browser", "browser"),
        ("code", "", "code"),
    ],
)
def test_grounded_phrase(resolver: AppResolver, app: str, request_text: str, expected: str) -> None:
    assert resolver.grounded_phrase(app, request_text) == expected


def test_shortened_name_still_asks(resolver: AppResolver) -> None:
    resolution = resolver.resolve_for_request("code", "open VS Code")
    assert resolution.status == "ambiguous"


def test_shortened_name_uses_the_users_alias(catalog: AppCatalog) -> None:
    resolver = AppResolver(catalog, aliases={"vs code": "codium.desktop"})
    resolution = resolver.resolve_for_request("code", "open VS Code")
    assert resolution.entry is not None and resolution.entry.desktop_id == "codium.desktop"
