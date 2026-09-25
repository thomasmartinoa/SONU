"""System settings through the same small programs a Hyprland rice uses (wpctl, brightnessctl, …)."""

from sonu.system.commands import (
    PACKAGES,
    CommandError,
    CommandResult,
    CommandRunner,
    ProgramMissing,
    SubprocessRunner,
)
from sonu.system.control import (
    Battery,
    SystemControl,
    Volume,
    parse_backlight,
    parse_bluetooth_powered,
    parse_volume,
    pick_backlight,
)

__all__ = [
    "PACKAGES",
    "Battery",
    "CommandError",
    "CommandResult",
    "CommandRunner",
    "ProgramMissing",
    "SubprocessRunner",
    "SystemControl",
    "Volume",
    "parse_backlight",
    "parse_bluetooth_powered",
    "parse_volume",
    "pick_backlight",
]
