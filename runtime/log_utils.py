import builtins
import os
import sys
from datetime import datetime
from typing import Optional, TextIO

from rich.console import Console
from rich.markup import escape


def _ts_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


_console = Console(file=sys.__stdout__, force_terminal=True)
_log_file: Optional[TextIO] = None
_LEVELS = {"debug": 10, "info": 20, "warning": 30, "warn": 30, "error": 40}
_log_level_name = "debug"
_log_level_value = _LEVELS[_log_level_name]


def _normalize_log_level(level: str | None) -> str | None:
    normalized = str(level or "").strip().lower()
    if normalized == "warn":
        return "warning"
    if normalized in ("debug", "info", "warning"):
        return normalized
    return None


def set_log_level(level: str | None) -> bool:
    global _log_level_name, _log_level_value
    normalized = _normalize_log_level(level)
    if normalized is None:
        _log_level_name = "debug"
        _log_level_value = _LEVELS[_log_level_name]
        os.environ["DVC_LOG_LEVEL"] = _log_level_name
        return False
    _log_level_name = normalized
    _log_level_value = _LEVELS[normalized]
    os.environ["DVC_LOG_LEVEL"] = normalized
    return True


def get_log_level() -> str:
    return _log_level_name


def _should_log(level: str) -> bool:
    return _LEVELS[level] >= _log_level_value


def set_log_file(file: Optional[TextIO]) -> None:
    global _log_file
    _log_file = file


def setup_timestamped_print() -> None:
    if getattr(builtins, "_dvc_ts_print", None) is not None:
        return

    def _ts_print(*args, **kwargs) -> None:
        if not _should_log("info"):
            return

        sep = kwargs.pop("sep", " ")
        end = kwargs.pop("end", "\n")
        file = kwargs.pop("file", None)
        flush = kwargs.pop("flush", False)

        if file is None:
            file = sys.stdout

        msg = sep.join(str(a) for a in args) + end
        file.write(_format_lines("info", msg))
        if flush:
            file.flush()

    builtins._dvc_ts_print = _ts_print
    builtins.print = _ts_print


def _format_lines(level: str, text: str) -> str:
    ts = _ts_now()
    prefix = f"[{ts}] [{level}] "
    lines = str(text).splitlines(True)
    if not lines:
        lines = [""]
    return "".join(prefix + line if line != "" else prefix for line in lines)


def _write_timestamped(level: str, text: str, *, color: str | None = None) -> None:
    formatted = _format_lines(level, text)
    lines = formatted.splitlines() or [formatted.rstrip("\n")]
    for line in lines:
        if _log_file is not None:
            try:
                _log_file.write(f"{line}\n")
                _log_file.flush()
            except Exception:
                pass
        if color:
            try:
                _console.print(f"[{color}]{escape(line)}[/]")
                continue
            except Exception:
                pass
        try:
            sys.__stdout__.write(f"{line}\n")
            sys.__stdout__.flush()
        except Exception:
            pass


def _log_color(text: str, color: str, level: str) -> None:
    if not _should_log(level):
        return
    _write_timestamped(level, text, color=color)


def log_debug(text: str) -> None:
    if _should_log("debug"):
        _write_timestamped("debug", text)


def log_info(text: str) -> None:
    if _should_log("info"):
        _write_timestamped("info", text)


def log_warn(text: str) -> None:
    _log_color(text, "yellow", "warning")


def log_error(text: str) -> None:
    _log_color(text, "red", "error")


def log_success(text: str) -> None:
    _log_color(text, "green", "info")


set_log_level(os.environ.get("DVC_LOG_LEVEL", "debug"))
