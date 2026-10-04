"""The individual ``sonu doctor`` checks.

Rules for every check:

* read-only — a check may inspect the system but never change it;
* bounded — every external call has a timeout;
* private — window titles and other personal content never appear in the output
  (window *classes* may, since they identify apps, not content);
* robust — a crashing check becomes a ``fail`` result instead of crashing the doctor.
"""

from __future__ import annotations

import asyncio
import re
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path

from sonu.audit.log import AuditError, AuditLog
from sonu.config.settings import ModelRole
from sonu.desktop.hyprland.errors import HyprlandError, HyprlandNotFound
from sonu.desktop.hyprland.instance import discover_instance
from sonu.desktop.hyprland.ipc import HyprlandClient
from sonu.doctor.model import CheckResult, DoctorContext, Status
from sonu.integrations.hypr_theme import detect_hypr_theme
from sonu.llm.ollama import InstalledModel, ModelInfo, OllamaClient, OllamaError, same_model
from sonu.system.commands import PACKAGES

MIN_PYTHON = (3, 12)
LUA_ERA_HYPRLAND = (0, 55)
TESTED_HYPRLAND = (0, 56)
GIB = 1024**3
MODEL_INFO_BUDGET_S = 15.0

Check = Callable[[DoctorContext], Awaitable[list[CheckResult]]]


# ---------------------------------------------------------------------------- system
async def check_system(ctx: DoctorContext) -> list[CheckResult]:
    group = "System"
    results: list[CheckResult] = []

    if ctx.platform.startswith("linux"):
        results.append(CheckResult("system.platform", group, "Operating system", "ok", "Linux"))
    else:
        results.append(
            CheckResult(
                "system.platform",
                group,
                "Operating system",
                "fail",
                f"SONU needs Linux (found {ctx.platform})",
            )
        )

    version = ".".join(str(part) for part in ctx.python_version)
    if ctx.python_version[:2] >= MIN_PYTHON:
        results.append(CheckResult("system.python", group, "Python", "ok", f"Python {version}"))
    else:
        results.append(
            CheckResult(
                "system.python",
                group,
                "Python",
                "fail",
                f"Python {version} is too old",
                hint="SONU needs Python 3.12+.",
            )
        )

    desktop = ctx.env.get("XDG_CURRENT_DESKTOP", "") or "unknown desktop"
    if ctx.env.get("WAYLAND_DISPLAY"):
        results.append(CheckResult("session.wayland", group, "Session", "ok", f"Wayland session ({desktop})"))
    else:
        results.append(
            CheckResult(
                "session.wayland",
                group,
                "Session",
                "warn",
                "no Wayland session in this environment",
                hint="Run SONU from a terminal inside your Hyprland session.",
            )
        )
    return results


