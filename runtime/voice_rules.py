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
)


def compute_effective(
    *,
    feature_enabled: Dict[str, bool],
    dialog_open: bool,
    open_listen: bool,
    commands_listen: bool,
    shout_context_allowed: bool,
) -> Dict[str, bool]:
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

    def effective(self) -> Dict[str, bool]:
        return compute_effective(
            feature_enabled=self.feature_enabled,
            dialog_open=self.dialog_open,
            open_listen=self.open_listen,
            commands_listen=self.commands_listen,
            shout_context_allowed=self.shout_context_allowed,
        )

    def effective_changed(self) -> tuple[bool, Dict[str, bool]]:
        current = self.effective()
        changed = current != self.last_effective
        if changed:
            self.last_effective = current
        return changed, current

    def format_effective(self, effective: Dict[str, bool] | None = None) -> str:
        return format_state("effective", effective or self.effective())

    def format_listen_status(self, effective: Dict[str, bool] | None = None) -> str:
        return format_state("[LISTEN][STATE] Listen status", effective or self.effective())
