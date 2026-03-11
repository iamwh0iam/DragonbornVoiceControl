import builtins
import sys
from datetime import datetime
from typing import Optional, TextIO

from rich.console import Console
from rich.markup import escape


def _ts_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


_console = Console(file=sys.__stdout__, force_terminal=True)
_log_file: Optional[TextIO] = None


def set_log_file(file: Optional[TextIO]) -> None:
    global _log_file
    _log_file = file


def setup_timestamped_print() -> None:
    if getattr(builtins, "_dvc_ts_print", None) is not None:
        return

    def _ts_print(*args, **kwargs) -> None:
        sep = kwargs.pop("sep", " ")
        end = kwargs.pop("end", "\n")
        file = kwargs.pop("file", None)
        flush = kwargs.pop("flush", False)

        if file is None:
            file = sys.stdout

        msg = sep.join(str(a) for a in args) + end
        prefix = f"[{_ts_now()}] "
        lines = msg.splitlines(True)
        if not lines:
            lines = [""]
        file.write("".join(prefix + line if line != "" else prefix for line in lines))
        if flush:
            file.flush()

    builtins._dvc_ts_print = _ts_print
    builtins.print = _ts_print


def _log_color(text: str, color: str) -> None:
    ts = _ts_now()
    lines = str(text).splitlines() or [""]
    for line in lines:
        if _log_file is not None:
            try:
                _log_file.write(f"[{ts}] {line}\n")
                _log_file.flush()
            except Exception:
                pass
        try:
            _console.print(f"[{color}][{ts}] {escape(line)}[/]")
        except Exception:
            try:
                sys.__stdout__.write(f"[{ts}] {line}\n")
                sys.__stdout__.flush()
            except Exception:
                pass


def log_warn(text: str) -> None:
    _log_color(text, "yellow")


def log_error(text: str) -> None:
    _log_color(text, "red")


def log_success(text: str) -> None:
    _log_color(text, "green")
