from dataclasses import dataclass, field
from typing import Dict

FEATURE_ORDER: tuple[str, ...] = (
    "select",
    "open",
    "close",
    "shouts",
    "powers",
    "weapons",
    "spells",
    "potions",
    "key_console",
    "pause_resume",
)


def compute_effective(
    *,
    feature_enabled: Dict[str, bool],
    dialog_open: bool,
    open_listen: bool,
    commands_listen: bool,
    shout_context_allowed: bool,
    pause_resume_enabled: bool = False,
    commands_paused: bool = False,
) -> Dict[str, bool]:
    pause_resume = bool(pause_resume_enabled and (open_listen or commands_listen or dialog_open or commands_paused))
    if commands_paused:
        return {
            "select": False,
            "open": False,
            "close": False,
            "shouts": False,
            "powers": False,
            "weapons": False,
            "spells": False,
            "potions": False,
            "key_console": False,
            "pause_resume": pause_resume,
            "paused": True,
        }

    if dialog_open:
        return {
            "select": bool(feature_enabled.get("select", False)),
            "open": False,
            "close": bool(feature_enabled.get("close", False)),
            "shouts": False,
            "powers": False,
            "weapons": False,
            "spells": False,
            "potions": False,
            "key_console": False,
            "pause_resume": pause_resume,
            "paused": False,
        }

    return {
        "select": False,
        "open": bool(feature_enabled.get("open", False) and open_listen),
        "close": False,
        "shouts": bool(
            feature_enabled.get("shouts", False)
            and commands_listen
            and shout_context_allowed
        ),
        "powers": bool(feature_enabled.get("powers", False) and commands_listen),
        "weapons": bool(feature_enabled.get("weapons", False) and commands_listen),
        "spells": bool(feature_enabled.get("spells", False) and commands_listen),
        "potions": bool(feature_enabled.get("potions", False) and commands_listen),
        "key_console": bool(feature_enabled.get("key_console", False) and commands_listen),
        "pause_resume": pause_resume,
        "paused": False,
    }


def format_state(prefix: str, state: Dict[str, bool]) -> str:
    parts = [f"{key}={'ON' if state.get(key, False) else 'OFF'}" for key in FEATURE_ORDER]
    return f"{prefix}: " + " ".join(parts)


@dataclass
class VoiceState:
    feature_enabled: Dict[str, bool] = field(default_factory=dict)
    dialog_open: bool = False
    open_listen: bool = False
    commands_listen: bool = False
    shout_context_allowed: bool = False
    pause_resume_enabled: bool = False
    commands_paused: bool = False
    last_effective: Dict[str, bool] | None = None

    def set_feature_enabled(self, enabled: Dict[str, bool]) -> None:
        self.feature_enabled = dict(enabled)

    def set_dialog_open(self, dialog_open: bool) -> None:
        self.dialog_open = bool(dialog_open)

    def set_open_listen(self, open_listen: bool) -> None:
        self.open_listen = bool(open_listen)

    def set_commands_listen(self, commands_listen: bool) -> None:
        self.commands_listen = bool(commands_listen)

    def set_shout_context_allowed(self, shout_context_allowed: bool) -> None:
        self.shout_context_allowed = bool(shout_context_allowed)

    def set_pause_resume(self, enabled: bool, paused: bool) -> None:
        self.pause_resume_enabled = bool(enabled)
        self.commands_paused = bool(paused)

    def effective(self) -> Dict[str, bool]:
        return compute_effective(
            feature_enabled=self.feature_enabled,
            dialog_open=self.dialog_open,
            open_listen=self.open_listen,
            commands_listen=self.commands_listen,
            shout_context_allowed=self.shout_context_allowed,
            pause_resume_enabled=self.pause_resume_enabled,
            commands_paused=self.commands_paused,
        )

    def effective_changed(self) -> tuple[bool, Dict[str, bool]]:
        current = self.effective()
        changed = current != self.last_effective
        if changed:
            self.last_effective = current
        return changed, current

    def format_effective(self, effective: Dict[str, bool] | None = None) -> str:
        return format_state("Active", effective or self.effective())