# ---------------------------------------------------------------------------- sonu itself
def _private_dir_result(path: Path) -> tuple[str, str] | None:
    """Return (status, message) for a directory that exists, None if it doesn't exist yet."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None
    if mode & 0o077:
        return "warn", f"{path} is accessible by other users (mode {mode:o})"
    return "ok", f"{path} (mode {mode:o})"


async def check_sonu(ctx: DoctorContext) -> list[CheckResult]:
    group = "SONU"
    results: list[CheckResult] = []
    config_path = ctx.paths.config_file

    if ctx.settings_error:
        results.append(
            CheckResult(
                "sonu.config",
                group,
                "Config",
                "fail",
                f"{config_path} is invalid — using built-in defaults",
                hint="Fix the file, or move it aside and run `sonu config init`.",
                details={"error": ctx.settings_error},
            )
        )
    elif ctx.settings_from_file:
        results.append(CheckResult("sonu.config", group, "Config", "ok", f"loaded {config_path}"))
    else:
        results.append(
            CheckResult(
                "sonu.config",
                group,
                "Config",
                "info",
                "no config file — using built-in defaults",
                hint="`sonu config init` writes a commented config you can edit.",
            )
        )

    problems: list[str] = []
    seen: list[str] = []
    for directory in (ctx.paths.config_dir, ctx.paths.data_dir, ctx.paths.state_dir):
        outcome = _private_dir_result(directory)
        if outcome is None:
            continue
        status, message = outcome
        (problems if status == "warn" else seen).append(message)
    if problems:
        results.append(
            CheckResult(
                "sonu.dirs",
                group,
                "Data directories",
                "warn",
                "; ".join(problems),
                hint="Run `chmod 700` on the listed directories.",
            )
        )
    else:
        results.append(
            CheckResult(
                "sonu.dirs",
                group,
                "Data directories",
                "ok",
                "private (0700)" if seen else "not created yet (created on first use, private)",
            )
        )

    if not ctx.settings.audit.enabled:
        results.append(
            CheckResult(
                "sonu.audit",
                group,
                "Audit log",
                "warn",
                "disabled in config",
                hint="Re-enable [audit] so every action SONU takes is recorded.",
            )
        )
    else:
        try:
            verification = AuditLog(ctx.paths.audit_file).verify()
        except (OSError, AuditError) as exc:
            results.append(CheckResult("sonu.audit", group, "Audit log", "fail", f"cannot read: {exc}"))
        else:
            if verification.ok:
                results.append(
                    CheckResult(
                        "sonu.audit",
                        group,
                        "Audit log",
                        "ok",
                        f"{verification.records} record(s), hash chain intact",
                        details={"path": str(ctx.paths.audit_file)},
                    )
                )
            else:
                results.append(
                    CheckResult(
                        "sonu.audit",
                        group,
                        "Audit log",
                        "fail",
                        f"line {verification.line}: {verification.error}",
                        hint=(
                            "SONU refuses to append to a damaged log. "
                            "Inspect the file (see `sonu audit path`)."
                        ),
                        details={"path": str(ctx.paths.audit_file)},
                    )
                )

    policy = ctx.settings.policy.autonomy
    cloud = ctx.settings.privacy.cloud
    results.append(
        CheckResult(
            "sonu.policy", group, "Safety policy", "info", f"autonomy: {policy} · cloud models: {cloud}"
        )
    )
    return results


# ---------------------------------------------------------------------------- GPU
async def check_gpu(ctx: DoctorContext) -> list[CheckResult]:
    group = "GPU"
    if not ctx.which("nvidia-smi"):
        return [
            CheckResult(
                "gpu.nvidia",
                group,
                "GPU",
                "info",
                "nvidia-smi not found — GPU not inspected (only NVIDIA is inspected for now)",
            )
        ]
    query = [
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,driver_version",
        "--format=csv,noheader,nounits",
    ]
    result = await asyncio.to_thread(ctx.run, query, 5.0)
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return [
            CheckResult(
                "gpu.nvidia",
                group,
                "GPU",
                "warn",
                "nvidia-smi failed",
                hint="Check the NVIDIA driver (`nvidia-smi` in a terminal).",
                details={"stderr": (result.stderr.strip()[:300] if result else "timeout or not executable")},
            )
        ]
    first = result.stdout.strip().splitlines()[0]
    parts = [part.strip() for part in first.split(",")]
    if len(parts) < 4:
        return [
            CheckResult("gpu.nvidia", group, "GPU", "warn", f"unexpected nvidia-smi output: {first[:120]}")
        ]
    name, total, used, driver = parts[0], parts[1], parts[2], parts[3]
    try:
        total_mib, used_mib = int(float(total)), int(float(used))
    except ValueError:
        return [
            CheckResult("gpu.nvidia", group, "GPU", "warn", f"unexpected nvidia-smi output: {first[:120]}")
        ]
    ctx.facts["gpu_vram_mib"] = total_mib
    return [
        CheckResult(
            "gpu.nvidia",
            group,
            "GPU",
            "ok",
            f"{name} · {total_mib / 1024:.0f} GiB VRAM ({used_mib} MiB in use) · driver {driver}",
            details={"name": name, "vram_total_mib": total_mib, "vram_used_mib": used_mib, "driver": driver},
        )
    ]


# ---------------------------------------------------------------------------- Hyprland
async def check_hyprland(ctx: DoctorContext) -> list[CheckResult]:
    group = "Hyprland"
    try:
        instance = discover_instance(ctx.env)
    except HyprlandNotFound as exc:
        return [
            CheckResult(
                "hyprland.instance",
                group,
                "Instance",
                "fail",
                str(exc),
                hint="Run SONU from a terminal inside your Hyprland session.",
            )
        ]

    results: list[CheckResult] = []
    if instance.discovered_by == "environment":
        results.append(
            CheckResult("hyprland.instance", group, "Instance", "ok", f"sockets in {instance.directory}")
        )
    else:
        results.append(
            CheckResult(
                "hyprland.instance",
                group,
                "Instance",
                "warn",
                f"HYPRLAND_INSTANCE_SIGNATURE not set; using newest instance {instance.signature}",
                hint=(
                    "If SONU runs as a service, import the session environment, e.g. "
                    "`systemctl --user import-environment HYPRLAND_INSTANCE_SIGNATURE WAYLAND_DISPLAY`."
                ),
            )
        )

    client = HyprlandClient(instance, timeout_s=ctx.settings.hyprland.request_timeout_s)
    try:
        version = await client.version()
    except HyprlandError as exc:
        results.append(CheckResult("hyprland.version", group, "Version", "fail", str(exc)))
        return results

    parsed = version.parsed
    ctx.facts["hyprland_version"] = parsed
    details = {"version": version.display, "tag": version.tag, "commit": version.commit[:12]}
    if parsed is None:
        results.append(
            CheckResult(
                "hyprland.version", group, "Version", "warn", "could not parse version", details=details
            )
        )
    elif parsed[:2] >= LUA_ERA_HYPRLAND:
        note = "" if parsed[:2] >= TESTED_HYPRLAND else " (SONU is developed against 0.56)"
        results.append(
            CheckResult(
                "hyprland.version",
                group,
                "Version",
                "ok",
                f"Hyprland {version.display}{note}",
                details=details,
            )
        )
    else:
        results.append(
            CheckResult(
                "hyprland.version",
                group,
                "Version",
                "warn",
                f"Hyprland {version.display} predates the Lua config (0.55)",
                hint="SONU targets 0.55+; the classic dispatcher syntax is supported but less tested.",
                details=details,
            )
        )

    try:
        provider = await client.config_provider()
    except HyprlandError as exc:
        results.append(CheckResult("hyprland.config", group, "Config system", "fail", str(exc)))
        return results
    ctx.facts["hyprland_config_provider"] = provider
    dialect = "Lua dispatchers (hl.dsp.*)" if provider == "lua" else "classic dispatchers"
    results.append(
        CheckResult(
            "hyprland.config",
            group,
            "Config system",
            "ok",
            f"{provider} config → SONU will use {dialect}",
            details={"config_provider": provider},
        )
    )

    try:
        monitors, workspaces, windows = await asyncio.gather(
            client.monitors(), client.workspaces(), client.clients()
        )
    except HyprlandError as exc:
        results.append(CheckResult("hyprland.state", group, "Desktop state", "fail", str(exc)))
        return results
    classes = sorted({window.window_class for window in windows if window.window_class})
    results.append(
        CheckResult(
            "hyprland.state",
            group,
            "Desktop state",
            "ok",
            f"{len(monitors)} monitor(s) · {len(workspaces)} workspace(s) · {len(windows)} window(s)",
            details={
                "monitors": [
                    {
                        "name": monitor.name,
                        "mode": f"{monitor.width}x{monitor.height}@{monitor.refresh_rate:.0f}",
                        "scale": monitor.scale,
                    }
                    for monitor in monitors
                ],
                "window_classes": classes,
            },
        )
    )
    return results


# ---------------------------------------------------------------------------- Ollama
_ROLE_NEEDS = {"planner": "tools", "planner_deep": "tools", "embed": "embedding"}
# Models fine-tuned to never refuse. SONU's safety never *relies* on a model refusing, but these
# remove one layer of defence, so SONU never suggests them for planning actions.
_NO_REFUSAL_TUNED = re.compile(r"uncensored|abliterat", re.IGNORECASE)


def _unsuitable_reason(role_name: str, capabilities: tuple[str, ...]) -> str | None:
    """Why a model can't serve a role, or None. Unknown capabilities are not judged."""
    if not capabilities:
        return None
    needed = _ROLE_NEEDS[role_name]
    if needed not in capabilities:
        return f"does not support {needed}"
    # Some embedding models inherit a chat template and report "tools" too (seen with qwen3-embedding).
    if needed == "tools" and "embedding" in capabilities:
        return "is an embedding model, not a chat model"
    return None


