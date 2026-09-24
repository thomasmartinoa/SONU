class HyprlandError(Exception):
    """Base class for Hyprland adapter errors."""


class HyprlandNotFound(HyprlandError):
    """No running Hyprland instance could be located."""


class HyprlandRequestError(HyprlandError):
    """A request could not be sent, timed out, or was refused."""


class HyprlandProtocolError(HyprlandError):
    """Hyprland answered with something SONU could not understand."""


class HyprlandDispatchError(HyprlandError):
    """Hyprland refused or failed a dispatch; the message is Hyprland's own reply."""
