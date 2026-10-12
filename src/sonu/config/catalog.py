"""The settings the user can change without editing the config file (D90): the Control Center and
``sonu settings`` show and change exactly these.

Each has a plain label, a group, its kind and — when a value gives SONU more reach or keeps more of the user's
data ("loosens") — the values that ask "are you sure?" first: cloud models, fewer confirmations, the password
said aloud, notifications kept, the audit log off. Changing is always the user's own action (a click or a
typed command): no tool of SONU's changes its settings, and the Control Center window is off-limits to its
screen tools.

Not here (stay in the file, for people who know what they do): the Ollama host and timeouts, MCP servers, app
aliases (``remember …`` does those), the deep planner's thinking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["bool", "int", "float", "choice", "text", "model", "list"]


@dataclass(frozen=True, slots=True)
class Entry:
    key: str  # "heads_up.enabled"
    group: str  # "Brain", "Voice", "Proactive", "Privacy", "Look"
    label: str
    help: str
    kind: Kind
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    # Values that loosen privacy or safety: asked "are you sure?" first. For a list: items that do.
    loosens: tuple[object, ...] = ()
    restart: bool = True  # sonud restarts (when idle) to use it
    extra: dict[str, str] = field(default_factory=dict)


CATALOG: tuple[Entry, ...] = (
    # ---------------------------------------------------------------- Brain
    Entry(
        "models.planner.model",
        "Brain",
        "Planner model",
        "The local model that understands your requests and plans what to do.",
        "model",
    ),
    Entry(
        "models.planner_deep.model",
        "Brain",
        "Deep planner model",
        "A bigger local model for `sonu --deep` and hard requests (slower).",
        "model",
    ),
    Entry(
        "privacy.cloud",
        "Brain",
        "Cloud models",
        "never: everything stays on this laptop. allow: what you pick below may go to Ollama Cloud "
        "(your words, never window titles for planning). ask: SONU asks each time.",
        "choice",
        choices=("never", "ask", "allow"),
        loosens=("ask", "allow"),
    ),
    Entry("cloud.model", "Brain", "Cloud model", "The Ollama Cloud model for planning and prompts.", "text"),
    Entry(
        "cloud.use_for",
        "Brain",
        "Use the cloud for",
        "prompts: your idea for Claude Code → a better prompt. planning: harder requests. vision: "
        "screenshots "
        "(anything visible on screen goes).",
        "list",
        choices=("prompts", "planning", "vision"),
        loosens=("vision",),
    ),
    Entry(
        "cloud.plan",
        "Brain",
        "Plan in the cloud",
        "hard: requests with several parts and ones the local model couldn't plan. always: every request.",
        "choice",
        choices=("hard", "always"),
        loosens=("always",),
    ),
    Entry(
        "policy.autonomy",
        "Brain",
        "Asking before acting",
        "confirm-risky: asks before anything risky (recommended). confirm-each: before every step. suggest: "
        "only says what it would do. trusted: asks only for the highest risk.",
        "choice",
        choices=("confirm-risky", "confirm-each", "suggest", "trusted"),
        loosens=("trusted",),
    ),
    Entry(
        "assistant.replies",
        "Brain",
        "Natural replies",
        "A short natural sentence after each request.",
        "bool",
    ),
    Entry("assistant.name", "Brain", "SONU's name", "The name it answers to and uses for itself.", "text"),
    Entry("assistant.user_name", "Brain", "Your name", "Used now and then in small talk.", "text"),
    Entry(
        "web.search_engine",
        "Brain",
        "Search engine",
        "Where “search the web for …” and research go.",
        "choice",
        choices=("duckduckgo", "google", "brave", "startpage"),
    ),
    # ---------------------------------------------------------------- Voice
    Entry(
        "voice.speak_replies",
        "Voice",
        "Speak replies",
        "Say every reply out loud when you talk to SONU.",
        "bool",
    ),
    Entry("voice.greet", "Voice", "Greet", "Say “How can I help?” when the SONU key is pressed.", "bool"),
    Entry(
        "voice.follow_up_s",
        "Voice",
        "Keep listening (s)",
        "After a reply, listen this long for the next request (0: off).",
        "float",
        minimum=0,
        maximum=30,
    ),
    Entry("voice.barge_in", "Voice", "Talk over SONU", "Talk while SONU speaks to stop it.", "bool"),
    Entry(
        "voice.voice",
        "Voice",
        "Voice",
        "SONU's voice.",
        "choice",
        choices=(
            "af_heart",
            "af_bella",
            "af_nicole",
            "af_sarah",
            "am_michael",
            "am_adam",
            "bf_emma",
            "bm_george",
        ),
    ),
    Entry("voice.speed", "Voice", "Speed", "How fast SONU talks.", "float", minimum=0.5, maximum=2.0),
    Entry(
        "voice.stt_model",
        "Voice",
        "Hearing model",
        "large-v3-turbo: best with accents and noise. small.en: lighter.",
        "choice",
        choices=("large-v3-turbo", "large-v3", "small.en"),
    ),
    Entry(
        "voice.password_by_voice",
        "Voice",
        "Password by voice",
        "Installing by voice: say the password as well as type it — anyone nearby hears it.",
        "bool",
        loosens=(True,),
    ),
    # ---------------------------------------------------------------- Proactive
    Entry(
        "heads_up.enabled",
        "Proactive",
        "Heads-ups",
        "Battery low, disk nearly full, CPU staying hot.",
        "bool",
    ),
    Entry(
        "heads_up.speak",
        "Proactive",
        "Say heads-ups",
        "Out loud too — never during a call, over a fullscreen window or in the quiet hours.",
        "bool",
    ),
    Entry("heads_up.battery_low", "Proactive", "Battery low at (%)", "", "int", minimum=1, maximum=99),
    Entry(
        "heads_up.battery_critical", "Proactive", "Battery critical at (%)", "", "int", minimum=1, maximum=99
    ),
    Entry("heads_up.disk_full", "Proactive", "Disk full at (% used)", "", "int", minimum=50, maximum=100),
    Entry("heads_up.hot", "Proactive", "CPU hot at (°C)", "", "int", minimum=50, maximum=110),
    Entry(
        "heads_up.quiet", "Proactive", "Quiet hours", "Nothing said out loud then, e.g. 23:00-07:00.", "text"
    ),
    Entry("briefing.daily", "Proactive", "Daily briefing", "Shown once a day by itself.", "bool"),
    Entry("briefing.at", "Proactive", "Briefing at", "From this time, e.g. 08:00.", "text"),
    Entry(
        "notifications.remember",
        "Proactive",
        "Keep notifications",
        "For “what was that notification?” — in memory only, never on disk; they can carry message previews.",
        "bool",
        loosens=(True,),
    ),
    Entry(
        "notifications.keep_minutes", "Proactive", "Keep them (minutes)", "", "int", minimum=1, maximum=1440
    ),
    # ---------------------------------------------------------------- Privacy
    Entry(
        "journal.enabled",
        "Privacy",
        "Activity journal",
        "Which window had the focus, for “what was I doing yesterday?” — on this laptop only.",
        "bool",
    ),
    Entry("journal.keep_days", "Privacy", "Keep the journal (days)", "", "int", minimum=1, maximum=365),
    Entry(
        "conversations.keep",
        "Privacy",
        "Remember conversations",
        "What you and SONU said, for “what did we talk about yesterday?” — on this laptop only.",
        "bool",
    ),
    Entry(
        "conversations.keep_days", "Privacy", "Keep conversations (days)", "", "int", minimum=1, maximum=365
    ),
    Entry(
        "audit.enabled",
        "Privacy",
        "Audit log",
        "What SONU did and why — off means no record.",
        "bool",
        loosens=(False,),
    ),
    # ---------------------------------------------------------------- Look
    Entry("hud.enabled", "Look", "HUD", "SONU's own window at the top: listening, steps, results.", "bool"),
    Entry("hud.hide_after_s", "Look", "Hide the HUD after (s)", "", "float", minimum=1, maximum=120),
)

BY_KEY = {entry.key: entry for entry in CATALOG}