def _fits(size_bytes: int, vram_mib: int | None) -> bool | None:
    if not vram_mib or not size_bytes:
        return None
    # The weights plus a working context need headroom; 90% of VRAM is a deliberately rough bar.
    return size_bytes <= vram_mib * 1024 * 1024 * 0.9


def _alternatives(
    role_name: str, installed: list[InstalledModel], infos: dict[str, ModelInfo], vram_mib: int | None
) -> list[str]:
    def suitable(model: InstalledModel) -> bool:
        capabilities = infos.get(model.name, ModelInfo()).capabilities
        if not capabilities or _unsuitable_reason(role_name, capabilities) is not None:
            return False
        if role_name != "embed" and _NO_REFUSAL_TUNED.search(model.name):
            return False
        return _fits(model.size, vram_mib) is not False

    candidates = [model for model in installed if suitable(model)]
    return [model.name for model in sorted(candidates, key=lambda model: model.size, reverse=True)]


def _role_result(
    role_name: str,
    role: ModelRole,
    installed: list[InstalledModel],
    infos: dict[str, ModelInfo],
    vram_mib: int | None,
) -> CheckResult:
    group = "Ollama"
    title = f"Model: {role_name}"
    check_id = f"ollama.model.{role_name}"
    needed = _ROLE_NEEDS[role_name]
    match = next((model for model in installed if same_model(model.name, role.model)), None)
    alternatives = _alternatives(role_name, installed, infos, vram_mib)

    if match is None:
        hint = f"Install it with `ollama pull {role.model}`"
        if alternatives:
            hint += f", or use an installed one that supports {needed}: {', '.join(alternatives[:5])}"
        return CheckResult(
            check_id,
            group,
            title,
            "warn",
            f"{role.model} is not installed",
            hint=hint + ".",
            details={"configured": role.model, "alternatives": alternatives},
        )

    capabilities = infos.get(match.name, ModelInfo()).capabilities
    size_gib = match.size / GIB
    reason = _unsuitable_reason(role_name, capabilities)
    if reason is not None:
        return CheckResult(
            check_id,
            group,
            title,
            "fail",
            f"{match.name} {reason}",
            hint=f"Pick a model with '{needed}' capability"
            + (f": {', '.join(alternatives[:5])}" if alternatives else "."),
            details={"capabilities": list(capabilities)},
        )
    if role_name != "embed" and _NO_REFUSAL_TUNED.search(match.name):
        return CheckResult(
            check_id,
            group,
            title,
            "warn",
            f"{match.name} is tuned to never refuse — not recommended for planning actions",
            hint="SONU's safety doesn't rely on refusals, but this removes a layer of defence."
            + (f" Consider: {', '.join(alternatives[:5])}." if alternatives else ""),
            details={"capabilities": list(capabilities)},
        )

    fits = _fits(match.size, vram_mib)
    if fits is False:
        return CheckResult(
            check_id,
            group,
            title,
            "warn",
            (
                f"{match.name} ({size_gib:.1f} GiB) is larger than your GPU memory — "
                "it will be split with the CPU (slower)"
            ),
            details={"size_bytes": match.size, "capabilities": list(capabilities)},
        )
    where = " · fits in GPU memory" if fits else ""
    return CheckResult(
        check_id,
        group,
        title,
        "ok",
        f"{match.name} ({size_gib:.1f} GiB){where}",
        details={"size_bytes": match.size, "capabilities": list(capabilities)},
    )


async def check_ollama(ctx: DoctorContext) -> list[CheckResult]:
    group = "Ollama"
    host = ctx.settings.ollama.host
    timeout_s = min(ctx.settings.ollama.timeout_s, 5.0)
    async with OllamaClient(host, timeout_s=timeout_s, transport=ctx.ollama_transport) as client:
        try:
            version = await client.version()
        except OllamaError as exc:
            return [
                CheckResult(
                    "ollama.server",
                    group,
                    "Server",
                    "fail",
                    str(exc),
                    hint="Is Ollama running? Try `systemctl status ollama` or start it with `ollama serve`.",
                )
            ]
        results = [CheckResult("ollama.server", group, "Server", "ok", f"Ollama {version} at {host}")]

        try:
            installed = await client.installed_models()
        except OllamaError as exc:
            results.append(CheckResult("ollama.models", group, "Installed models", "fail", str(exc)))
            return results

        infos: dict[str, ModelInfo] = {}
        try:
            # Bounded overall, so a hung server can't stall the doctor once per installed model.
            async with asyncio.timeout(MODEL_INFO_BUDGET_S):
                for model in installed:
                    try:
                        infos[model.name] = await client.model_info(model.name)
                    except OllamaError:
                        continue
        except TimeoutError:
            pass  # report with the capabilities gathered so far
        tool_capable = sorted(
            name
            for name, info in infos.items()
            if info.capabilities and _unsuitable_reason("planner", info.capabilities) is None
        )
        results.append(
            CheckResult(
                "ollama.models",
                group,
                "Installed models",
                "ok" if installed else "warn",
                f"{len(installed)} installed, {len(tool_capable)} support tool calling",
                details={"installed": [model.name for model in installed], "tool_capable": tool_capable},
            )
        )

        vram_mib = ctx.facts.get("gpu_vram_mib")
        roles = ctx.settings.models
        for role_name, role in (
            ("planner", roles.planner),
            ("planner_deep", roles.planner_deep),
            ("embed", roles.embed),
        ):
            results.append(_role_result(role_name, role, installed, infos, vram_mib))

        try:
            loaded = await client.loaded_models()
        except OllamaError as exc:
            results.append(CheckResult("ollama.loaded", group, "Loaded now", "warn", str(exc)))
        else:
            if loaded:
                summary = ", ".join(
                    f"{model.name} ({model.size_vram / GIB:.1f}/{model.size / GIB:.1f} GiB on GPU)"
                    for model in loaded
                )
            else:
                summary = "no models loaded right now"
            results.append(CheckResult("ollama.loaded", group, "Loaded now", "info", summary))
    return results


# ---------------------------------------------------------------------------- desktop tools
# (binary, what SONU uses it for, Arch package, needed from milestone)
DESKTOP_TOOLS: tuple[tuple[str, str, str, str], ...] = (
    ("notify-send", "desktop notifications", "libnotify", "M1"),
    ("systemd-run", "tracking launched apps in their own scopes", "systemd", "M1"),
    ("gio", "launching apps from their .desktop files", "glib2", "M1"),
    ("uwsm", "launching apps through your session manager (optional)", "uwsm", "M1"),
    ("qs", "Quickshell menu/bar integration (optional)", "quickshell", "M2"),
    ("grim", "screenshots for screen understanding", "grim", "M7"),
    ("wtype", "typing text into apps", "wtype", "M7"),
)
_ESSENTIAL_TOOLS = ("notify-send", "systemd-run", "gio")


async def check_tools(ctx: DoctorContext) -> list[CheckResult]:
    found = [tool for tool, *_ in DESKTOP_TOOLS if ctx.which(tool)]
    missing = [
        (tool, purpose, package, milestone)
        for tool, purpose, package, milestone in DESKTOP_TOOLS
        if tool not in found
    ]
    essential_missing = [tool for tool, *_ in missing if tool in _ESSENTIAL_TOOLS]
    status: Status = "warn" if essential_missing else "ok"
    summary = f"found: {', '.join(found) or 'none'}"
    if missing:
        summary += f" · missing: {', '.join(tool for tool, *_ in missing)}"
    hint = None
    if missing:
        needed_now = [item for item in missing if item[3] in ("M1", "M2")]
        later = [item for item in missing if item[3] not in ("M1", "M2")]
        parts = []
        if needed_now:
            parts.append(
                "soon: "
                + "; ".join(f"{tool} — {purpose} (pacman: {pkg})" for tool, purpose, pkg, _ in needed_now)
            )
        if later:
            parts.append(
                "later: "
                + "; ".join(f"{tool} — {purpose} (pacman: {pkg})" for tool, purpose, pkg, _ in later)
            )
        hint = " | ".join(parts)
    return [
        CheckResult(
            "tools.desktop",
            "Desktop tools",
            "Helper programs",
            status,
            summary,
            hint=hint,
            details={"found": found, "missing": [tool for tool, *_ in missing]},
        )
    ]


# ---------------------------------------------------------------------------- system settings
SETTINGS_PROGRAMS: tuple[tuple[str, str], ...] = (
    ("wpctl", "volume and mic"),
    ("brightnessctl", "screen brightness"),
    ("bluetoothctl", "Bluetooth"),
    ("rfkill", "unblocking Bluetooth"),
    ("nmcli", "Wi-Fi"),
    ("playerctl", "media play/pause/next"),
    ("powerprofilesctl", "power mode"),
    ("yt-dlp", "playing songs and videos from YouTube"),
    ("pw-dump", "hearing that a video plays when the browser isn't in the media controls"),
    ("slurp", "screenshots of an area"),
    ("wl-copy", "copying screenshots"),
    ("systemd-run", "reminders and timers"),
    ("qs", "night light, keep awake, do-not-disturb, lock (the rice's switches)"),
)


async def check_settings_programs(ctx: DoctorContext) -> list[CheckResult]:
    found = [program for program, _ in SETTINGS_PROGRAMS if ctx.which(program)]
    missing = [(program, purpose) for program, purpose in SETTINGS_PROGRAMS if program not in found]
    summary = f"found: {', '.join(found) or 'none'}"
    hint = None
    if missing:
        summary += f" · missing: {', '.join(program for program, _ in missing)}"
        hint = "; ".join(
            f"{program} — {purpose} (pacman: {PACKAGES[program]})" for program, purpose in missing
        )
    return [
        CheckResult(
            "tools.settings",
            "Desktop tools",
            "Control programs",
            "info" if missing else "ok",
            summary,
            hint=hint,
            details={"found": found, "missing": [program for program, _ in missing]},
        )
    ]


# ---------------------------------------------------------------------------- integrations
async def check_integrations(ctx: DoctorContext) -> list[CheckResult]:
    info = detect_hypr_theme(ctx.home)
    if not info.detected:
        return [
            CheckResult(
                "integration.hypr_theme",
                "Integrations",
                "Rice",
                "skip",
                "no Orrery (hypr-dotfiles) / Quickshell setup detected",
            )
        ]
    parts = []
    if info.theme_dir_present:
        parts.append("Orrery rice")
    for kind in ("browser", "terminal", "files", "agent"):
        app = info.defaults.get(kind)
        if app:
            note = {"rice-fallback": " (rice default)", "binds": " (binds.lua)"}.get(app.source, "")
            parts.append(f"{'file manager' if kind == 'files' else kind}: {app.command}{note}")
    if info.quickshell_config:
        parts.append("Quickshell shell")
    hint = (
        "SONU will use these apps for 'open my browser/terminal' and for folders."
        if info.defaults
        else "No default apps found; SONU will ask which browser/terminal you mean."
    )
    if info.quickshell_config:
        hint += (
            " Key and SUPER+SPACE menu setup: docs/integrations/hypr-dotfiles.md (a bar widget comes in M3)."
        )
    return [
        CheckResult(
            "integration.hypr_theme",
            "Integrations",
            "Rice",
            "info",
            " · ".join(parts),
            hint=hint,
            details={
                "defaults": {
                    kind: {"command": app.command, "source": app.source}
                    for kind, app in info.defaults.items()
                },
                "quickshell": info.quickshell_config,
            },
        )
    ]


CHECKS: tuple[Check, ...] = (
    check_system,
    check_sonu,
    check_gpu,  # before Ollama: model-fit checks use the VRAM it records
    check_hyprland,
    check_ollama,
    check_tools,
    check_settings_programs,
    check_integrations,
)


async def run_checks(ctx: DoctorContext, checks: tuple[Check, ...] = CHECKS) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in checks:
        try:
            results.extend(await check(ctx))
        except Exception as exc:  # the doctor must report problems, never crash on them
            results.append(
                CheckResult(
                    f"doctor.{check.__name__}",
                    "Doctor",
                    check.__name__,
                    "fail",
                    f"check crashed: {exc.__class__.__name__}: {exc}",
                    hint="This is a bug in SONU — please report it.",
                )
            )
    return results
